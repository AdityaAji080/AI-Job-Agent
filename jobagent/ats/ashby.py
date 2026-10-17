"""Ashby: api.ashbyhq.com/posting-api/job-board/<name>?includeCompensation=true"""
from __future__ import annotations

from urllib.parse import urlsplit

from .base import Board, Posting, html_to_text, iso_date, request_json

NAME = "ashby"


def parse_url(url: str) -> str | None:
    parts = urlsplit(url)
    segs = [s for s in parts.path.split("/") if s]
    host = parts.netloc.lower()
    if host == "jobs.ashbyhq.com" and segs:
        return segs[0]
    if host == "api.ashbyhq.com" and len(segs) >= 3 and segs[:2] == ["posting-api", "job-board"]:
        return segs[2]
    return None


def parse(data: dict, board: Board) -> list[Posting]:
    out = []
    for job in data.get("jobs") or []:
        if job.get("isListed") is False:
            continue
        locs = [job.get("location") or ""]
        for sec in job.get("secondaryLocations") or []:
            locs.append(sec.get("location") if isinstance(sec, dict) else str(sec))
        comp = job.get("compensation") or {}
        out.append(Posting(
            ats=NAME, board_slug=board.slug, company=board.company or board.slug,
            job_id=str(job.get("id", "")),
            title=(job.get("title") or "").strip(),
            url=job.get("jobUrl") or f"https://jobs.ashbyhq.com/{board.slug}/{job.get('id', '')}",
            apply_url=job.get("applyUrl") or "",
            location=" / ".join(l for l in locs if l),
            posted=iso_date(job.get("publishedAt")),
            compensation=comp.get("scrapeableCompensationSalarySummary")
                         or comp.get("compensationTierSummary") or "",
            description=job.get("descriptionPlain") or html_to_text(job.get("descriptionHtml")),
            remote=job.get("isRemote"),
        ))
    return out


async def fetch(client, board: Board) -> list[Posting]:
    data = await request_json(client, "GET",
                              f"https://api.ashbyhq.com/posting-api/job-board/{board.slug}",
                              params={"includeCompensation": "true"})
    return parse(data, board)
