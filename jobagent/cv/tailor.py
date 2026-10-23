"""Tailor the CV for one posting: research, draft, review, revise, check, render.

    research   what the company does (Sonnet + web search; cached per company)
    draft      a selection and rewording of the CV source (Opus), and whether the
               posting needs a cover letter
    critique   a hiring-manager review of the draft (Sonnet)
    revise     the draft again with the review (Opus), unless the review says "ready"
    repair     up to two more tries if the automatic content checks fail
    letter     when needed (or forced): the cover letter's draft, review, revision and
               repairs, the same way, on the finished CV. A failed letter never holds
               back the CV.
    render     LaTeX to PDF, fitted to the page limit, then the PDF checks

Each step is one request built in llm.py. Output goes to
data/applications/<company>_<role>_<id>/: <Your_Name>_CV.pdf, cv.tex, tailored.json,
review.json, and with a letter <Your_Name>_Cover_Letter.pdf, cover_letter.tex and
cover_letter.json.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .. import config, postings, store
from . import letter as letters
from . import llm
from .content import Problem, check_content
from .render import build

COMPANY_NOTES_MAX_AGE_DAYS = 45
MAX_REPAIRS = 2


@dataclass
class Result:
    ok: bool
    out_dir: Path
    pdf: Path | None = None
    problems: list[Problem] = field(default_factory=list)
    usages: list[dict] = field(default_factory=list)
    error: str = ""
    letter_pdf: Path | None = None
    letter_note: str = ""  # what goes in the tracker's cover_letter column

    @property
    def cost(self) -> float:
        return llm.cost(self.usages)


def _slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:limit].strip("-") or "unknown"


def out_dir_for(posting: dict) -> Path:
    pid = postings.posting_id(posting["url"])[:6]
    return (config.DATA_DIR / "applications"
            / f"{_slug(posting.get('company'))}_{_slug(posting.get('role'))}_{pid}")


# ---------------------------------------------------------------- company notes cache

def _notes_path(company: str) -> Path:
    return config.DATA_DIR / "companies" / f"{_slug(store.company_key(company), 60)}.json"


def cached_notes(company: str) -> str | None:
    path = _notes_path(company)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    age = (date.today() - date.fromisoformat(data.get("saved", "1970-01-01"))).days
    return data.get("notes") if age <= COMPANY_NOTES_MAX_AGE_DAYS else None


def save_notes(company: str, notes: str) -> None:
    path = _notes_path(company)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"company": company, "saved": date.today().isoformat(),
                                "notes": notes}, indent=1, ensure_ascii=False), encoding="utf-8")


def letter_column(decision: dict | None, pdf: Path | None, error: str = "", forced: bool = False) -> str:
    """The tracker's cover_letter cell: the PDF, why there is none, or why it failed."""
    if pdf is not None:
        return str(pdf.relative_to(config.DATA_DIR))
    if error:
        return f"failed: {error}"[:200]
    if forced or letters.wanted(decision):
        return ""
    if not decision:
        return ""
    reason = " ".join((decision.get("reason") or "").split())
    label = "not wanted" if decision.get("decision") == "declined" else "not needed"
    return f"{label}: {reason}"[:150] if reason else label


# ---------------------------------------------------------------- the pipeline

