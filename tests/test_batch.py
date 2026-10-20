"""Batch tailoring with a fake Batch API client: stages, repairs, failures, resuming."""
import copy
import json
import shutil
from pathlib import Path

import pytest

from jobagent import config, postings, store
from jobagent.cv import batch, llm
from jobagent.cv.content import baseline, load_source
from jobagent.cv.tailor import save_notes

SOURCE = load_source(Path(__file__).parent.parent / "example" / "profile" / "cv_source.yaml")
needs_latex = pytest.mark.skipif(not shutil.which("pdflatex"), reason="pdflatex not installed")

READY = {"verdict": "ready", "grounding": [], "coverage": [], "emphasis": [], "overall": "ok"}
REVISE = {"verdict": "revise", "grounding": [{"where": "summary", "quote": "", "problem": "x"}],
          "coverage": [], "emphasis": [], "overall": "fix"}


def good_cv(**extra):
    cv = copy.deepcopy(baseline(SOURCE))
    cv["gaps"] = ["Rust"]
    cv.update(extra)
    return cv


def bad_cv():
    cv = good_cv()
    cv["skills"][0]["items"].append("COBOL")
    return cv


def reply(data=None, text=None):
    body = json.dumps(data) if text is None else text
    return {"model": llm.DRAFT_MODEL, "stop_reason": "end_turn",
            "content": [{"type": "text", "text": body}],
            "usage": {"input_tokens": 4000, "output_tokens": 1000}}


class FakeBatches:
    """Answers each request by its kind; `script[kind]` maps (job url) -> reply data."""

    def __init__(self, script, hold=0):
        self.script, self.hold, self.submitted, self.store = script, hold, [], {}

    def create(self, requests):
        bid = f"batch_{len(self.submitted)}"
        self.submitted.append([r["custom_id"].split("-")[0] for r in requests])
        self.store[bid] = requests
        return {"id": bid}

    def retrieve(self, bid):
        if self.hold:
            self.hold -= 1
            return {"processing_status": "in_progress"}
        return {"processing_status": "ended"}

    def results(self, bid):
        out = []
        for r in self.store[bid]:
            kind = r["custom_id"].split("-")[0]
            if kind == "research":
                out.append({"custom_id": r["custom_id"], "result": {
                    "type": "succeeded", "message": {**reply(text="- builds robots"),
                                                     "usage": {"input_tokens": 9000, "output_tokens": 300,
                                                               "server_tool_use": {"web_search_requests": 2}}}}})
                continue
            url = r["params"]["messages"][0]["content"].split("URL: ", 1)[1].split("\n", 1)[0]
            answer = self.script[kind](url)
            if isinstance(answer, dict) and answer.get("type") in ("errored", "expired"):
                out.append({"custom_id": r["custom_id"], "result": answer})
            else:
                out.append({"custom_id": r["custom_id"], "result": {"type": "succeeded",
                                                                    "message": reply(answer)}})
        return out


class Client:
    def __init__(self, script, hold=0):
        self.messages = type("M", (), {})()
        self.messages.batches = FakeBatches(script, hold)

    @property
    def stages(self):
        return [sorted(set(b)) for b in self.messages.batches.submitted]


@pytest.fixture
def data(tmp_path, monkeypatch):
    for attr, name in [("JOBS_CSV", "jobs.csv"), ("SEEN_CSV", "seen.csv")]:
        monkeypatch.setattr(config, attr, tmp_path / name)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    async def unknown(url, company=""):  # job boards unreachable: use the saved text
        return postings.UNKNOWN, None
    monkeypatch.setattr(postings, "check", unknown)
    return tmp_path


def track(n, company="Acme", date_added=None, text="Python and Kafka services.", **row):
    url = f"https://jobs.lever.co/{company.lower()}/{n}"
    store.append_jobs([{"company": company, "role": f"Engineer {n}", "url": url, "market": "India",
                        "why": "fits"}])
    if date_added or row:
        store.update_job(url, **({"date_added": date_added} if date_added else {}), **row)
    if text:
        postings.save(url=url, company=company, role=f"Engineer {n}", text=text)
    return url


