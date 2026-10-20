"""Turn a source + tailored selection into a PDF, then check the PDF itself.

The checks read the compiled PDF's text layer, which is what an applicant-tracking
system reads, so they catch what the .tex can't show: overflow onto an extra page,
a near-empty last page, contact details that didn't survive, text that was clipped.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import jinja2
from pypdf import PdfReader

from .content import Problem

TEMPLATE_DIR = Path(__file__).parent
MAX_PAGES = 2
# A last page with less text than this share of the first page reads as a spill-over.
MIN_LAST_PAGE_FILL = 0.25

_TEX_SPECIAL = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
    "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    "<": r"\textless{}", ">": r"\textgreater{}",
}
_TEX_RX = re.compile("|".join(re.escape(k) for k in _TEX_SPECIAL))


def tex_escape(value) -> str:
    """Escape everything, so no model-written text can act as a LaTeX command."""
    if value is None:
        return ""
    return _TEX_RX.sub(lambda m: _TEX_SPECIAL[m.group(0)], str(value))


def _env() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATE_DIR),
        block_start_string="((*", block_end_string="*))",
        variable_start_string="(((", variable_end_string=")))",
        comment_start_string="((#", comment_end_string="#))",
        finalize=tex_escape, undefined=jinja2.StrictUndefined,
        trim_blocks=True, lstrip_blocks=True, autoescape=False,
    )


def context(source: dict, cv: dict) -> dict:
    """Merge: headers and facts from the source, chosen and reworded text from the CV."""
    c = source["contact"]
    contact = [c.get(k) for k in ("location", "phone", "email", "linkedin", "github") if c.get(k)]

    def merged(section, chronological):
        src = {e["id"]: e for e in source.get(section) or []}
        chosen = {e["id"]: e for e in cv.get(section) or [] if e.get("id") in src}
        # Jobs always run newest first, as in the source; projects follow the model's priority.
        order = [e["id"] for e in source.get(section) or []] if chronological else list(chosen)
        return [{**{k: v for k, v in src[i].items() if k != "bullets"},
                 "bullets": [b["text"] for b in chosen[i]["bullets"]]}
                for i in order if i in chosen]

    wanted = {c.lower() for c in cv.get("coursework") or []}
    education = [{**ed, "coursework": [c for c in ed.get("coursework") or [] if c.lower() in wanted]}
                 for ed in source.get("education") or []]
    first = "projects" if cv.get("section_order") == "projects_first" else "experience"
    return {"name": source["name"],
            "headline": (cv.get("headline") or "").strip() or source.get("headline", ""),
            "sections": [first, "projects" if first == "experience" else "experience"],
            "contact_line": " | ".join(contact),
            "summary": cv["summary"], "skills": [g for g in cv["skills"] if g.get("items")],
            "experience": merged("experience", True), "projects": merged("projects", False),
            "education": education}


def render_tex(source: dict, cv: dict) -> str:
    return _env().get_template("template.tex").render(**context(source, cv))


def compile_pdf(tex: str, out_pdf: Path) -> tuple[bool, str]:
    """pdflatex twice in a scratch folder; only the PDF is kept."""
    if not shutil.which("pdflatex"):
        return False, "pdflatex is not installed"
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "cv.tex").write_text(tex, encoding="utf-8")
        for _ in range(2):
            run = subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "cv.tex"],
                                 cwd=tmp, capture_output=True, text=True, timeout=120,
                                 # LaTeX logs aren't in Windows' default code page.
                                 encoding="utf-8", errors="replace")
            if run.returncode != 0:
                errors = [l for l in run.stdout.splitlines() if l.startswith("!")]
                return False, "; ".join(errors[:3]) or run.stdout[-500:]
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(Path(tmp) / "cv.pdf", out_pdf)
        overfull = len(re.findall(r"Overfull \\hbox \((\d+\.\d+)pt", run.stdout))
        return True, f"{overfull} overfull lines" if overfull else ""


def _squash(s: str) -> str:
    """Compare text the way a parser sees it: letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def check_pdf(pdf: Path, source: dict, cv: dict, max_pages: int = MAX_PAGES) -> list[Problem]:
    problems: list[Problem] = []
    pages = [p.extract_text() or "" for p in PdfReader(str(pdf)).pages]
    text = "\n".join(pages)
    flat = _squash(text)

    if len(pages) > max_pages:
        problems.append(Problem("error", "layout", f"{len(pages)} pages; limit {max_pages}"))
    if len(pages) > 1 and len(pages[-1]) < MIN_LAST_PAGE_FILL * len(pages[0]):
        share = len(pages[-1]) / max(1, len(pages[0]))
        problems.append(Problem("warn", "layout", f"last page is only {share:.0%} full; trim to "
                                                  f"{len(pages) - 1} page(s) or add content"))
    if "(cid:" in text or "�" in text:
        problems.append(Problem("error", "text layer", "unreadable glyphs in the extracted text"))

    contact = source["contact"]
    for key in ("email", "phone"):
        if contact.get(key) and _squash(contact[key]) not in flat:
            problems.append(Problem("error", "text layer", f"{key} is not readable as text"))
    if _squash(source["name"]) not in flat:
        problems.append(Problem("error", "text layer", "name is not readable as text"))

    # Every bullet must be findable in order, which proves nothing was clipped or reordered.
    for section in ("experience", "projects"):
        for entry in cv.get(section) or []:
            for b in entry.get("bullets") or []:
                head = _squash(" ".join(b["text"].split()[:8]))
                if head and head not in flat:
                    problems.append(Problem("error", f"{section}.{entry['id']}.{b['from']}",
                                            "bullet text not found in the PDF"))
    return problems


