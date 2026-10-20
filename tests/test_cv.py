"""CV content rules, escaping and fitting, without a model. Rendering tests need pdflatex."""
import copy
import shutil
from pathlib import Path

import pytest

from jobagent.cv.content import baseline, check_content, load_source, numbers
from jobagent.cv.render import build, drop_one_bullet, render_tex, tex_escape

SOURCE = load_source(Path(__file__).parent.parent / "example" / "profile" / "cv_source.yaml")
needs_latex = pytest.mark.skipif(not shutil.which("pdflatex"), reason="pdflatex not installed")


def errors(cv):
    return [str(p) for p in check_content(SOURCE, cv) if p.level == "error"]


def tailored():
    return copy.deepcopy(baseline(SOURCE))


def test_numbers():
    assert numbers("$65K/week ($3.4M/yr), 50% to 86%") == {"65", "3.4", "50", "86"}
    assert numbers("20,000 images") == {"20000"}


def test_baseline_is_clean():
    assert errors(tailored()) == []


def test_invented_number_in_bullet():
    cv = tailored()
    cv["experience"][0]["bullets"][0]["text"] = "Built a pricing service handling 5K requests/second."
    assert any("number '5'" in e for e in errors(cv))


def test_number_from_another_bullet_is_not_allowed():
    cv = tailored()  # 14 belongs to the migration bullet, not the pricing one
    cv["experience"][0]["bullets"][0]["text"] = "Built a pricing service and 14 batch jobs."
    assert any("number '14'" in e for e in errors(cv))


def test_number_from_entry_header_is_allowed():
    cv = tailored()
    cv["projects"][0]["bullets"][0]["text"] = "In 2026, trained models on 3M historical deliveries."
    assert errors(cv) == []


def test_summary_may_use_any_source_number_but_no_new_ones():
    cv = tailored()
    cv["summary"] = "Engineer whose pricing service handles 2K requests/second; MSc 2026."
    assert errors(cv) == []
    cv["summary"] = "Engineer with 7 years of experience."
    assert any("number '7'" in e for e in errors(cv))


def test_invented_skill_coursework_entry_and_bullet():
    cv = tailored()
    cv["skills"][0]["items"].append("Rust")
    cv["coursework"].append("Quantum Computing")
    cv["projects"].append({"id": "hackathon", "bullets": [{"from": "x", "text": "Won."}]})
    cv["experience"][0]["bullets"].append({"from": "leadership", "text": "Led a team."})
    found = " ".join(errors(cv))
    for needle in ("'Rust'", "Quantum Computing", "unknown entry 'hackathon'", "leadership"):
        assert needle in found


def test_every_role_must_appear():
    cv = tailored()
    cv["experience"] = []
    assert any("every role must appear" in e for e in errors(cv))


def test_skills_may_be_regrouped_and_cut():
    cv = tailored()
    cv["skills"] = [{"label": "Backend", "items": ["go", "Kafka", "PostgreSQL"]}]
    assert errors(cv) == []


def test_length_limits():
    cv = tailored()
    cv["summary"] = "word " * 80
    cv["experience"][0]["bullets"][0]["text"] = "word " * 60
    found = " ".join(errors(cv))
    assert "summary" in found and "60 words" in found


def test_escape_neutralises_latex():
    assert tex_escape(r"\input{/etc/passwd} 50% & $5_x #1 ~^") == (
        r"\textbackslash{}input\{/etc/passwd\} 50\% \& \$5\_x \#1 "
        r"\textasciitilde{}\textasciicircum{}")


def test_rendered_tex_escapes_model_text_and_keeps_source_facts():
    cv = tailored()
    cv["experience"][0]["bullets"][0]["text"] = r"Shipped \input{secret} at 2K requests/second."
    tex = render_tex(SOURCE, cv)
    assert r"\input{secret}" not in tex
    assert r"\textbackslash{}input\{secret\}" in tex
    assert "Example Logistics" in tex and "2022 – 2025" in tex  # headers come from the source


def test_roles_render_in_source_order_whatever_the_model_returns():
    cv = tailored()
    cv["projects"] = []
    tex = render_tex(SOURCE, cv)
    assert "\\section{Projects}" not in tex


def test_drop_one_bullet_takes_the_last_bullet_of_the_longest_entry():
    cv = tailored()
    assert drop_one_bullet(cv) == "experience.logistics.oncall"
    assert drop_one_bullet(cv) == "projects.thesis.calib"   # the role is at its floor of two
    assert drop_one_bullet(cv) is None                      # roles keep two, projects one


@needs_latex
def test_build_baseline(tmp_path):
    pdf, problems = build(SOURCE, tailored(), tmp_path)
    assert pdf and pdf.exists()
    assert [str(p) for p in problems if p.level == "error"] == []


@needs_latex
def test_build_fits_by_dropping_bullets(tmp_path):
    cv = tailored()
    long = "Built an event-driven pricing service handling 2K requests/second, " * 3
    for i in range(40):  # roughly three pages of text, whatever the layout
        cv["experience"][0]["bullets"].append({"from": "pricing", "text": f"{long}({i})."})
    pdf, problems = build(SOURCE, cv, tmp_path, max_pages=1)
    assert pdf is not None
    assert any(p.where == "fit" for p in problems)
    assert not any(p.where == "layout" and "pages" in p.message for p in problems)
