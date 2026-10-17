"""The board registry (boards.csv): every company job board the agent has found.

It starts from whatever you seed it with and grows on its own: whenever the agent
comes across a posting on a supported job board, that company's whole board is
added and harvested on every later run.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, fields
from datetime import date

from . import config
from .ats import Board, detect_board
from .ats import mycareersfuture

BUILTIN_SOURCES = [mycareersfuture.BOARD]


@dataclass
class Entry:
    company: str
    ats: str
    slug: str
    source: str = "manual"     # seed | discovered | manual
    added: str = ""
    enabled: str = "yes"
    last_checked: str = ""
    last_status: str = ""      # "ok" or the last error
    failures: str = "0"        # consecutive failed runs
    postings: str = "0"        # total postings at last check
    candidates_total: str = "0"  # postings that passed the prefilter, all runs
    recorded_total: str = "0"    # postings recorded in the tracker, all runs

    @property
    def board(self) -> Board:
        return Board(ats=self.ats, slug=self.slug, company=self.company)

    @property
    def key(self) -> str:
        return self.board.key


COLUMNS = [f.name for f in fields(Entry)]


class Registry:
    def __init__(self, entries: list[Entry]):
        self.entries = {e.key: e for e in entries}

    @classmethod
    def load(cls) -> "Registry":
        entries = []
        if config.BOARDS_CSV.exists():
            with open(config.BOARDS_CSV, newline="", encoding="utf-8") as f:
                entries = [Entry(**{k: v for k, v in row.items() if k in COLUMNS})
                           for row in csv.DictReader(f)]
        reg = cls(entries)
        for board in BUILTIN_SOURCES:  # search sources that aren't found by discovery
            reg.add(board, board.company, source="builtin")
        return reg

    def save(self) -> None:
        config.BOARDS_CSV.parent.mkdir(parents=True, exist_ok=True)
        rows = sorted(self.entries.values(), key=lambda e: (e.company.lower(), e.ats))
        with open(config.BOARDS_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(e.__dict__ for e in rows)

    def active(self) -> list[Entry]:
        return [e for e in self.entries.values() if e.enabled == "yes"]

    def add(self, board: Board, company: str = "", source: str = "discovered") -> tuple[Entry, bool]:
        """Add a board if new. Returns (entry, was_new)."""
        if board.key in self.entries:
            entry = self.entries[board.key]
            if company and (not entry.company or entry.company == entry.slug):
                entry.company = company
            return entry, False
        entry = Entry(company=company or board.company or board.slug, ats=board.ats, slug=board.slug,
                      source=source, added=date.today().isoformat())
        self.entries[entry.key] = entry
        return entry, True

    def add_url(self, url: str, company: str = "", source: str = "discovered") -> tuple[Entry | None, bool]:
        board = detect_board(url, company)
        return (None, False) if board is None else self.add(board, company, source)

    def record_check(self, key: str, *, ok: bool, status: str = "", postings: int = 0,
                     candidates: int = 0) -> None:
        e = self.entries.get(key)
        if not e:
            return
        e.last_checked = date.today().isoformat()
        if ok:
            e.last_status, e.failures, e.postings = "ok", "0", str(postings)
            e.candidates_total = str(int(e.candidates_total or 0) + candidates)
        else:
            e.last_status = status[:200]
            e.failures = str(int(e.failures or 0) + 1)
            if int(e.failures) >= config.MAX_BOARD_FAILURES:
                e.enabled = "no"

    def record_recorded(self, key: str, n: int = 1) -> None:
        if key in self.entries:
            e = self.entries[key]
            e.recorded_total = str(int(e.recorded_total or 0) + n)