def tailor(client, source: dict, posting: dict, log=print, force_letter: bool = False) -> Result:
    out = out_dir_for(posting)
    out.mkdir(parents=True, exist_ok=True)
    usages: list[dict] = []
    record: dict = {"posting": {k: posting.get(k, "") for k in ("company", "role", "url")},
                    "date": date.today().isoformat(), "models": {"draft": llm.DRAFT_MODEL,
                                                                 "review": llm.REVIEW_MODEL},
                    "steps": []}

    def step(name: str, params: dict):
        started = time.monotonic()
        replies, used = llm.send(client, params)
        usages.extend(used)
        record["steps"].append({"step": name, "seconds": round(time.monotonic() - started, 1),
                                "usage": used})
        return replies

    def finish(result: Result) -> Result:
        record["usage_total_usd_estimate"] = round(llm.cost(usages), 4)
        record["problems"] = [str(p) for p in result.problems]
        record["error"] = result.error
        (out / "review.json").write_text(json.dumps(record, indent=1, ensure_ascii=False),
                                         encoding="utf-8")
        result.usages = usages
        return result

    try:
        # 1. What the company does, from cache when we've looked recently.
        notes = cached_notes(posting.get("company", ""))
        if notes is None:
            log("  researching the company")
            replies = step("research", llm.research_request(posting))
            notes = "\n".join(llm.text_of(r) for r in replies).strip()
            if notes:
                save_notes(posting.get("company", ""), notes)
        else:
            log("  company notes from cache")
        record["company_notes"] = notes

        # 2. Draft.
        log("  drafting")
        cv = llm.reply_json(step("draft", llm.draft_request(source, posting))[-1])
        decision = cv.pop("cover_letter", None)
        record["draft"] = cv
        record["cover_letter"] = {"decision": decision, "forced": force_letter}

        # 3. Review, and revise unless it's ready.
        log("  reviewing")
        review = llm.reply_json(step("critique", llm.critique_request(source, posting, cv, notes))[-1])
        record["review"] = review
        if review.get("verdict") != "ready" or review.get("grounding"):
            log(f"  revising ({len(review.get('grounding') or [])} grounding, "
                f"{len(review.get('coverage') or [])} coverage, "
                f"{len(review.get('emphasis') or [])} emphasis points)")
            gaps = cv.get("gaps", [])
            cv = llm.reply_json(step("revise", llm.revise_request(source, posting, cv, review))[-1])
            if not cv.get("gaps"):  # a revision tends to return an empty list
                cv["gaps"] = gaps

        # 4. The automatic content checks, with up to two repair attempts.
        problems = check_content(source, cv)
        for attempt in range(MAX_REPAIRS):
            errors = [p for p in problems if p.level == "error"]
            if not errors:
                break
            log(f"  repairing {len(errors)} check failure(s), attempt {attempt + 1}")
            gaps = cv.get("gaps", [])
            cv = llm.reply_json(step("repair", llm.repair_request(source, posting, cv, errors))[-1])
            if not cv.get("gaps"):  # a revision tends to return an empty list
                cv["gaps"] = gaps
            problems = check_content(source, cv)
        (out / "tailored.json").write_text(json.dumps(cv, indent=1, ensure_ascii=False), encoding="utf-8")
        record["final"] = cv
        if any(p.level == "error" for p in problems):
            return finish(Result(False, out, problems=problems,
                                 error="content checks still failing after repairs"))

        # 5. The cover letter, on the finished CV, when the posting needs one.
        letter, letter_error = None, ""
        if force_letter or letters.wanted(decision):
            letter, letter_error = _write_letter(step, source, posting, cv, notes, decision, record, log)

        # 6. Render, fit, and check the PDFs.
        log("  rendering")
        pdf, pdf_problems = build(source, cv, out)
        problems += pdf_problems
        # Fitting may have dropped bullets; keep the JSON in step with the PDF.
        (out / "tailored.json").write_text(json.dumps(cv, indent=1, ensure_ascii=False), encoding="utf-8")
        ok = pdf is not None and not any(p.level == "error" for p in problems)
        letter_pdf = None
        if ok and letter is not None and not letter_error:
            letter_pdf, found = letters.build_letter(source, letter, posting, out)
            record["cover_letter"]["problems"] = record["cover_letter"].get("problems", []) + [str(p) for p in found]
            if letter_pdf is None:
                letter_error = "letter PDF checks failed"
            else:
                (out / "cover_letter.json").write_text(json.dumps(letter, indent=1, ensure_ascii=False),
                                                       encoding="utf-8")
        record["cover_letter"]["error"] = letter_error
        return finish(Result(ok, out, pdf=pdf, problems=problems,
                             error="" if ok else "PDF checks failed", letter_pdf=letter_pdf,
                             letter_note=letter_column(decision, letter_pdf, letter_error, force_letter)))
    except llm.TailorError as exc:
        return finish(Result(False, out, error=str(exc)))
    except Exception as exc:  # an API error: keep what was spent and the steps so far
        if type(exc).__module__.split(".")[0] != "anthropic":
            raise
        return finish(Result(False, out, error=f"API error: {exc}"))


def _write_letter(step, source: dict, posting: dict, cv: dict, notes: str, decision: dict | None,
                  record: dict, log) -> tuple[dict | None, str]:
    """Draft, review, revise and check the letter. Returns (letter, error); never raises
    for a bad reply, because a failed letter mustn't cost the CV."""
    why = letters.why(decision) if letters.wanted(decision) else "requested by hand"
    info = record["cover_letter"]
    try:
        log("  writing the cover letter")
        letter = llm.reply_json(step("letter", llm.letter_request(source, posting, cv, notes, why))[-1])
        info["draft"] = letter
        review = llm.reply_json(step("letter_review", llm.letter_critique_request(
            source, posting, cv, letter, notes))[-1])
        info["review"] = review
        if review.get("verdict") != "ready" or review.get("grounding"):
            log("  revising the cover letter")
            letter = llm.reply_json(step("letter_revise", llm.letter_revise_request(
                source, posting, cv, letter, review, notes))[-1])
        found = letters.check_letter(source, letter, posting, notes)
        for attempt in range(MAX_REPAIRS):
            errors = [p for p in found if p.level == "error"]
            if not errors:
                break
            log(f"  repairing the cover letter ({len(errors)} check failure(s)), attempt {attempt + 1}")
            letter = llm.reply_json(step("letter_repair", llm.letter_repair_request(
                source, posting, cv, letter, errors, notes))[-1])
            found = letters.check_letter(source, letter, posting, notes)
        info["final"], info["problems"] = letter, [str(p) for p in found]
        if any(p.level == "error" for p in found):
            return None, "letter checks still failing after repairs"
        return letter, ""
    except llm.TailorError as exc:
        return None, str(exc)
    except Exception as exc:  # an API error during the letter costs only the letter
        if type(exc).__module__.split(".")[0] != "anthropic":
            raise
        return None, f"API error: {exc}"


def tailor_url(client, source: dict, url: str, *, posting_file: Path | None = None,
               company: str = "", role: str = "", log=print, force_letter: bool = False) -> Result:
    """Look the job up in the tracker, get its posting text, tailor, and note the CV."""
    import asyncio

    row = next((j for j in store.read_jobs() if store.job_key(j.get("url", "")) == store.job_key(url)), {})
    company = company or row.get("company", "")
    role = role or row.get("role", "")
    if posting_file:
        postings.save(url=url, company=company, role=role,
                      text=Path(posting_file).read_text(encoding="utf-8"), source="pasted")
        posting = postings.load(url)
    else:
        posting = asyncio.run(postings.ensure(url, company, role))
    if not posting or not posting.get("text"):
        return Result(False, out_dir_for({"url": url, "company": company, "role": role}),
                      error="no posting text: the job board couldn't be read. Save the posting "
                            "to a text file and pass --posting-file.")
    posting["company"] = company or posting.get("company", "")
    posting["role"] = role or posting.get("role", "")
    result = tailor(client, source, posting, log=log, force_letter=force_letter)
    if result.ok and result.pdf and row:
        store.update_job(url, cv=str(result.pdf.relative_to(config.DATA_DIR)),
                         cover_letter=result.letter_note)
    return result
