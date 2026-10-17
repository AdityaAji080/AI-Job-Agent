"""MyCareersFuture, Singapore's national job portal.

Unlike the other adapters this is not one company's board: it is a keyword search
across every employer in Singapore. Employers must advertise here for at least 14
days before applying for an Employment Pass, so it reaches employers nobody would
think to search for, early in each posting's life.

The site runs on a JSON API that is used by other open tools but is not officially
documented. If its shape changes, the board shows as failed in the run summary.

    search: POST https://api.mycareersfuture.gov.sg/v2/search?limit=50&page=N  {"search": term}
    detail: GET  https://api.mycareersfuture.gov.sg/v2/jobs/<uuid>
"""
from __future__ import annotations

import re
from datetime import date

from .. import config
from .base import Board, BoardError, Posting, html_to_text, iso_date, money, request_json

NAME = "mycareersfuture"
API = "https://api.mycareersfuture.gov.sg/v2"
BOARD = Board(ats=NAME, slug="sg", company="MyCareersFuture (all Singapore employers)")

# Agencies by name: generic words, plus the large firms whose names don't say so.
_AGENCY = re.compile(
    r"recruit|staffing|manpower|personnel|headhunt|executive search|talent|\bhr\b|human resource|"
    r"placement|employment agency|outsourc|"
    r"randstad|adecco|kelly services|persolkelly|michael page|page personnel|robert walters|"
    r"robert half|\bhays\b|access people|ambition|\bgmp\b|kerry consulting|morgan mckinley|"
    r"charterhouse|scientec|good job creations|search index|achieve group|careerally|"
    r"idc technologies|aryan solutions|unizen",
    re.I)
# Singapore employment agencies must print their EA licence (e.g. "EA Licence 16C8209")
# and their recruiter's registration number (e.g. "R1872345") on every ad.
_EA_LICENCE = re.compile(
    r"\bEA\s*(licen[cs]e|lic\b|reg(istration)?\b)|"                       # "EA Licence", "EA Reg"
    r"(licen[cs]e|\blic\b\.?|\bEA\b)[^\n]{0,30}\b\d{2}C\d{4,5}\b|"          # "Licence No: 16C8209"
    r"(registration|reg\.?|personnel|recruiter|consultant)[^\n]{0,40}\bR\d{7}\b",  # "Reg No: R1872345"
    re.I)
_LEGAL_SUFFIX = re.compile(r"[\s,.]*(pte\.?\s*ltd\.?|pte\.?\s*limited|private limited|limited|ltd\.?|"
                           r"pte\.?|llp|inc\.?|\((singapore|s)\)|singapore branch|s'pore)\s*$", re.I)


def parse_url(url: str) -> str | None:
    return None  # a search source, not a company board; never registered from a URL


_COMMON_SHORT = {"bank", "group", "labs", "lab", "tech", "data", "asia", "care", "life", "link", "mind",
                 "soft", "core", "edge", "wave", "home", "food", "auto", "city", "next", "health", "one"}


def company_name(raw: str) -> str:
    """'CFGI SINGAPORE PTE. LTD.' -> 'Cfgi Singapore'. Legal suffixes go; shouting is softened."""
    name = (raw or "").strip()
    for _ in range(3):
        name = _LEGAL_SUFFIX.sub("", name).strip(" ,.")
    if name.upper() == name:
        # Short all-caps words are usually acronyms (CFGI, DBS, AI); keep those.
        name = " ".join(w if len(w) <= 4 and w.lower() not in _COMMON_SHORT else w.title()
                        for w in name.split())
    return name


def _location(job: dict) -> str:
    addr = job.get("address") or {}
    if addr.get("isOverseas"):
        return "Overseas (per MyCareersFuture)"
    places = [d.get("location") for d in addr.get("districts") or [] if d.get("location")]
    return "Singapore" + (f" ({', '.join(dict.fromkeys(places))})" if places else "")


def _salary(job: dict) -> str:
    sal = job.get("salary") or {}
    kind = ((sal.get("type") or {}).get("salaryType") or "").lower()
    period = {"monthly": "month", "annually": "year", "annual": "year", "yearly": "year",
              "hourly": "hour", "daily": "day"}.get(kind, kind or "month")
    return money(sal.get("minimum"), sal.get("maximum"), "SGD", period)


