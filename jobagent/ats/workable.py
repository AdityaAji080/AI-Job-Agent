"""Workable: apply.workable.com/api/v1/widget/accounts/<slug>?details=true"""
from __future__ import annotations

from urllib.parse import urlsplit

from .base import Board, Posting, html_to_text, iso_date, request_json

NAME = "workable"


def parse_url(url: str) -> str | None:
    parts = urlsplit(url)
    host = parts.netloc.lower()
    segs = [s for s in parts.path.split("/") if s]
    if host == "apply.workable.com" and segs and segs[0] not in ("api", "j"):
        return segs[0]
    if host.endswith(".workable.com") and host not in ("apply.workable.com", "www.workable.com",
                                                       "jobs.workable.com", "help.workable.com"):
        return host.split(".")[0]
    return None


def parse(data: dict, board: Board) -> list[Posting]:
    out = []
    for job in data.get("jobs") or []:
        locs = []
        for loc in job.get("locations") or []:
            locs.append(", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x))
        if not locs:
            locs = [", ".join(x for x in (job.get("city"), job.get("state"), job.get("country")) if x)]
        remote = job.get("telecommuting")
        location = " / ".join(l for l in locs if l)
        if remote and "remote" not in location.lower():
            location = f"{location} (remote)".strip()
        code = job.get("shortcode") or job.get("code") or ""
        out.append(Posting(
            ats=NAME, board_slug=board.slug, company=board.company or data.get("name") or board.slug,
            job_id=str(code),
            title=(job.get("title") or "").strip(),
            url=job.get("url") or job.get("shortlink") or f"https://apply.workable.com/{board.slug}/j/{code}/",
            apply_url=job.get("application_url") or "",
            location=location,
            posted=iso_date(job.get("published_on") or job.get("created_at")),
            description=html_to_text(job.get("description")),
            remote=remote,
        ))
    return out


async def fetch(client, board: Board) -> list[Posting]:
    data = await request_json(client, "GET",
                              f"https://apply.workable.com/api/v1/widget/accounts/{board.slug}",
                              params={"details": "true"})
    return parse(data, board)
