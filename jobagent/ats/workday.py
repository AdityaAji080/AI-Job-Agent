"""Workday: POST https://<tenant>.wdN.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs

Workday tenants at large employers list thousands of roles, so instead of paging
through everything we run a handful of keyword searches (config.WORKDAY_SEARCH_TERMS).
The board slug is "<host>/<site>", e.g. "nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite".
"""
from __future__ import annotations

import re
from datetime import timedelta
from urllib.parse import urlsplit

from .. import config
from .base import Board, BoardError, Posting, html_to_text, iso_date, request_json, today

NAME = "workday"
_LOCALE = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")


def parse_url(url: str) -> str | None:
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if not re.fullmatch(r"[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com", host):
        return None
    segs = [s for s in parts.path.split("/") if s]
    if segs[:2] == ["wday", "cxs"] and len(segs) >= 4:
        return f"{host}/{segs[3]}"
    segs = [s for s in segs if not _LOCALE.match(s)]
    return f"{host}/{segs[0]}" if segs else None


def _split(slug: str) -> tuple[str, str, str]:
    host, site = slug.split("/", 1)
    return host, host.split(".")[0], site


def posted_on(text: str) -> tuple[str, str]:
    """'Posted 3 Days Ago' -> (date, ''); 'Posted 30+ Days Ago' -> ('', '30+ days ago')."""
    t = (text or "").lower()
    if "today" in t:
        return today().isoformat(), ""
    if "yesterday" in t:
        return (today() - timedelta(days=1)).isoformat(), ""
    m = re.search(r"(\d+)\s*\+", t)
    if m:
        return "", f"{m.group(1)}+ days ago"
    m = re.search(r"(\d+)\s*day", t)
    if m:
        return (today() - timedelta(days=int(m.group(1)))).isoformat(), ""
    return "", ""


def parse(data: dict, board: Board) -> list[Posting]:
    host, _, site = _split(board.slug)
    out = []
    for job in data.get("jobPostings") or []:
        path = job.get("externalPath") or ""
        if not path:
            continue
        posted, note = posted_on(job.get("postedOn", ""))
        req = next((b for b in job.get("bulletFields") or [] if re.match(r"^[A-Z]*-?\d", str(b))), "")
        out.append(Posting(
            ats=NAME, board_slug=board.slug, company=board.company or host.split(".")[0],
            job_id=req or path.rsplit("/", 1)[-1],
            title=(job.get("title") or "").strip(),
            url=f"https://{host}/{site}{path}",
            location=job.get("locationsText") or "",
            posted=posted, posted_note=note,
            needs_detail=True,
            extra={"path": path},
        ))
    return out


def parse_detail(data: dict, posting: Posting) -> None:
    info = data.get("jobPostingInfo") or {}
    posting.description = html_to_text(info.get("jobDescription"))
    if info.get("location"):
        extra = info.get("additionalLocations") or []
        posting.location = " / ".join([info["location"], *extra])
    if info.get("startDate") and not posting.posted:
        # On public Workday boards "startDate" appears to be the posting date, not the
        # job's start date. Verify against a few live postings if dates look wrong.
        posting.posted = iso_date(info["startDate"])
        posting.posted_note = ""
    if info.get("externalUrl"):
        posting.url = info["externalUrl"]
    if info.get("canApply") is False:
        posting.extra["closed"] = True
    posting.needs_detail = False


async def fetch(client, board: Board) -> list[Posting]:
    host, tenant, site = _split(board.slug)
    endpoint = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    found: dict[str, Posting] = {}
    errors = 0
    for term in config.WORKDAY_SEARCH_TERMS:
        offset = 0
        while offset < config.WORKDAY_MAX_PER_TERM:
            try:
                data = await request_json(client, "POST", endpoint, json={
                    "appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term})
            except BoardError:
                errors += 1
                break
            batch = parse(data, board)
            for p in batch:
                found.setdefault(p.extra["path"], p)
            offset += 20
            if len(batch) < 20 or offset >= int(data.get("total") or 0):
                break
    if errors == len(config.WORKDAY_SEARCH_TERMS):
        raise BoardError(f"every Workday search failed at {endpoint}")
    return list(found.values())


async def detail(client, posting: Posting) -> None:
    host, tenant, site = _split(posting.board_slug)
    data = await request_json(client, "GET",
                              f"https://{host}/wday/cxs/{tenant}/{site}{posting.extra['path']}")
    parse_detail(data, posting)