def _to_posting(job: dict) -> Posting | None:
    uuid = job.get("uuid")
    meta = job.get("metadata") or {}
    if not uuid:
        return None
    posted_by = company_name((job.get("postedCompany") or {}).get("name", ""))
    hiring = company_name((job.get("hiringCompany") or {}).get("name", "")) if job.get("hiringCompany") else ""
    p = Posting(
        ats=NAME, board_slug=BOARD.slug,
        company=hiring or posted_by or "Unknown employer",
        job_id=str(uuid),
        title=(job.get("title") or "").strip(),
        url=meta.get("jobDetailsUrl") or f"https://www.mycareersfuture.gov.sg/job/{uuid}",
        location=_location(job),
        posted=iso_date(meta.get("newPostingDate") or meta.get("createdAt")),
        compensation=_salary(job),
        needs_detail=True,
    )
    p.extra["posted_by"] = posted_by
    p.extra["names_employer"] = bool(hiring) and hiring.lower() != posted_by.lower()
    if p.extra["names_employer"]:
        p.extra["note"] = f"posted by {posted_by} on behalf of {hiring}"
    if _AGENCY.search(posted_by):
        p.extra["agency"] = True
    _apply_meta(p, job)
    return p


def _apply_meta(p: Posting, job: dict) -> None:
    meta = job.get("metadata") or {}
    for key in ("totalNumberJobApplication", "totalNumberOfJobApplication", "totalJobApplication"):
        if isinstance(meta.get(key), int):
            p.extra["applicants"] = meta[key]
            break
    if meta.get("expiryDate"):
        p.extra["closes"] = iso_date(meta["expiryDate"])
    yrs = job.get("minimumYearsExperience")
    if isinstance(yrs, (int, float)) and yrs > 0:
        p.extra["min_years"] = int(yrs)


def parse(data: dict, board: Board = BOARD) -> list[Posting]:
    out = []
    for job in data.get("results") or []:
        p = _to_posting(job)
        if p:
            out.append(p)
    return out


def parse_detail(data: dict, posting: Posting) -> None:
    posting.description = html_to_text(data.get("description"))
    _apply_meta(posting, data)
    if posting.extra.get("min_years"):
        # Stated as a sentence so the usual experience extraction picks it up.
        posting.description = (f"Minimum experience: {posting.extra['min_years']} years of experience "
                               f"(MyCareersFuture field).\n\n{posting.description}")
    if _EA_LICENCE.search(posting.description):
        posting.extra["agency"] = True
    if posting.extra.get("agency") and not posting.extra.get("names_employer"):
        # An agency ad that doesn't say who the employer is: out of scope by the criteria,
        # and not an ad the employer could use for an Employment Pass anyway.
        posting.extra["drop"] = "recruitment agency, employer not named"
    status = ((data.get("status") or {}).get("jobStatus") or "").lower()
    if status and status not in ("open", "re-open", "reopen"):
        posting.extra["closed"] = True
    if posting.extra.get("closes") and posting.extra["closes"] < date.today().isoformat():
        posting.extra["closed"] = True
    posting.needs_detail = False


async def fetch(client, board: Board = BOARD) -> list[Posting]:
    found: dict[str, Posting] = {}
    failures = 0
    for term in config.MCF_SEARCH_TERMS:
        for page in range(config.MCF_PAGES_PER_TERM):
            try:
                data = await request_json(client, "POST", f"{API}/search",
                                          params={"limit": 50, "page": page}, json={"search": term})
            except BoardError:
                failures += 1
                break
            batch = parse(data)
            for p in batch:
                age = (date.today() - date.fromisoformat(p.posted)).days if p.posted else 0
                if age <= config.MCF_MAX_AGE_DAYS:
                    found.setdefault(p.job_id, p)
            if len(batch) < 50:
                break
    if failures == len(config.MCF_SEARCH_TERMS):
        raise BoardError("every MyCareersFuture search failed")
    return list(found.values())


async def detail(client, posting: Posting) -> None:
    parse_detail(await request_json(client, "GET", f"{API}/jobs/{posting.job_id}"), posting)
