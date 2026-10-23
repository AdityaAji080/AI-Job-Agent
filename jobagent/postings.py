"""Saved posting text (data/postings/), so tailoring works from what the agent judged.

When `add_jobs` records a pool candidate, its description is saved here. Jobs recorded
before this existed, or found by open search, can be re-read from their job board with
`refetch`. Saved text is the employer's own words and is treated as untrusted data by
everything downstream.

    python -m jobagent.postings URL [URL ...]   # re-read and save, e.g. for older tracker rows
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import sys
from datetime import date
from pathlib import Path

from . import config, store
from .ats import ADAPTERS, Board, Posting, detect_board
from .ats.base import make_client

_HEADER_KEYS = ("company", "role", "url", "source", "saved")


def posting_id(url: str) -> str:
    """Stable file id for a posting: the same job under a different URL spelling is one file."""
    return hashlib.sha1(store.job_key(url).encode()).hexdigest()[:16]


def path_for(url: str) -> Path:
    return config.DATA_DIR / "postings" / f"{posting_id(url)}.md"


def save(*, url: str, company: str, role: str, text: str, source: str = "") -> Path | None:
    """Write a posting. Returns None (and writes nothing) when there is no text to save."""
    text = (text or "").strip()
    if not text or not url:
        return None
    path = path_for(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {"company": company, "role": role, "url": url, "source": source,
              "saved": date.today().isoformat()}
    lines = [f"{k}: {' '.join(str(v).split())}" for k, v in header.items()]
    path.write_text("\n".join(lines) + "\n---\n\n" + text + "\n", encoding="utf-8")
    return path


def load(url: str) -> dict | None:
    """{'company', 'role', 'url', 'source', 'saved', 'text'} or None if never saved."""
    path = path_for(url)
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8")
    head, _, body = raw.partition("\n---\n")
    out = {k: "" for k in _HEADER_KEYS}
    for line in head.splitlines():
        key, _, value = line.partition(": ")
        if key in out:
            out[key] = value
    out["text"] = body.strip()
    return out


# ---------------------------------------------------------------- re-reading a posting

_MCF_UUID = re.compile(r"([0-9a-f]{32})/?$")


def _same_job(p: Posting, url: str) -> bool:
    """A board posting matches a URL by normalised URL, or by its job id appearing in it."""
    if store.job_key(p.url) == store.job_key(url):
        return True
    return bool(p.job_id) and len(p.job_id) >= 6 and p.job_id in url


async def _read_detail(client, adapter, posting: Posting) -> Posting:
    if posting.needs_detail and hasattr(adapter, "detail"):
        await adapter.detail(client, posting)
    return posting


async def _from_mcf(client, url: str) -> Posting | None:
    mcf = ADAPTERS["mycareersfuture"]
    m = _MCF_UUID.search(url.split("?")[0])
    if not m:
        return None
    from .ats.base import request_json
    data = await request_json(client, "GET", f"{mcf.API}/jobs/{m.group(1)}")
    posting = mcf._to_posting({**data, "uuid": data.get("uuid") or m.group(1)})
    if posting is None:
        return None
    mcf.parse_detail(data, posting)
    return posting


async def _from_workday(client, board: Board, url: str) -> Posting | None:
    """Workday boards are searched, not listed, so read the one posting by its path."""
    wd = ADAPTERS["workday"]
    host, site = board.slug.split("/", 1)
    path = url.split("?")[0].split(f"/{site}", 1)[-1]
    if not path.startswith("/job/"):
        return None
    posting = Posting(ats="workday", board_slug=board.slug, company=board.company or host,
                      job_id=path.rsplit("_", 1)[-1], title="", url=url, needs_detail=True,
                      extra={"path": path})
    await wd.detail(client, posting)
    return posting


_SR_POSTING = re.compile(r"^/([^/]+)/(\d+)(?:-[^/]*)?/?$")


async def _from_smartrecruiters(client, board: Board, url: str) -> Posting | None:
    """SmartRecruiters boards are read only up to 2,000 postings (some, like Bosch, have
    more), so a posting missing from that list proves nothing. Read it by its id: the
    posting's own address answers 404 once it closes."""
    from urllib.parse import urlsplit
    m = _SR_POSTING.match(urlsplit(url).path)
    if not m:
        return None
    sr = ADAPTERS["smartrecruiters"]
    posting = Posting(ats="smartrecruiters", board_slug=board.slug, company=board.company or board.slug,
                      job_id=m.group(2), title="", url=url, needs_detail=True)
    await sr.detail(client, posting)  # BoardError "404" when it has closed
    return posting


