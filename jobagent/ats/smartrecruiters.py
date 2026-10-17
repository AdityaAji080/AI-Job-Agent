"""SmartRecruiters: api.smartrecruiters.com/v1/companies/<id>/postings (paged), details per posting."""
from __future__ import annotations

from urllib.parse import urlsplit

from .base import Board, Posting, html_to_text, iso_date, request_json

NAME = "smartrecruiters"
API = "https://api.smartrecruiters.com/v1/companies"


def parse_url(url: str) -> str | None:
    parts = urlsplit(url)
    host = parts.netloc.lower()
    segs = [s for s in parts.path.split("/") if s]
    if host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com") and segs:
        return segs[0]
    if host == "api.smartrecruiters.com" and len(segs) >= 3 and segs[:2] == ["v1", "companies"]:
        return segs[2]
    return None


def _location(loc: dict) -> str:
    if not loc:
        return ""
    if loc.get("fullLocation"):
        text = loc["fullLocation"]
    else:
        text = ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)
    return f"{text} (remote)" if loc.get("remote") and "remote" not in text.lower() else text


def parse(data: dict, board: Board) -> list[Posting]:
    out = []
    for job in data.get("content") or []:
        jid = str(job.get("id", ""))
        out.append(Posting(
            ats=NAME, board_slug=board.slug, company=board.company or board.slug,
            job_id=jid,
            title=(job.get("name") or "").strip(),
            url=f"https://jobs.smartrecruiters.com/{board.slug}/{jid}",
            location=_location(job.get("location") or {}),
            posted=iso_date(job.get("releasedDate")),
            remote=(job.get("location") or {}).get("remote"),
            needs_detail=True,
        ))
    return out


def parse_detail(data: dict, posting: Posting) -> None:
    sections = ((data.get("jobAd") or {}).get("sections") or {})
    parts = []
    for key in ("jobDescription", "qualifications", "additionalInformation"):
        sec = sections.get(key) or {}
        if sec.get("text"):
            parts.append(f"{sec.get('title') or key}\n{html_to_text(sec['text'])}")
    posting.description = "\n\n".join(parts)
    posting.url = data.get("postingUrl") or posting.url
    posting.apply_url = data.get("applyUrl") or posting.apply_url
    posting.needs_detail = False


async def fetch(client, board: Board) -> list[Posting]:
    out, offset = [], 0
    while True:
        data = await request_json(client, "GET", f"{API}/{board.slug}/postings",
                                  params={"limit": 100, "offset": offset})
        batch = parse(data, board)
        out.extend(batch)
        offset += 100
        if not batch or offset >= int(data.get("totalFound") or 0) or offset >= 2000:
            return out


async def detail(client, posting: Posting) -> None:
    data = await request_json(client, "GET", f"{API}/{posting.board_slug}/postings/{posting.job_id}")
    parse_detail(data, posting)
