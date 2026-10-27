"""Cover letters: whether one is wanted, the rules a letter must pass, and its PDF.

A letter is JSON the model returns: {"greeting", "paragraphs": [...], "closing"}.
The name, contact details, date, company and role line and signature come from the
source and the posting at render time, so the model can't get them wrong.

check_letter() is pure, like check_content(): every rule is unit-tested.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from pathlib import Path

import yaml

from .. import config
from .content import Problem, numbers
from .render import _env, _squash, compile_pdf

MIN_WORDS = 180
MAX_WORDS = 380
MIN_PARAGRAPHS = 3
MAX_PARAGRAPHS = 5
ASKED = {"required", "invited"}

_PLACEHOLDER = re.compile(r"\[[^\]]*\]|\{[^}]*\}|<[^>]+>|\bX{3,}\b|lorem ipsum", re.I)
_CLICHES = re.compile(r"\bpassionate\b|perfect fit|\bdynamic\b|excited to apply|great asset|"
                      r"i am writing to|to whom it may concern", re.I)


def wanted(decision: dict | None) -> bool:
    """Whether to write a letter, by the posting's stance and config.COVER_LETTERS."""
    if not decision:
        return False
    stance = decision.get("decision")
    if config.COVER_LETTERS == "unless_declined":
        return stance != "declined"
    if config.COVER_LETTERS == "when_asked":
        return stance in ASKED
    return stance == "required"  # "when_required", the default (and any unknown value)  # "unless_declined": forms often have a box the posting never mentions


def why(decision: dict | None) -> str:
    """What the letter writer is told about why this application has a letter."""
    stance = (decision or {}).get("decision")
    if stance in ASKED:
        return (decision or {}).get("reason", "") or "the posting asks for one"
    if stance and stance != "declined":
        return ("the posting doesn't mention one; it's for the application form's cover letter "
                "field, so write it as a letter for this role, not a reply to the posting")
    return "requested by hand"


def _words(text: str) -> int:
    return len((text or "").split())


def check_letter(source: dict, letter: dict, posting: dict, notes: str = "") -> list[Problem]:
    """The automatic rules. Errors send the letter back for repair; warnings are recorded."""
    problems: list[Problem] = []
    err = lambda where, msg: problems.append(Problem("error", where, msg))
    warn = lambda where, msg: problems.append(Problem("warn", where, msg))

    greeting = (letter.get("greeting") or "").strip()
    closing = (letter.get("closing") or "").strip()
    paragraphs = [p for p in letter.get("paragraphs") or [] if (p or "").strip()]
    if not greeting:
        err("greeting", "missing")
    elif _words(greeting) > 8:
        err("greeting", f"{_words(greeting)} words; a greeting is a short line")
    if not closing:
        err("closing", "missing")
    if not MIN_PARAGRAPHS <= len(paragraphs) <= MAX_PARAGRAPHS:
        err("paragraphs", f"{len(paragraphs)} paragraphs; use {MIN_PARAGRAPHS} to {MAX_PARAGRAPHS}")
    total = sum(_words(p) for p in paragraphs)
    if total > MAX_WORDS:
        err("paragraphs", f"{total} words; limit {MAX_WORDS}")
    elif total < MIN_WORDS:
        warn("paragraphs", f"only {total} words")

    # A number must come from the source, the posting or the company notes.
    known = numbers(yaml.safe_dump({k: v for k, v in source.items() if k not in ("contact", "layout")},
                                   allow_unicode=True))
    known |= numbers(posting.get("text", "")) | numbers(notes)
    for i, text in enumerate(paragraphs, 1):
        where = f"paragraphs.{i}"
        for n in sorted(numbers(text) - known):
            err(where, f"number '{n}' appears in none of the source, the posting or the company notes")
        if _PLACEHOLDER.search(text):
            err(where, f"placeholder left in: {_PLACEHOLDER.search(text).group(0)!r}")
        for m in _CLICHES.finditer(text):
            warn(where, f"stock phrase: {m.group(0)!r}")
    for where, text in (("greeting", greeting), ("closing", closing)):
        if _PLACEHOLDER.search(text):
            err(where, f"placeholder left in: {_PLACEHOLDER.search(text).group(0)!r}")

    company = (posting.get("company") or "").strip()
    body = " ".join(paragraphs).lower()
    if company and company.lower() not in body and company.split()[0].lower() not in body:
        warn("paragraphs", f"never names {company}")
    return problems


# ---------------------------------------------------------------- rendering

def letter_filename(source: dict) -> str:
    """'Jane_Example_Cover_Letter.pdf', next to the CV."""
    plain = unicodedata.normalize("NFKD", source.get("name", "")).encode("ascii", "ignore").decode()
    name = re.sub(r"[^A-Za-z0-9]+", "_", plain).strip("_")
    return f"{name}_Cover_Letter.pdf" if name else "Cover_Letter.pdf"


def render_letter_tex(source: dict, letter: dict, posting: dict, today: date | None = None) -> str:
    c = source["contact"]
    contact = [c.get(k) for k in ("location", "phone", "email", "linkedin", "github") if c.get(k)]
    when = today or date.today()
    return _env().get_template("letter.tex").render(
        name=source["name"], contact_line=" | ".join(contact),
        date=f"{when.day} {when:%B %Y}", company=posting.get("company", ""), role=posting.get("role", ""),
        greeting=letter.get("greeting", "").strip(), closing=letter.get("closing", "").strip(),
        paragraphs=[p.strip() for p in letter.get("paragraphs") or [] if (p or "").strip()])


def check_letter_pdf(pdf: Path, source: dict, letter: dict) -> list[Problem]:
    from pypdf import PdfReader

    problems: list[Problem] = []
    pages = [p.extract_text() or "" for p in PdfReader(str(pdf)).pages]
    flat = _squash("\n".join(pages))
    if len(pages) > 1:
        problems.append(Problem("error", "layout", f"{len(pages)} pages; a cover letter is one page"))
    if _squash(source["name"]) not in flat:
        problems.append(Problem("error", "text layer", "name is not readable as text"))
    email = source["contact"].get("email")
    if email and _squash(email) not in flat:
        problems.append(Problem("error", "text layer", "email is not readable as text"))
    for i, text in enumerate(letter.get("paragraphs") or [], 1):
        head = _squash(" ".join((text or "").split()[:8]))
        if head and head not in flat:
            problems.append(Problem("error", f"paragraphs.{i}", "paragraph text not found in the PDF"))
    return problems


def build_letter(source: dict, letter: dict, posting: dict, out_dir: Path) -> tuple[Path | None, list[Problem]]:
    """Render, compile and check. Writes cover_letter.tex always, and the PDF when it compiles."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tex = render_letter_tex(source, letter, posting)
    (out_dir / "cover_letter.tex").write_text(tex, encoding="utf-8")
    pdf = out_dir / letter_filename(source)
    ok, note = compile_pdf(tex, pdf)
    if not ok:
        return None, [Problem("error", "compile", note)]
    problems = check_letter_pdf(pdf, source, letter)
    if note:
        problems.append(Problem("warn", "layout", note))
    if any(p.level == "error" for p in problems):
        pdf.unlink()  # never leave a letter that failed its checks next to the CV
        return None, problems
    return pdf, problems
