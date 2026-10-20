"""Tailor CVs for every untailored job in the tracker, through the Batch API.

    python -m jobagent.cv batch --max 40          # newest first, up to 40 jobs
    python -m jobagent.cv apply-updates           # write finished CV paths into jobs.csv

The steps are the same requests as `tailor` (llm.py), sent as batches at half price:

    1. research (companies not yet cached) + draft
    2. critique
    3. revise, for drafts the reviewer didn't pass
    4. repair, up to twice, for drafts failing the content checks
    then the PDF is rendered here, as for a single job.

Progress lives in data/tailor_state.json and is saved after every batch, so a run that
reaches its time limit leaves its batch running and the next run collects it. Nothing
is paid for twice.

This module never edits jobs.csv. Finished CV paths (or "failed: ..." / "skipped: ...")
wait in the state file until `apply-updates`, which the workflow runs right after pulling
the latest data, so tracker edits made during a long run are never overwritten.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from datetime import date

from .. import config, postings, store
from . import llm
from .content import check_content
from .render import build
from .tailor import MAX_REPAIRS, cached_notes, out_dir_for, save_notes

STATE_FILE = "tailor_state.json"
# A tracker row is tailored when its status is still new and it hasn't been applied to.
OPEN_STATUSES = {"", "new"}
APPLIED = {"yes", "y", "true", "1", "x", "applied", "done"}
PREPARE_CONCURRENCY = 6


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
        store.update_job(url, cv=value)  # a row deleted meanwhile is simply skipped
        del state["tracker_updates"][url]
        done += 1
    save_state(state)
    return done


# ---------------------------------------------------------------- choosing jobs

def eligible(rows: list[dict], exclude: set[str]) -> list[dict]:
    """Rows still to tailor, newest first: no CV yet, status new, not applied to."""
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
        if rtype != "succeeded":
            job["stage"], job["error"] = "failed", f"API error: {llm.field(result, 'error')}"
            continue
        message = llm.field(result, "message")
        job["steps"].append({"step": kind, "usage": [llm.usage_of(message)]})
        try:
            data = llm.reply_json(message)
        except llm.TailorError as exc:
            job["stage"], job["error"] = "failed", str(exc)
            continue
        if kind == "draft":
            job["cv"], job["draft"], job["stage"] = data, data, "critique"
        elif kind == "critique":
            job["review"] = data
            ready = data.get("verdict") == "ready" and not data.get("grounding")
            job["stage"] = "check" if ready else "revise"
        elif kind in ("revise", "repair"):
            job["cv"], job["stage"] = _keep_gaps(job, data), "check"


# ---------------------------------------------------------------- local steps

def _record(job: dict, problems: list, error: str = "") -> dict:
    usages = [u for s in job["steps"] for u in s["usage"]]
    return {"posting": {k: job[k] for k in ("company", "role", "url")}, "mode": "batch",
            "date": date.today().isoformat(),
            "models": {"draft": llm.DRAFT_MODEL, "review": llm.REVIEW_MODEL},
            "steps": job["steps"], "company_notes": cached_notes(job["company"]) or "",
            "draft": job.get("draft"), "review": job.get("review"), "final": job.get("cv"),
            "problems": [str(p) for p in problems], "error": error,
            "usage_total_usd_estimate": round(llm.cost(usages, batch=True), 4)}


def _write(out, name: str, data) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / name).write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def advance_local(state: dict, source: dict, results: dict, log) -> None:
    """Content checks, repairs, rendering, and finishing jobs, with no model calls."""
    for pid in list(state["jobs"]):
        job = state["jobs"][pid]
        out = out_dir_for(job)
        problems = []
        if job["stage"] == "check":
            problems = check_content(source, job["cv"])
            if any(p.level == "error" for p in problems):
                if job["repairs"] < MAX_REPAIRS:
                    job["repairs"] += 1
                    job["stage"] = "repair"
                    continue
                job["stage"], job["error"] = "failed", "content checks still failing after repairs"
            else:
                job["stage"] = "render"
        if job["stage"] == "render":
            pdf, pdf_problems = build(source, job["cv"], out)
            problems += pdf_problems
            if pdf is None or any(p.level == "error" for p in problems):
                job["stage"], job["error"] = "failed", "PDF checks failed"
            else:
                _write(out, "tailored.json", job["cv"])
                _write(out, "review.json", _record(job, problems))
                state["tracker_updates"][job["url"]] = str(pdf.relative_to(config.DATA_DIR))
                results["tailored"].append(job)
                del state["jobs"][pid]
                continue
        if job["stage"] == "failed":
            if job.get("cv"):
                _write(out, "tailored.json", job["cv"])
            _write(out, "review.json", _record(job, problems, job["error"]))
            state["tracker_updates"][job["url"]] = f"failed: {job['error']}"[:200]
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
                                  "repairs": 0, "steps": [], "error": ""}
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
        progress = {pid: j["stage"] for pid, j in state["jobs"].items()}
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
    return {"tailored": len(results["tailored"]), "failed": [(j["company"], j["role"], j["error"])
                                                             for j in results["failed"]],
            "closed": results["closed"], "missing_text": results["missing_text"],
            "in_progress": len(state["jobs"]), "batch_running": bool(state["batch"]),
            "cost_usd": round(spent, 2)}


def write_step_summary(summary: dict) -> None:
    """A short report on the Actions run page (company names only, never CV content)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [f"## Tailored CVs, {date.today().isoformat()}", "",
             f"Tailored **{summary['tailored']}**, failed {len(summary['failed'])}, "
             f"skipped {summary['closed']} closed posting(s). About ${summary['cost_usd']:.2f} "
             f"at batch prices.", ""]
    if summary["missing_text"]:
        lines.append(f"{summary['missing_text']} job(s) had no readable posting text; "
                     "use the manual workflow with --posting-file for those.")
    if summary["in_progress"] or summary["batch_running"]:
        lines.append(f"{summary['in_progress']} job(s) still in progress; the next run continues them.")
    for company, role, error in summary["failed"]:
        lines.append(f"- Failed: {company}, {role}: {error}")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
