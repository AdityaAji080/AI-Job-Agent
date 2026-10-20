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


def test_section_order_and_project_order():
    cv = tailored()
    cv["projects"] = [{"id": "thesis", "bullets": cv["projects"][0]["bullets"]}]
    tex = render_tex(SOURCE, cv)
    assert tex.index("\\section{Experience}") < tex.index("\\section{Projects}")
    cv["section_order"] = "projects_first"
    tex = render_tex(SOURCE, cv)
    assert tex.index("\\section{Projects}") < tex.index("\\section{Experience}")
    cv["section_order"] = "skills_first"
    assert any("section_order" in e for e in errors(cv))


def test_projects_follow_the_models_order_but_jobs_stay_chronological():
    source = copy.deepcopy(SOURCE)
    source["projects"].append({"id": "side", "title": "Side Project", "venue": "", "dates": "2024",
                               "bullets": [{"id": "x", "text": "Wrote a parser."}]})
    source["experience"].append({"id": "older", "title": "Intern", "org": "Old Co", "location": "",
                                 "dates": "2020", "bullets": [{"id": "y", "text": "Fixed bugs."}]})
    cv = baseline(source)
    cv["projects"].reverse()
    cv["experience"].reverse()
    tex = render_tex(source, cv)
    assert tex.index("Side Project") < tex.index("Uncertainty Estimates")   # model's order
    assert tex.index("Example Logistics") < tex.index("Old Co")             # source order


def test_headline_must_be_an_allowed_option():
    source = copy.deepcopy(SOURCE)
    source["headline"] = "Software Engineer"
    source["headline_options"] = ["Machine Learning Engineer"]
    cv = baseline(source)
    assert "Software Engineer" in render_tex(source, cv)          # empty choice keeps the default
    cv["headline"] = "machine learning engineer"
    assert check_content(source, cv) == []
    assert "machine learning engineer" in render_tex(source, cv)
    cv["headline"] = "Chief AI Officer"
    assert any("headline" in str(p) for p in check_content(source, cv))


def test_pdf_is_named_after_the_candidate():
    from jobagent.cv.render import pdf_filename
    assert pdf_filename(SOURCE) == "Jane_Example_CV.pdf"
    assert pdf_filename({"name": "Zoë O'Brien-Smith"}) == "Zoe_O_Brien_Smith_CV.pdf"
    assert pdf_filename({"name": ""}) == "CV.pdf"


@needs_latex
def test_rebuilding_replaces_an_old_cv_pdf(tmp_path):
    (tmp_path / "cv.pdf").write_text("old")
    pdf, _ = build(SOURCE, tailored(), tmp_path)
    assert pdf.name == "Jane_Example_CV.pdf"
    assert sorted(p.name for p in tmp_path.glob("*.pdf")) == ["Jane_Example_CV.pdf"]