def run(client, **kw):
    kw.setdefault("max_jobs", 40)
    kw.setdefault("deadline_seconds", 60)
    return batch.run(client, SOURCE, poll_seconds=0, log=lambda *_: None, **kw)


@needs_latex
def test_two_jobs_through_every_stage(data):
    save_notes("Acme", "- cached")
    a = track(1, "Acme")
    b = track(2, "Beta")
    client = Client({"draft": lambda u: good_cv(), "revise": lambda u: good_cv(gaps=[]),
                     "critique": lambda u: READY if u == a else REVISE})
    summary = run(client)
    assert summary["tailored"] == 2 and summary["failed"] == [] and summary["in_progress"] == 0
    # Research only for the uncached company, alongside the drafts; one revision.
    assert client.stages == [["draft", "research"], ["critique"], ["revise"]]
    # jobs.csv is untouched until apply_updates.
    assert all(not j["cv"] for j in store.read_jobs())
    assert batch.apply_updates() == 2
    cvs = {j["url"]: j["cv"] for j in store.read_jobs()}
    assert all(v.startswith("applications/") and v.endswith("cv.pdf") for v in cvs.values())
    rec = json.loads((config.DATA_DIR / cvs[b]).with_name("review.json").read_text())
    assert rec["mode"] == "batch" and [s["step"] for s in rec["steps"]][:2] == ["draft", "research"]
    assert rec["final"]["gaps"] == ["Rust"]  # the revision's empty gaps didn't wipe them
    assert batch.load_state() == {"jobs": {}, "batch": None, "tracker_updates": {}}


def test_newest_first_and_only_untouched_rows(data):
    track(1, date_added="2026-10-01")
    newest = track(2, date_added="2026-10-19")
    middle = track(3, date_added="2026-10-10")
    track(4, date_added="2026-10-20", applied="Yes")
    track(5, date_added="2026-10-20", status="rejected")
    track(6, date_added="2026-10-20", cv="applications/x/cv.pdf")
    rows = batch.eligible(store.read_jobs(), exclude=set())
    assert [r["url"] for r in rows][:2] == [newest, middle] and len(rows) == 3


def test_max_jobs_caps_the_run_and_closed_postings_are_skipped(data, monkeypatch):
    for n in range(5):
        track(n, date_added=f"2026-10-1{n}")

    async def check(url, company=""):
        return (postings.CLOSED, None) if url.endswith("/4") else (postings.UNKNOWN, None)
    monkeypatch.setattr(postings, "check", check)
    client = Client({"draft": lambda u: None})  # never reached: we stop before submitting
    client.messages.batches.create = lambda requests: (_ for _ in ()).throw(SystemExit)
    with pytest.raises(SystemExit):
        run(client, max_jobs=2)
    state = batch.load_state()
    assert sorted(j["url"][-1] for j in state["jobs"].values()) == ["2", "3"]  # newest open two
    assert state["tracker_updates"] == {"https://jobs.lever.co/acme/4": "skipped: posting closed"}


@needs_latex
def test_check_failures_are_repaired_then_given_up_on(data):
    save_notes("Acme", "- cached")
    fixable = track(1)
    hopeless = track(2)
    repairs = {fixable: [good_cv()], hopeless: [bad_cv(), bad_cv()]}
    client = Client({"draft": lambda u: bad_cv(), "critique": lambda u: READY,
                     "repair": lambda u: repairs[u].pop(0)})
    summary = run(client)
    assert summary["tailored"] == 1
    assert [f[2] for f in summary["failed"]] == ["content checks still failing after repairs"]
    assert client.stages == [["draft"], ["critique"], ["repair"], ["repair"]]
    updates = batch.load_state()["tracker_updates"]
    assert updates[hopeless].startswith("failed: content checks")
    assert updates[fixable].endswith("cv.pdf")


