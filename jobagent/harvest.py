"""Read every registered board and produce a ranked shortlist for the model.

Run directly to check boards without spending anything on the model:
    python -m jobagent.harvest                 # all boards in the registry
    python -m jobagent.harvest URL [URL ...]   # just these boards (not saved)
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import sys
from dataclasses import dataclass, field

from . import config, filters
from .ats import ADAPTERS, Board, BoardError, Posting
from .ats.base import make_client
from .store import job_key


@dataclass
class Candidate:
    id: str
    board_key: str
    company: str
    title: str
    location: str
    market: str
    posted: str
    posted_note: str
    age_days: int | None
    url: str
    apply_url: str
    compensation: str
    description: str
    experience_hints: list[str]
    phd_hint: str
    looks_stale: bool
    applicants: int | None = None
    note: str = ""

    def summary(self) -> dict:
        """The compact view the model scans; get_posting returns the full text."""
        out = {"id": self.id, "company": self.company, "title": self.title,
               "location": self.location, "market": self.market}
        if self.posted:
            out["posted"] = f"{self.posted} ({self.age_days}d ago)"
        elif self.posted_note:
            out["posted"] = self.posted_note
        if self.compensation:
            out["compensation"] = self.compensation
        if self.experience_hints:
            out["experience_mentions"] = self.experience_hints
        if self.phd_hint:
            out["phd_mention"] = self.phd_hint
        if self.applicants is not None:
            out["applicants_so_far"] = self.applicants
        if self.note:
            out["note"] = self.note
        if self.looks_stale:
            out["stale_check"] = f"posted >{config.STALE_DAYS} days ago and names no current/future year"
        if not self.description:
            out["description"] = "none from the source; open the url to read it"
        return out


@dataclass
class BoardResult:
    board: Board
    ok: bool
    postings: int = 0
    candidates: list[Candidate] = field(default_factory=list)
    error: str = ""
    dropped: dict = field(default_factory=dict)  # reason -> count, after reading full postings


def to_candidate(p: Posting, board: Board) -> Candidate:
    text = p.description or ""
    age = filters.age_days(p.posted)
    stale = (age is not None and age > config.STALE_DAYS
             and not filters.mentions_future_date(text + " " + p.title))
    return Candidate(
        id=p.id, board_key=board.key, company=p.company, title=p.title, location=p.location,
        market=filters.market_of(p.location, p.remote), posted=p.posted, posted_note=p.posted_note,
        age_days=age, url=p.url, apply_url=p.apply_url, compensation=p.compensation,
        description=text, experience_hints=filters.experience_hints(text),
        phd_hint=filters.phd_hint(text), looks_stale=stale,
        applicants=p.extra.get("applicants"), note=p.extra.get("note", ""),
    )


def prefilter(postings: list[Posting], known: set[str]) -> list[Posting]:
    keep = []
    for p in postings:
        if not p.url or job_key(p.url) in known:
            continue
        if not filters.title_ok(p.title):
            continue
        if filters.market_of(p.location, p.remote) == "Other":
            continue
        keep.append(p)
    return keep


async def harvest_board(client, board: Board, known: set[str], sem: asyncio.Semaphore) -> BoardResult:
    adapter = ADAPTERS.get(board.ats)
    if adapter is None:
        return BoardResult(board, ok=False, error=f"no adapter for '{board.ats}'")
    async with sem:
        try:
            postings = await adapter.fetch(client, board)
        except BoardError as exc:
            return BoardResult(board, ok=False, error=str(exc))
        except Exception as exc:  # a malformed payload shouldn't sink the whole run
            return BoardResult(board, ok=False, error=f"{type(exc).__name__}: {exc}"[:200])
    kept = prefilter(postings, known)

    async def enrich(p: Posting):
        if p.needs_detail and hasattr(adapter, "detail"):
            async with sem:
                try:
                    await adapter.detail(client, p)
                except Exception:
                    pass  # keep the posting; the model can open the URL itself
    await asyncio.gather(*(enrich(p) for p in kept))
    dropped = Counter(p.extra["drop"] for p in kept if p.extra.get("drop"))
    kept = [p for p in kept if not p.extra.get("closed") and not p.extra.get("drop")]
    return BoardResult(board, ok=True, postings=len(postings),
                       candidates=[to_candidate(p, board) for p in kept], dropped=dict(dropped))


async def harvest(boards: list[Board], known: set[str]) -> list[BoardResult]:
    sem = asyncio.Semaphore(config.CONCURRENCY)
    async with make_client() as client:
        return await asyncio.gather(*(harvest_board(client, b, known, sem) for b in boards))


def rank(candidates: list[Candidate]) -> list[Candidate]:
    """Market priority first, then freshest first, undated last within a market."""
    return sorted(candidates, key=lambda c: (filters.market_rank(c.market), c.looks_stale,
                                             c.age_days if c.age_days is not None else 10_000))


def _main() -> int:
    from .registry import Registry
    from .ats import detect_board
    from .store import known_keys

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="*", help="board or posting URLs to check instead of the registry")
    ap.add_argument("--show", type=int, default=5, help="candidates to print per board")
    args = ap.parse_args()

    if args.urls:
        boards = [b for b in (detect_board(u) for u in args.urls) if b]
        if len(boards) < len(args.urls):
            print("Some URLs aren't on a supported job board and were ignored.", file=sys.stderr)
        known = set()
    else:
        boards = [e.board for e in Registry.load().active()]
        known = known_keys()
    if not boards:
        print("No boards to check.")
        return 1

    results = asyncio.run(harvest(boards, known))
    failed = 0
    for r in results:
        if r.ok:
            print(f"OK    {r.board.key:<55} {r.postings:>4} postings, {len(r.candidates):>3} candidates")
            for c in rank(r.candidates)[: args.show]:
                print(f"        - [{c.market}] {c.title} | {c.location} | {c.posted or c.posted_note or 'undated'}"
                      f"{' | desc ' + str(len(c.description)) + ' chars' if c.description else ' | NO DESCRIPTION'}")
        else:
            failed += 1
            print(f"FAIL  {r.board.key:<55} {r.error}")
    total = sum(len(r.candidates) for r in results)
    print(f"\n{len(results) - failed}/{len(results)} boards read, {total} candidates.")
    return 0 if failed < len(results) else 1


if __name__ == "__main__":
    sys.exit(_main())