def page_count(pdf: Path) -> int:
    return len(PdfReader(str(pdf)).pages)


def drop_one_bullet(cv: dict, min_per_role: int = 2) -> str | None:
    """Remove the least important bullet and return where it came from.

    The model lists bullets most-relevant first, so the last bullet of an entry is
    its weakest. Projects give way before roles, and a role keeps at least
    `min_per_role` bullets. Returns None when nothing more can go.
    """
    options = []
    for rank, section in enumerate(("projects", "experience")):
        floor = 1 if section == "projects" else min_per_role
        for entry in cv.get(section) or []:
            if len(entry.get("bullets") or []) > floor:
                options.append((len(entry["bullets"]), -rank, section, entry))
    if not options:
        return None
    _, _, section, entry = max(options, key=lambda o: (o[0], o[1]))
    gone = entry["bullets"].pop()
    return f"{section}.{entry['id']}.{gone['from']}"


def build(source: dict, cv: dict, out_dir: Path, max_pages: int | None = None,
          fit: bool = True) -> tuple[Path | None, list[Problem]]:
    """Render, compile and check. Writes cv.tex always and cv.pdf when it compiles.

    With `fit`, a CV that runs past its page limit loses its least important bullets
    one at a time until it fits; each removal is reported, never silent.
    """
    max_pages = max_pages or (source.get("layout") or {}).get("max_pages") or MAX_PAGES
    out_dir.mkdir(parents=True, exist_ok=True)
    dropped: list[str] = []
    while True:
        tex = render_tex(source, cv)
        (out_dir / "cv.tex").write_text(tex, encoding="utf-8")
        ok, note = compile_pdf(tex, out_dir / "cv.pdf")
        if not ok:
            return None, [Problem("error", "compile", note)]
        if not fit or page_count(out_dir / "cv.pdf") <= max_pages:
            break
        gone = drop_one_bullet(cv)
        if gone is None:
            break
        dropped.append(gone)
    problems = check_pdf(out_dir / "cv.pdf", source, cv, max_pages)
    if dropped:
        problems.append(Problem("warn", "fit", f"dropped to fit {max_pages} page(s): {', '.join(dropped)}"))
    if note:
        problems.append(Problem("warn", "layout", note))
    return out_dir / "cv.pdf", problems
