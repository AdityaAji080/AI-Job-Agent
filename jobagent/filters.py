"""Cheap, deterministic filtering done before the model sees anything.

The goal is to drop obvious non-matches (wrong country, sales roles, internships)
without ever dropping something the model might want. When in doubt, keep.
"""
from __future__ import annotations

import re
from datetime import date

from . import config

MARKET_ORDER = [name for name, _ in config.MARKETS] + ["Remote (unspecified)", "Unknown"]


def market_of(location: str, remote: bool | None = None) -> str:
    """Highest-priority market a location string mentions, or Other/Unknown."""
    loc = location or ""
    for name, pattern in config.MARKETS:
        if pattern.search(loc):
            return name
    if config.OTHER_PLACES.search(loc):
        return "Other"
    if remote or config.REMOTE.search(loc):
        return "Remote (unspecified)"
    return "Unknown"


def market_rank(market: str) -> int:
    return MARKET_ORDER.index(market) if market in MARKET_ORDER else len(MARKET_ORDER)


def title_ok(title: str) -> bool:
    return bool(config.TITLE_INCLUDE.search(title or "")) and not config.TITLE_EXCLUDE.search(title or "")


_YEARS = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*(\d{1,2})\s*\+?)?\s*(?:years?|yrs?)", re.I)
_PHD = re.compile(r"\bph\.?\s?d\b|doctoral|doctorate", re.I)


_SENTENCE = re.compile(r"[^.;!?\n]+")
_EXPERIENCE_WORDS = re.compile(r"experience|industry|professional|working|track record", re.I)


def experience_hints(text: str, limit: int = 3) -> list[str]:
    """The sentences that state an 'N years ... experience' requirement."""
    hints = []
    for sentence in _SENTENCE.findall(text or ""):
        if _YEARS.search(sentence) and _EXPERIENCE_WORDS.search(sentence):
            snippet = " ".join(sentence.split())[:220]
            if snippet not in hints:
                hints.append(snippet)
        if len(hints) >= limit:
            break
    return hints


def phd_hint(text: str) -> str:
    m = _PHD.search(text or "")
    if not m:
        return ""
    return " ".join(text[max(0, m.start() - 80): m.end() + 80].split())


def age_days(posted: str, today: date | None = None) -> int | None:
    try:
        return ((today or date.today()) - date.fromisoformat(posted)).days
    except (TypeError, ValueError):
        return None


_FUTURE_YEAR = re.compile(r"\b(20\d\d)\b")
_TIMING_WORDS = re.compile(
    r"start|intake|join(ing)?\b|deadline|apply by|applications? close|cohort|class of|"
    r"graduat|batch|programme|program\b|commenc|onboard", re.I)


def mentions_future_date(text: str, today: date | None = None) -> bool:
    """True if a sentence about timing (start, intake, deadline, cohort) names this year or later.

    A bare year is not enough: company boilerplate ("Forbes AI 50, 2026", "© 2026")
    would otherwise make every old evergreen posting look current.
    """
    year = (today or date.today()).year
    for sentence in _SENTENCE.findall(text or ""):
        if _TIMING_WORDS.search(sentence) and any(int(y) >= year for y in _FUTURE_YEAR.findall(sentence)):
            return True
    return False
