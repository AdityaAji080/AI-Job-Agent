"""Lever: api.lever.co/v0/postings/<site>?mode=json (api.eu.lever.co for EU boards)"""
from __future__ import annotations

from urllib.parse import urlsplit

from .base import Board, Posting, html_to_text, iso_date, money, request_json

NAME = "lever"


def parse_url(url: str) -> str | None:
    parts = urlsplit(url)
    host = parts.netloc.lower()
    segs = [s for s in parts.path.split("/") if s]
    if host in ("jobs.lever.co", "jobs.eu.lever.co") and segs:
        return ("eu/" if ".eu." in host else "") + segs[0]
    if host in ("api.lever.co", "api.eu.lever.co") and len(segs) >= 3 and segs[1] == "postings":
        return ("eu/" if ".eu." in host else "") + segs[2]
    return None


def _split(slug: str) -> tuple[str, str]:
    return ("api.eu.lever.co", slug[3:]) if slug.startswith("eu/") else ("api.lever.co", slug)


def parse(data: list, board: Board) -> list[Posting]:
    out = []
    for job in data or []:
        cats = job.get("categories") or {}
        locs = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
        sal = job.get("salaryRange") or {}
        sections = [job.get("descriptionPlain") or html_to_text(job.get("description"))]
        for block in job.get("lists") or []:
            sections.append(f"{block.get('text', '')}\n{html_to_text(block.get('content'))}")
        sections.append(job.get("additionalPlain") or html_to_text(job.get("additional")))
        out.append(Posting(
            ats=NAME, board_slug=board.slug, company=board.company or _split(board.slug)[1],
            job_id=str(job.get("id", "")),
            title=(job.get("text") or "").strip(),
            url=job.get("hostedUrl") or "",
            apply_url=job.get("applyUrl") or "",
            location=" / ".join(locs),
            posted=iso_date(job.get("createdAt")),
            compensation=money(sal.get("min"), sal.get("max"), sal.get("currency", ""),
                               sal.get("interval", "")) or job.get("salaryDescriptionPlain", "") or "",
            description="\n\n".join(s for s in sections if s).strip(),
            remote=(job.get("workplaceType") == "remote") if job.get("workplaceType") else None,
        ))
    return out


async def fetch(client, board: Board) -> list[Posting]:
    host, site = _split(board.slug)
    data = await request_json(client, "GET", f"https://{host}/v0/postings/{site}",
                              params={"mode": "json"})
    return parse(data, board)
