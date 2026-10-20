"""The CV source file, the tailored selection a model returns, and the rules between them.

The source (profile/cv_source.yaml) holds everything true about the candidate.
A tailored CV is only a *selection and rewording* of it:

    {
      "summary": "...",
      "skills": [{"label": "Languages", "items": ["Python", "C++"]}, ...],
      "experience": [{"id": "its", "bullets": [{"from": "gps", "text": "..."}]}, ...],
      "projects":   [{"id": "inc", "bullets": [{"from": "harness", "text": "..."}]}, ...],
      "coursework": ["Computer Vision", ...]
    }

Titles, employers, dates, contact details and education are never in it: the
renderer copies them from the source, so a model cannot get them wrong.
check_content() enforces the rest. It is pure, so every rule is unit-tested.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

# Limits that keep a two-year CV readable and inside its page budget.
SUMMARY_MAX_WORDS = 60
BULLET_MAX_WORDS = 42
MAX_BULLETS_PER_ENTRY = 6
MIN_BULLETS_PER_ROLE = 1


@dataclass
class Problem:
    level: str      # "error" blocks the CV; "warn" is shown but doesn't
    where: str      # e.g. "experience.its.gps"
    message: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.where}: {self.message}"


def load_source(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for section in ("experience", "projects"):
        for entry in data.get(section) or []:
            ids = [b["id"] for b in entry.get("bullets") or []]
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate bullet id in {section}.{entry['id']}")
    return data


def baseline(source: dict) -> dict:
    """The untailored CV: every source bullet, in source order, worded as written."""
    def entries(section):
        return [{"id": e["id"], "bullets": [{"from": b["id"], "text": b["text"]} for b in e["bullets"]]}
                for e in source.get(section) or []]
    return {
        "summary": source["summary"],
        "skills": [{"label": g["label"], "items": list(g["items"])} for g in source["skills"]],
        "experience": entries("experience"),
        "projects": entries("projects"),
        "coursework": [c for ed in source.get("education") or [] for c in ed.get("coursework") or []],
    }


# ---------------------------------------------------------------- the rules

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers(text: str) -> set[str]:
    """'$65K/week ($3.4M/yr)' -> {'65', '3.4'}; commas ignored so 20,000 == 20000."""
    return {n.replace(",", "") for n in _NUMBER.findall(text or "")}


def _words(text: str) -> int:
    return len((text or "").split())


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def check_content(source: dict, cv: dict) -> list[Problem]:
    problems: list[Problem] = []
    err = lambda where, msg: problems.append(Problem("error", where, msg))
    warn = lambda where, msg: problems.append(Problem("warn", where, msg))

    # The summary may use any number the source states anywhere, headers and dates included.
    every_source_number = numbers(yaml.safe_dump(
        {k: v for k, v in source.items() if k not in ("contact", "layout")}, allow_unicode=True))

    # Summary: rewritten freely, but no number the source doesn't contain anywhere.
    summary = cv.get("summary") or ""
    if not summary.strip():
        err("summary", "missing")
    if _words(summary) > SUMMARY_MAX_WORDS:
        err("summary", f"{_words(summary)} words; limit {SUMMARY_MAX_WORDS}")
    for n in sorted(numbers(summary) - every_source_number):
        err("summary", f"number '{n}' appears nowhere in the source")

    # Skills: may be regrouped and cut, never added to.
    known = {_norm(i) for g in source["skills"] for i in g["items"]}
    for g in cv.get("skills") or []:
        for item in g.get("items") or []:
            if _norm(item) not in known:
                err(f"skills.{g.get('label', '?')}", f"'{item}' is not in the source skills")

    # Experience and projects.
    for section, required in (("experience", True), ("projects", False)):
        src = {e["id"]: e for e in source.get(section) or []}
        got = cv.get(section) or []
        got_ids = [e.get("id") for e in got]
        for eid in got_ids:
            if eid not in src:
                err(section, f"unknown entry '{eid}'")
        if len(got_ids) != len(set(got_ids)):
            err(section, "an entry appears twice")
        if required:
            for eid in src:
                if eid not in got_ids:
                    err(f"{section}.{eid}", "every role must appear; dropping one leaves a gap in the dates")
        for entry in got:
            if entry.get("id") not in src:
                continue
            where = f"{section}.{entry['id']}"
            source_bullets = {b["id"]: b["text"] for b in src[entry["id"]]["bullets"]}
            # A bullet may use numbers from its own source bullet or its entry's header
            # ("NeurIPS 2026"), never from another bullet.
            header_numbers = numbers(" ".join(str(v) for k, v in src[entry["id"]].items()
                                             if k not in ("bullets", "id")))
            bullets = entry.get("bullets") or []
            if required and len(bullets) < MIN_BULLETS_PER_ROLE:
                err(where, "no bullets")
            if len(bullets) > MAX_BULLETS_PER_ENTRY:
                err(where, f"{len(bullets)} bullets; limit {MAX_BULLETS_PER_ENTRY}")
            used = [b.get("from") for b in bullets]
            if len(used) != len(set(used)):
                warn(where, "two bullets come from the same source bullet")
            for b in bullets:
                bid, text = b.get("from"), b.get("text") or ""
                if bid not in source_bullets:
                    err(f"{where}.{bid}", "does not name a source bullet")
                    continue
                for n in sorted(numbers(text) - numbers(source_bullets[bid]) - header_numbers):
                    err(f"{where}.{bid}", f"number '{n}' is not in the source bullet")
                if _words(text) > BULLET_MAX_WORDS:
                    err(f"{where}.{bid}", f"{_words(text)} words; limit {BULLET_MAX_WORDS}")
                if not text.strip():
                    err(f"{where}.{bid}", "empty")

    # Coursework: a subset, never invented.
    courses = {_norm(c) for ed in source.get("education") or [] for c in ed.get("coursework") or []}
    for c in cv.get("coursework") or []:
        if _norm(c) not in courses:
            err("coursework", f"'{c}' is not in the source")

    return problems
