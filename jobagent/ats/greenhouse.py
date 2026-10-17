"""Greenhouse: boards-api.greenhouse.io/v1/boards/<token>/jobs?content=true"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from .base import Board, Posting, html_to_text, iso_date, request_json

NAME = "greenhouse"
_HOST = re.compile(r"^(job-boards|boards)(\.eu)?\.greenhouse\.io$|^boards-api\.greenhouse\.io$")


def parse_url(url: str) -> str | None:
    parts = urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.")
    if not _HOST.match(host):
        return None
    segs = [s for s in parts.path.split("/") if s]
    if host.startswith("boards-api"):          # /v1/boards/<token>/...
        return segs[2] if len(segs) >= 3 and segs[:2] == ["v1", "boards"] else None
    if segs[:1] == ["embed"]:                   # /embed/job_board?for=<token>
        return (parse_qs(parts.query).get("for") or [None])[0]
    return segs[0] if segs else None


def parse(data: dict, board: Board) -> list[Posting]:
    out = []
    for job in data.get("jobs") or []:
        loc = (job.get("location") or {}).get("name", "") or ", ".join(
            o.get("name", "") for o in job.get("offices") or [] if o.get("name"))
        out.append(Posting(
            ats=NAME, board_slug=board.slug, company=board.company or board.slug,
            job_id=str(job.get("id", "")),
            title=(job.get("title") or "").strip(),
            url=job.get("absolute_url") or "",
            location=loc,
            posted=iso_date(job.get("first_published") or job.get("updated_at")),
            posted_note="" if job.get("first_published") else "last updated, not first posted",
            description=html_to_text(job.get("content")),
        ))
    return out


async def fetch(client, board: Board) -> list[Posting]:
    data = await request_json(client, "GET",
                              f"https://boards-api.greenhouse.io/v1/boards/{board.slug}/jobs",
                              params={"content": "true"})
    return parse(data, board)
