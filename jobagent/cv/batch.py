"""Tailor CVs for every untailored job in the tracker, through the Batch API.

    python -m jobagent.cv batch --max 40          # newest first, up to 40 jobs
    python -m jobagent.cv apply-updates           # write finished CV paths into jobs.csv

The steps are the same requests as `tailor` (llm.py), sent as batches at half price:

    1. research (companies not yet cached) + draft, which also decides whether the
       application needs a cover letter
    2. critique
    3. revise, for drafts the reviewer didn't pass
    4. repair, up to twice, for drafts failing the content checks
    5. for jobs that need one: the cover letter's draft, review, revision and repairs,
       on the finished CV
    then the PDFs are rendered here, as for a single job. A letter that fails never holds
    back the CV: the job finishes with its CV and the letter's error is recorded.

Progress lives in data/tailor_state.json and is saved after every batch, so a run that
reaches its time limit leaves its batch running and the next run collects it. Nothing
is paid for twice. A job in progress that has since been marked applied (yes or no), had
its status changed, or left the tracker is dropped at the start of the next run; a batch
left running for dropped jobs alone is cancelled.

This module never edits jobs.csv. Finished CV and letter paths (or "failed: ...",
"skipped: ..." / "not needed: ...") wait in the state file until `apply-updates`, which the workflow runs right after pulling
the latest data, so tracker edits made during a long run are never overwritten.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from datetime import date

from .. import archive, config, postings, store
from . import letter as letters
from . import llm
from .content import check_content
from .render import build
from .tailor import MAX_REPAIRS, cached_notes, letter_column, out_dir_for, save_notes

STATE_FILE = "tailor_state.json"
# A tracker row is tailored when its status is still new and it hasn't been applied to.
OPEN_STATUSES = {"", "new"}
APPLIED = archive.APPLIED | archive.DECLINED  # marked either way: no CV needed
PREPARE_CONCURRENCY = 6
LETTER_STAGES = ("letter", "letter_review", "letter_revise", "letter_repair")


# ---------------------------------------------------------------- state

def _state_path():
    return config.DATA_DIR / STATE_FILE


def load_state() -> dict:
    path = _state_path()
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    state.setdefault("jobs", {})
    state.setdefault("batch", None)
    state.setdefault("tracker_updates", {})
    return state


def save_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def apply_updates() -> int:
    """Write finished results into jobs.csv. Run on freshly pulled data."""
    state = load_state()
    done = 0
    for url, value in list(state["tracker_updates"].items()):
        fields = value if isinstance(value, dict) else {"cv": value}  # older state: the CV path alone
        store.update_job(url, **fields)  # a row deleted meanwhile is simply skipped
        del state["tracker_updates"][url]
        done += 1
    save_state(state)
    return done


# ---------------------------------------------------------------- choosing jobs

def eligible(rows: list[dict], exclude: set[str]) -> list[dict]:
    """Rows still to tailor, newest first: no CV yet, status new, applied left blank."""
    picked = []
    for index, row in enumerate(rows):
        url = row.get("url", "")
        if not url or row.get("cv", "").strip() or store.job_key(url) in exclude:
            continue
        if row.get("status", "").strip().lower() not in OPEN_STATUSES:
            continue
        if row.get("applied", "").strip().lower() in APPLIED:
            continue
        picked.append((row.get("date_added", ""), index, row))
    return [row for *_, row in sorted(picked, key=lambda t: (t[0], t[1]), reverse=True)]


def _no_longer_needed(row: dict | None) -> str:
    """Why a tracker row no longer wants a CV, or "" if it still does."""
    if row is None:
        return "no longer in the tracker"
    if row.get("applied", "").strip().lower() in archive.APPLIED:
        return "marked applied"
    if row.get("applied", "").strip().lower() in archive.DECLINED:
        return "marked not applying"
    if row.get("status", "").strip().lower() not in OPEN_STATUSES:
        return f"status is {row.get('status', '').strip()}"
    return ""


def drop_unneeded(client, state: dict, log) -> int:
    """Stop work on jobs that no longer need a CV (applied to already, turned down, or
    gone from the tracker). Cancels a running batch that holds nothing else. Returns how
    many jobs were dropped."""
    rows = {}
    for row in store.read_jobs():
        if row.get("url"):
            for key in store.key_aliases(store.job_key(row["url"])):
                rows[key] = row
    dropped = 0
    for pid, job in list(state["jobs"].items()):
        reason = _no_longer_needed(rows.get(store.job_key(job["url"])))
        if reason:
            log(f"  dropping {job['company']}, {job['role']}: {reason}")
            del state["jobs"][pid]
            dropped += 1
    if dropped and state["batch"] and not state["jobs"]:
        batch_id = state["batch"]["id"]
        try:
            client.messages.batches.cancel(batch_id)
            log(f"  cancelled batch {batch_id}: none of its jobs need a CV any more")
        except Exception as exc:  # already ended, or gone: nothing more to pay either way
            log(f"  could not cancel batch {batch_id} ({exc}); its results are ignored")
        state["batch"] = None
    if dropped:
        save_state(state)
    return dropped


async def _prepare(rows: list[dict], need: int, log) -> tuple[list[dict], dict, int]:
    """Check each candidate is still open and has posting text, until `need` are ready.

    Returns (ready postings, tracker updates for closed jobs, count without text).
    """
    ready, closed, missing = [], {}, 0
    sem = asyncio.Semaphore(PREPARE_CONCURRENCY)

    async def one(row):
        url, company, role = row["url"], row.get("company", ""), row.get("role", "")
        async with sem:
            status, fresh = await postings.check(url, company)
        if status == postings.CLOSED:
            return "closed", row
        if status == postings.OPEN and fresh and fresh.description:
            postings.save(url=url, company=company, role=role, text=fresh.description,
                          source=f"{fresh.ats}:{fresh.board_slug}")
        saved = postings.load(url)
        if status == postings.UNKNOWN and not (saved and saved["text"]):
            # Not on a board we can read (an employer's own careers site, LinkedIn): read
            # the page itself.
            async with sem:
                text = await postings.read_page(url, role)
            if text:
                postings.save(url=url, company=company, role=role, text=text, source="page")
                saved = postings.load(url)
        if saved and saved["text"]:
            return "ready", {"url": url, "company": company or saved["company"],
                             "role": role or saved["role"]}
        return "missing", row

    for start in range(0, len(rows), PREPARE_CONCURRENCY * 2):
        if len(ready) >= need:
            break
        for outcome, item in await asyncio.gather(*(one(r) for r in rows[start:start + PREPARE_CONCURRENCY * 2])):
            if outcome == "closed":
                closed[item["url"]] = "skipped: posting closed"
            elif outcome == "missing":
                missing += 1
            elif len(ready) < need:
                ready.append(item)
    if closed:
        log(f"  {len(closed)} posting(s) have closed; not tailoring them")
    return ready, closed, missing


# ---------------------------------------------------------------- requests and results

def _company_id(company: str) -> str:
    return hashlib.sha1(store.company_key(company).encode()).hexdigest()[:16]


def _posting(job: dict) -> dict:
    saved = postings.load(job["url"]) or {"text": ""}
    return {"url": job["url"], "company": job["company"], "role": job["role"], "text": saved["text"]}


def build_requests(state: dict, source: dict) -> tuple[list[dict], dict]:
    """One request per job that is waiting on the model, plus research for new companies."""
    requests, research = [], {}
    for pid, job in state["jobs"].items():
        stage, posting = job["stage"], _posting(job)
        notes = cached_notes(job["company"]) or ""
        if stage == "draft":
            params = llm.draft_request(source, posting)
            if cached_notes(job["company"]) is None:
                research.setdefault(_company_id(job["company"]), posting)
        elif stage == "critique":
            params = llm.critique_request(source, posting, job["cv"], cached_notes(job["company"]) or "")
        elif stage == "revise":
            params = llm.revise_request(source, posting, job["cv"], job["review"])
        elif stage == "repair":
            errors = [p for p in check_content(source, job["cv"]) if p.level == "error"]
            params = llm.repair_request(source, posting, job["cv"], errors)
        elif stage == "letter":
            decision = job.get("decision")
            why = letters.why(decision) if letters.wanted(decision) else "requested by hand"
            params = llm.letter_request(source, posting, job["cv"], notes, why)
        elif stage == "letter_review":
            params = llm.letter_critique_request(source, posting, job["cv"], job["letter"], notes)
        elif stage == "letter_revise":
            params = llm.letter_revise_request(source, posting, job["cv"], job["letter"],
                                               job["letter_review"], notes)
        elif stage == "letter_repair":
            errors = [p for p in letters.check_letter(source, job["letter"], posting, notes)
                      if p.level == "error"]
            params = llm.letter_repair_request(source, posting, job["cv"], job["letter"], errors, notes)
        else:
            continue
        requests.append({"custom_id": f"{stage}-{pid}", "params": params})
    for cid, posting in research.items():
        requests.append({"custom_id": f"research-{cid}", "params": llm.research_request(posting)})
    companies = {cid: p["company"] for cid, p in research.items()}
    return requests, companies


def _keep_gaps(job: dict, new_cv: dict) -> dict:
    if not new_cv.get("gaps") and job.get("cv"):
        new_cv["gaps"] = job["cv"].get("gaps", [])
    return new_cv


def process_results(state: dict, entries, companies: dict, log) -> None:
    jobs = state["jobs"]
    for entry in entries:
        cid = llm.field(entry, "custom_id")
        result = llm.field(entry, "result")
        kind, _, key = cid.partition("-")
        rtype = llm.field(result, "type")
        if rtype in ("expired", "canceled"):
            continue  # never reached the model and not billed: the job stays at this stage
        if kind == "research":
            company = companies.get(key, "")
            if rtype == "succeeded":
                message = llm.field(result, "message")
                notes = llm.text_of(message)
                if notes and company:
                    save_notes(company, notes)
                # Research is shared by every job at the company; book its cost to the first.
                owner = next((j for j in jobs.values() if j["company"] == company), None)
                if owner is not None:
                    owner["steps"].append({"step": "research", "usage": [llm.usage_of(message)]})
            continue
        job = jobs.get(key)
        if job is None:
            continue
        in_letter = kind in LETTER_STAGES
        if rtype != "succeeded":
            if in_letter:  # the letter is lost, the CV is not
                job["letter_error"], job["stage"] = f"API error: {llm.field(result, 'error')}", "render"
            else:
                job["stage"], job["error"] = "failed", f"API error: {llm.field(result, 'error')}"
            continue
        message = llm.field(result, "message")
        job["steps"].append({"step": kind, "usage": [llm.usage_of(message)]})
        try:
            data = llm.reply_json(message)
        except llm.TailorError as exc:
            if in_letter:
                job["letter_error"], job["stage"] = str(exc), "render"
            else:
                job["stage"], job["error"] = "failed", str(exc)
            continue
        if kind == "draft":
            job["decision"] = data.pop("cover_letter", None)
            job["cv"], job["draft"], job["stage"] = data, data, "critique"
        elif kind == "critique":
            job["review"] = data
            ready = data.get("verdict") == "ready" and not data.get("grounding")
            job["stage"] = "check" if ready else "revise"
        elif kind in ("revise", "repair"):
            job["cv"], job["stage"] = _keep_gaps(job, data), "check"
        elif kind == "letter":
            job["letter"], job["letter_draft"], job["stage"] = data, data, "letter_review"
        elif kind == "letter_review":
            job["letter_review"] = data
            ready = data.get("verdict") == "ready" and not data.get("grounding")
            job["stage"] = "letter_check" if ready else "letter_revise"
        elif kind in ("letter_revise", "letter_repair"):
            job["letter"], job["stage"] = data, "letter_check"


# ---------------------------------------------------------------- local steps

def _record(job: dict, problems: list, error: str = "") -> dict:
    usages = [u for s in job["steps"] for u in s["usage"]]
    return {"posting": {k: job[k] for k in ("company", "role", "url")}, "mode": "batch",
            "date": date.today().isoformat(),
            "models": {"draft": llm.DRAFT_MODEL, "review": llm.REVIEW_MODEL},
            "steps": job["steps"], "company_notes": cached_notes(job["company"]) or "",
            "draft": job.get("draft"), "review": job.get("review"), "final": job.get("cv"),
            "problems": [str(p) for p in problems], "error": error,
            "cover_letter": {"decision": job.get("decision"), "forced": bool(job.get("force_letter")),
                             "draft": job.get("letter_draft"), "review": job.get("letter_review"),
                             "final": job.get("letter"), "problems": job.get("letter_problems", []),
                             "error": job.get("letter_error", "")},
            "usage_total_usd_estimate": round(llm.cost(usages, batch=True), 4)}


def _wants_letter(job: dict) -> bool:
    return bool(job.get("force_letter")) or letters.wanted(job.get("decision"))


def _write(out, name: str, data) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / name).write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def advance_local(state: dict, source: dict, results: dict, log) -> None:
    """Content checks, repairs, rendering, and finishing jobs, with no model calls."""
    for pid in list(state["jobs"]):
        job = state["jobs"][pid]
        out = out_dir_for(job)
        problems = []
        if job["stage"] in LETTER_STAGES + ("letter_check",) and not _wants_letter(job):
            # COVER_LETTERS changed while the letter was under way: finish with the CV only.
            job["letter"], job["stage"] = None, "render"
        if job["stage"] == "check":
            problems = check_content(source, job["cv"])
            if any(p.level == "error" for p in problems):
                if job["repairs"] < MAX_REPAIRS:
                    job["repairs"] += 1
                    job["stage"] = "repair"
                    continue
                job["stage"], job["error"] = "failed", "content checks still failing after repairs"
            elif _wants_letter(job) and not job.get("letter") and not job.get("letter_error"):
                job["stage"] = "letter"  # on the finished CV, so the two agree
                continue
            else:
                job["stage"] = "render"
        if job["stage"] == "letter_check":
            found = letters.check_letter(source, job["letter"], _posting(job),
                                         cached_notes(job["company"]) or "")
            job["letter_problems"] = [str(p) for p in found]
            if any(p.level == "error" for p in found):
                if job.get("letter_repairs", 0) < MAX_REPAIRS:
                    job["letter_repairs"] = job.get("letter_repairs", 0) + 1
                    job["stage"] = "letter_repair"
                    continue
                job["letter_error"] = "letter checks still failing after repairs"
            job["stage"] = "render"
        if job["stage"] == "render":
            if not problems:  # arrived here via the letter stages: record the CV's warnings too
                problems = check_content(source, job["cv"])
            pdf, pdf_problems = build(source, job["cv"], out)
            problems += pdf_problems
            if pdf is None or any(p.level == "error" for p in problems):
                job["stage"], job["error"] = "failed", "PDF checks failed"
            else:
                letter_pdf = None
                if job.get("letter") and not job.get("letter_error"):
                    letter_pdf, found = letters.build_letter(source, job["letter"], _posting(job), out)
                    job["letter_problems"] = job.get("letter_problems", []) + [str(p) for p in found]
                    if letter_pdf is None:
                        job["letter_error"] = "letter PDF checks failed"
                    else:
                        _write(out, "cover_letter.json", job["letter"])
                _write(out, "tailored.json", job["cv"])
                _write(out, "review.json", _record(job, problems))
                state["tracker_updates"][job["url"]] = {
                    "cv": str(pdf.relative_to(config.DATA_DIR)),
                    "cover_letter": letter_column(job.get("decision"), letter_pdf, job.get("letter_error", ""),
                                                  forced=bool(job.get("force_letter")))}
                results["tailored"].append(job)
                if letter_pdf is not None:
                    results.setdefault("letters", []).append(job)
                del state["jobs"][pid]
                continue
        if job["stage"] == "failed":
            if job.get("cv"):
                _write(out, "tailored.json", job["cv"])
            _write(out, "review.json", _record(job, problems, job["error"]))
            state["tracker_updates"][job["url"]] = {"cv": f"failed: {job['error']}"[:200]}
            results["failed"].append(job)
            del state["jobs"][pid]


# ---------------------------------------------------------------- the run

def _wait(client, batch_id: str, deadline: float, poll: float) -> bool:
    while True:
        status = llm.field(client.messages.batches.retrieve(batch_id), "processing_status")
        if status == "ended":
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def _collect(client, state: dict, source: dict, results: dict, log) -> None:
    batch = state["batch"]
    process_results(state, client.messages.batches.results(batch["id"]), batch["companies"], log)
    state["batch"] = None
    advance_local(state, source, results, log)
    save_state(state)


def run(client, source: dict, *, max_jobs: int, deadline_seconds: float, poll_seconds: float = 60,
        log=print) -> dict:
    """Tailor up to `max_jobs` jobs. Returns what happened, for the summary."""
    deadline = time.monotonic() + deadline_seconds
    state = load_state()
    results = {"tailored": [], "failed": [], "closed": 0, "missing_text": 0}
    results["dropped"] = drop_unneeded(client, state, log)

    if state["batch"]:
        log(f"Collecting the batch left running by the last run ({state['batch']['id']})")
        if not _wait(client, state["batch"]["id"], deadline, poll_seconds):
            log("  still running; the next run will collect it")
            return _summary(state, results)
        _collect(client, state, source, results, log)

    need = max_jobs - len(state["jobs"])
    if need > 0:
        exclude = {store.job_key(j["url"]) for j in state["jobs"].values()}
        exclude |= {store.job_key(u) for u in state["tracker_updates"]}
        rows = eligible(store.read_jobs(), exclude)
        log(f"{len(rows)} tracked job(s) without a CV; preparing up to {need}")
        ready, closed, missing = asyncio.run(_prepare(rows, need, log))
        state["tracker_updates"].update(closed)
        results["closed"], results["missing_text"] = len(closed), missing
        for posting in ready:
            pid = postings.posting_id(posting["url"])
            state["jobs"][pid] = {**posting, "stage": "draft", "cv": None, "review": None,
                                  "repairs": 0, "steps": [], "error": "", "decision": None,
                                  "letter": None, "letter_review": None, "letter_repairs": 0,
                                  "letter_error": "", "letter_problems": []}
        save_state(state)

    previous = None
    while True:
        advance_local(state, source, results, log)
        requests, companies = build_requests(state, source)
        if not requests:
            break
        if time.monotonic() >= deadline:
            log("  time limit reached; the remaining jobs continue next run")
            break
        # A repeated repair is progress too, so the attempt counts are part of the snapshot.
        progress = {pid: (j["stage"], j.get("repairs", 0), j.get("letter_repairs", 0))
                    for pid, j in state["jobs"].items()}
        if progress == previous:  # a whole round expired unanswered: try again next run
            log("  the last batch expired without answers; retrying next run")
            break
        previous = progress
        stages = sorted({r["custom_id"].split("-")[0] for r in requests})
        log(f"Submitting {len(requests)} request(s): {', '.join(stages)}")
        batch = client.messages.batches.create(requests=requests)
        state["batch"] = {"id": llm.field(batch, "id"), "companies": companies,
                          "submitted": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        save_state(state)
        if not _wait(client, state["batch"]["id"], deadline, poll_seconds):
            log("  time limit reached; the next run will collect this batch")
            break
        _collect(client, state, source, results, log)
    return _summary(state, results)


def _summary(state: dict, results: dict) -> dict:
    spent = sum(llm.cost([u for s in j["steps"] for u in s["usage"]], batch=True)
                for j in results["tailored"] + results["failed"])
    return {"tailored": len(results["tailored"]), "letters": len(results.get("letters", [])),
            "failed": [(j["company"], j["role"], j["error"]) for j in results["failed"]],
            "closed": results["closed"], "missing_text": results["missing_text"],
            "dropped": results.get("dropped", 0),
            "in_progress": len(state["jobs"]), "batch_running": bool(state["batch"]),
            "cost_usd": round(spent, 2)}


def write_step_summary(summary: dict) -> None:
    """A short report on the Actions run page (company names only, never CV content)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [f"## Tailored CVs, {date.today().isoformat()}", "",
             f"Tailored **{summary['tailored']}** (with {summary.get('letters', 0)} cover letter(s)), "
             f"failed {len(summary['failed'])}, "
             f"skipped {summary['closed']} closed posting(s). About ${summary['cost_usd']:.2f} "
             f"at batch prices.", ""]
    if summary.get("dropped"):
        lines.append(f"Stopped work on {summary['dropped']} job(s) already marked applied, marked "
                     "not applying, or gone from the tracker.")
    if summary["missing_text"]:
        lines.append(f"{summary['missing_text']} job(s) had no readable posting text; "
                     "use the manual workflow with --posting-file for those.")
    if summary["in_progress"] or summary["batch_running"]:
        lines.append(f"{summary['in_progress']} job(s) still in progress; the next run continues them.")
    for company, role, error in summary["failed"]:
        lines.append(f"- Failed: {company}, {role}: {error}")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
