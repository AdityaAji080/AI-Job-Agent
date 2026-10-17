"""Find a company's job board from its name, without web search.

Most job-board systems address a company's board by a short slug ("sarvam", "sarvam-ai").
Given company names, this tries the likely slugs on each system's public API and reports
the boards that exist and have open postings. Web search is unreliable for this: `site:`
queries mostly come back as aggregator pages.

Workday is not probed: its boards live on per-company hosts that can't be guessed.
"""
from __future__ import annotations

import asyncio
import re
import unicodedata

import httpx

from . import config
from .ats import ADAPTERS, Board

# (system, list URL template, public board URL template)
PROBES = [
    ("greenhouse", "https://boards-api.greenhouse.io/v1/boards/{s}/jobs", "https://job-boards.greenhouse.io/{s}"),
    ("lever", "https://api.lever.co/v0/postings/{s}?mode=json", "https://jobs.lever.co/{s}"),
    ("ashby", "https://api.ashbyhq.com/posting-api/job-board/{s}", "https://jobs.ashbyhq.com/{s}"),
    ("smartrecruiters", "https://api.smartrecruiters.com/v1/companies/{s}/postings?limit=100",
     "https://jobs.smartrecruiters.com/{s}"),
    ("workable", "https://apply.workable.com/api/v1/widget/accounts/{s}", "https://apply.workable.com/{s}/"),
    ("recruitee", "https://{s}.recruitee.com/api/offers/", "https://{s}.recruitee.com/"),
]

_SUFFIXES = {"ai", "labs", "lab", "inc", "technologies", "technology", "tech", "india", "pvt", "private",
             "ltd", "limited", "llc", "corp", "corporation", "co", "group", "software", "systems",
             "solutions", "global", "hq", "the", "singapore", "sg", "canada"}


def slug_variants(name: str, limit: int = 4) -> list[str]:
    """Likely board slugs for a company name: 'Sarvam AI' -> sarvamai, sarvam-ai, sarvam."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    text = text.replace("&", " and ")
    words = re.findall(r"[a-z0-9]+", text)
    if not words:
        return []
    core = [w for w in words if w not in _SUFFIXES] or words
    variants = ["".join(words), "-".join(words), "".join(core), "-".join(core)]
    if len(core[0]) >= 5:  # a lone short first word ("open", "deep") collides with unrelated boards
        variants.append(core[0])
    out: list[str] = []
    for v in variants:
        if v and v not in out:
            out.append(v)
    return out[:limit]


async def _probe_one(client: httpx.AsyncClient, sem: asyncio.Semaphore, name: str, system: str,
                     url_t: str, board_t: str, slug: str) -> dict | None:
    async with sem:
        try:
            resp = await client.get(url_t.format(s=slug), timeout=12.0)
        except httpx.HTTPError:
            return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
        postings = ADAPTERS[system].parse(data, Board(system, slug, name))
    except Exception:
        return None
    if not postings:
        return None
    board_name = data.get("name") if isinstance(data, dict) and isinstance(data.get("name"), str) else ""
    return {
        "company_queried": name,
        "system": system,
        "slug": slug,
        "board_url": board_t.format(s=slug),
        "board_name": board_name,  # only some systems report it
        "postings": len(postings),
        "sample": [f"{p.title} | {p.location}" for p in postings[:4]],
    }


async def probe(names: list[str], client: httpx.AsyncClient | None = None,
                known_keys: set[str] = frozenset()) -> dict:
    """Probe every system for every name. Returns {name: [hits]} plus names with no hit."""
    own = client is None
    client = client or httpx.AsyncClient(follow_redirects=True,
                                         headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"})
    sem = asyncio.Semaphore(16)
    tasks, already = [], {}
    try:
        for name in names:
            for system, url_t, board_t in PROBES:
                for slug in slug_variants(name):
                    if f"{system}:{slug}" in known_keys:
                        already.setdefault(name, []).append(f"{system}:{slug}")
                        continue
                    tasks.append(_probe_one(client, sem, name, system, url_t, board_t, slug))
        results = await asyncio.gather(*tasks)
    finally:
        if own:
            await client.aclose()

    found: dict[str, list[dict]] = {}
    for hit in filter(None, results):
        found.setdefault(hit["company_queried"], []).append(hit)
    for hits in found.values():  # a slug and its hyphenated twin can point at the same board
        seen, unique = set(), []
        for h in sorted(hits, key=lambda h: -h["postings"]):
            key = (h["system"], tuple(h["sample"]))
            if key not in seen:
                seen.add(key)
                unique.append(h)
        hits[:] = unique
    return {
        "found": found,
        "already_registered": already,
        "not_found": [n for n in names if n not in found and n not in already],
    }


if __name__ == "__main__":
    # python -m jobagent.discover "Sarvam AI" "Qure.ai" ...
    import json
    import sys
    print(json.dumps(asyncio.run(probe(sys.argv[1:])), indent=1))
