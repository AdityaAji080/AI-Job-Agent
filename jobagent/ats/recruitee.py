"""Recruitee: <company>.recruitee.com/api/offers/"""
from __future__ import annotations

from urllib.parse import urlsplit

from .base import Board, Posting, html_to_text, iso_date, money, request_json

NAME = "recruitee"


def parse_url(url: str) -> str | None:
    host = urlsplit(url).netloc.lower()
    if host.endswith(".recruitee.com") and host.count(".") == 2 and not host.startswith("www."):
        return host.split(".")[0]
    return None


def parse(data: dict, board: Board) -> list[Posting]:
    out = []
    for job in data.get("offers") or []:
        if job.get("status") and job["status"] != "published":
            continue
        sal = job.get("salary") or {}
        location = job.get("location") or ", ".join(
            x for x in (job.get("city"), job.get("country")) if x)
        if job.get("remote") and "remote" not in location.lower():
            location = f"{location} (remote)".strip()
        out.append(Posting(
            ats=NAME, board_slug=board.slug, company=board.company or job.get("company_name") or board.slug,
            job_id=str(job.get("id", "")),
            title=(job.get("title") or "").strip(),
            url=job.get("careers_url") or f"https://{board.slug}.recruitee.com/o/{job.get('slug', '')}",
            apply_url=job.get("careers_apply_url") or "",
            location=location,
            posted=iso_date(job.get("published_at") or job.get("created_at")),
            compensation=money(sal.get("min"), sal.get("max"), sal.get("currency", ""),
                               sal.get("period", "")) if isinstance(sal, dict) else "",
            description="\n\n".join(x for x in (html_to_text(job.get("description")),
                                                html_to_text(job.get("requirements"))) if x),
            remote=job.get("remote"),
        ))
    return out


async def fetch(client, board: Board) -> list[Posting]:
    data = await request_json(client, "GET", f"https://{board.slug}.recruitee.com/api/offers/")
    return parse(data, board)