def test_an_errored_request_fails_only_its_job_and_expired_ones_wait(data):
    save_notes("Acme", "- cached")
    broken = track(1)
    late = track(2)
    client = Client({"draft": lambda u: {"type": "errored", "error": {"message": "overloaded"}}
                     if u == broken else {"type": "expired"}})
    summary = run(client)
    assert [f[0] for f in summary["failed"]] == ["Acme"] and "overloaded" in summary["failed"][0][2]
    # The expired draft is retried once, then left for the next run rather than looping.
    assert client.stages == [["draft"], ["draft"]]
    state = batch.load_state()
    assert [j["url"] for j in state["jobs"].values()] == [late]  # expired: still to draft
    assert state["jobs"][next(iter(state["jobs"]))]["stage"] == "draft"


@needs_latex
def test_a_batch_left_running_is_collected_by_the_next_run(data):
    save_notes("Acme", "- cached")
    track(1)
    script = {"draft": lambda u: good_cv(), "critique": lambda u: READY}
    first = Client(script, hold=10**9)          # never finishes within this run
    summary = run(first, deadline_seconds=0.05)
    assert summary["batch_running"] and summary["tailored"] == 0
    assert batch.load_state()["batch"]["id"] == "batch_0"
    # Next run: the same batch (same fake store) has finished.
    second = Client(script)
    second.messages.batches.store = first.messages.batches.store
    second.messages.batches.submitted = list(first.messages.batches.submitted)
    summary = run(second)
    assert summary["tailored"] == 1 and not summary["batch_running"]
    assert second.stages == [["draft"], ["critique"]]  # the draft batch was not resubmitted


def test_nothing_is_submitted_once_the_time_limit_has_passed(data):
    save_notes("Acme", "- cached")
    track(1)
    client = Client({})
    summary = run(client, deadline_seconds=0)
    assert client.stages == [] and summary["in_progress"] == 1


def test_jobs_without_posting_text_are_counted_not_drafted(data):
    track(1, text="")
    client = Client({})
    summary = run(client)
    assert summary["missing_text"] == 1 and client.stages == []


def test_apply_updates_keeps_edits_made_during_the_run(data):
    url = track(1)
    state = batch.load_state()
    state["tracker_updates"][url] = "applications/a/cv.pdf"
    batch.save_state(state)
    store.update_job(url, applied="Yes")  # you marked it applied while the batch ran
    batch.apply_updates()
    row = store.read_jobs()[0]
    assert row["cv"] == "applications/a/cv.pdf" and row["applied"] == "Yes"


def test_batch_cost_halves_tokens_but_not_searches():
    u = {"model": "claude-opus-5-5", "input": 1_000_000, "output": 0, "cache_read": 0,
         "cache_write": 0, "searches": 2}
    assert llm.cost([u], batch=True) == pytest.approx(2.02)


# ---------------------------------------------------------------- is the posting still open?

def _fake_board(monkeypatch, postings_on_board=(), error=None):
    import asyncio as _a
    from jobagent.ats import ADAPTERS, Posting

    async def fetch(client, board):
        if error:
            raise error
        return [Posting("lever", board.slug, "Acme", jid, "MLE", f"https://jobs.lever.co/acme/{jid}",
                        description="Open role text") for jid in postings_on_board]
    monkeypatch.setattr(ADAPTERS["lever"], "fetch", fetch)
    return lambda url, company="Acme": _a.run(postings.check(url, company))


def test_a_posting_still_on_its_board_is_open(monkeypatch):
    check = _fake_board(monkeypatch, ["abc123"])
    status, p = check("https://jobs.lever.co/acme/abc123")
    assert status == postings.OPEN and p.description == "Open role text"


def test_a_posting_gone_from_its_own_board_is_closed(monkeypatch):
    check = _fake_board(monkeypatch, ["other99"])
    assert check("https://jobs.lever.co/acme/abc123")[0] == postings.CLOSED


def test_network_trouble_is_never_read_as_closed(monkeypatch):
    import httpx
    check = _fake_board(monkeypatch, error=httpx.ConnectError("down"))
    assert check("https://jobs.lever.co/acme/abc123")[0] == postings.UNKNOWN


def test_a_404_on_the_posting_itself_is_closed(monkeypatch):
    from jobagent.ats.base import BoardError
    check = _fake_board(monkeypatch, error=BoardError("not found (404) at https://..."))
    assert check("https://jobs.lever.co/acme/abc123")[0] == postings.CLOSED
