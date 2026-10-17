"""Registry of job-board adapters and URL detection.

Each adapter module exposes:
  NAME                        short name stored in boards.csv
  parse_url(url) -> slug|None recognise a board from any of its URLs
  parse(data, board)          pure: API payload -> list[Posting]   (unit-tested)
  fetch(client, board)        network: read the whole board
  detail(client, posting)     optional: fill in a description fetched separately
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from . import ashby, greenhouse, lever, recruitee, smartrecruiters, workable, workday
from .base import Board, BoardError, Posting  # noqa: F401  (re-exported)

ADAPTERS = {m.NAME: m for m in (greenhouse, lever, ashby, smartrecruiters, workable, recruitee, workday)}

# Systems we recognise but cannot read through a public API. Roles on these are
# still recordable; the agent opens them with WebFetch instead.
UNSUPPORTED = {
    "darwinbox": re.compile(r"\.darwinbox\.(in|com)$"),
    "keka": re.compile(r"\.keka\.com$"),
    "zoho recruit": re.compile(r"zohorecruit\.(com|in|eu)$|recruit\.zoho\."),
    "freshteam": re.compile(r"\.freshteam\.com$"),
    "icims": re.compile(r"\.icims\.com$"),
    "taleo": re.compile(r"\.taleo\.net$"),
    "successfactors": re.compile(r"successfactors\.(com|eu)$|\.sapsf\.(com|eu)$"),
    "oracle cloud": re.compile(r"\.oraclecloud\.com$"),
    "eightfold": re.compile(r"\.eightfold\.ai$"),
    "jobvite": re.compile(r"\.jobvite\.com$"),
    "bamboohr": re.compile(r"\.bamboohr\.com$"),
    "teamtailor": re.compile(r"\.teamtailor\.com$"),
    "personio": re.compile(r"\.jobs\.personio\.(de|com)$"),
}


def detect_board(url: str, company: str = "") -> Board | None:
    """Return the Board a URL belongs to, if it is on a system we can read."""
    url = (url or "").strip()
    if not url.startswith("http"):
        url = "https://" + url
    for name, mod in ADAPTERS.items():
        slug = mod.parse_url(url)
        if slug:
            return Board(ats=name, slug=slug, company=company)
    return None


def unsupported_system(url: str) -> str | None:
    host = urlsplit(url if url.startswith("http") else "https://" + url).netloc.lower()
    return next((name for name, rx in UNSUPPORTED.items() if rx.search(host)), None)