def _boards_for(url: str, company: str) -> list[Board]:
    """The board a URL is on, or failing that, registered boards for the same company
    (Greenhouse postings often live on the company's own domain, like ?gh_jid=...)."""
    board = detect_board(url, company)
    if board:
        return [board]
    from .registry import Registry
    want = store.company_key(company)
    return [e.board for e in Registry.load().active()
            if e.ats != "mycareersfuture" and want and store.company_key(e.company) == want]


OPEN, CLOSED, UNKNOWN = "open", "closed", "unknown"


async def check(url: str, company: str = "") -> tuple[str, Posting | None]:
    """Re-read one posting and say whether it is still open.

    CLOSED needs positive evidence: the board says so, the posting's own address
    answers 404, or the URL's own board was read in full and no longer lists it.
    Anything else that goes wrong (network trouble, a board found only by company
    name) is UNKNOWN, never CLOSED, so a flaky request can't retire a live job.
    """
    from .ats.base import BoardError
    async with make_client() as client:
        try:
            if "mycareersfuture.gov.sg" in url:
                p = await _from_mcf(client, url)
                return (CLOSED if p and p.extra.get("closed") else OPEN if p else UNKNOWN), p
        except BoardError as exc:
            return (CLOSED if "404" in str(exc) else UNKNOWN), None
        except Exception:
            return UNKNOWN, None
        direct = detect_board(url, company) is not None
        for board in _boards_for(url, company):
            adapter = ADAPTERS.get(board.ats)
            if adapter is None:
                continue
            try:
                if direct and board.ats in ("workday", "smartrecruiters"):
                    read_one = _from_workday if board.ats == "workday" else _from_smartrecruiters
                    p = await read_one(client, board, url)
                    if p:
                        return (CLOSED if p.extra.get("closed") else OPEN), p
                    continue
                for p in await adapter.fetch(client, board):
                    if _same_job(p, url):
                        return OPEN, await _read_detail(client, adapter, p)
                if direct:  # the posting's own board, read in full, no longer has it
                    return CLOSED, None
            except BoardError as exc:
                # A 404 is evidence only from the posting's own board. A board found by
                # company name may simply have moved; try the next one.
                if direct:
                    return (CLOSED if "404" in str(exc) else UNKNOWN), None
            except Exception:
                if direct:
                    return UNKNOWN, None
    return UNKNOWN, None


async def refetch(url: str, company: str = "") -> Posting | None:
    """Re-read one posting from its job board. None when it can't be read or has closed."""
    status, posting = await check(url, company)
    return posting if status == OPEN else None


async def ensure(url: str, company: str = "", role: str = "") -> dict | None:
    """The saved posting for a URL, re-reading and saving it first if needed."""
    have = load(url)
    if have and have["text"]:
        return have
    p = await refetch(url, company)
    if not p or not p.description:
        return None
    save(url=url, company=company or p.company, role=role or p.title, text=p.description,
         source=f"{p.ats}:{p.board_slug}")
    return load(url)


def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="+")
    args = ap.parse_args()
    rows = {store.job_key(j["url"]): j for j in store.read_jobs()}
    missing = 0
    for url in args.urls:
        row = rows.get(store.job_key(url), {})
        got = asyncio.run(ensure(url, row.get("company", ""), row.get("role", "")))
        if got:
            print(f"saved   {path_for(url).name}  {got['company']}: {got['role']} ({len(got['text'])} chars)")
        else:
            missing += 1
            print(f"MISSING {url}  (not readable from its board; save the text by hand)")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(_main())
