"""Shared types and helpers for job-board adapters."""
from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import httpx

from .. import config


@dataclass(frozen=True)
class Board:
    """One company's job board on one applicant tracking system."""
    ats: str          # adapter name, e.g. "greenhouse"
    slug: str         # the board identifier that adapter understands
    company: str = ""

    @property
    def key(self) -> str:
        return f"{self.ats}:{self.slug.lower()}"


@dataclass
class Posting:
    """A job as read from a board, normalised across systems."""
    ats: str
    board_slug: str
    company: str
    job_id: str
    title: str
    url: str
    location: str = ""
    posted: str = ""            # ISO date, or "" when the board doesn't say
    posted_note: str = ""       # e.g. "30+ days ago" when only a rough age is known
    compensation: str = ""
    description: str = ""
    apply_url: str = ""
    remote: bool | None = None
    needs_detail: bool = False  # description must be fetched separately
    extra: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.ats}:{self.board_slug.lower()}:{self.job_id}"


class BoardError(Exception):
    """A board could not be read. The message is shown in run summaries."""


_BLOCK_TAGS = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6]|tr|section)[^>]*>", re.I)
_TAGS = re.compile(r"<[^>]+>")


def html_to_text(raw: str | None) -> str:
    """Turn (possibly double-escaped) HTML into readable plain text."""
    if not raw:
        return ""
    text = html.unescape(raw)
    if "&lt;" in text or "&amp;" in text:  # Greenhouse escapes its HTML once more
        text = html.unescape(text)
    text = _BLOCK_TAGS.sub("\n", text)
    text = _TAGS.sub("", text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def iso_date(value) -> str:
    """Best-effort conversion of a timestamp (ISO string or epoch ms) to YYYY-MM-DD."""
    if value in (None, ""):
        return ""
    try:
        if isinstance(value, (int, float)):
            secs = value / 1000 if value > 1e11 else value
            return datetime.fromtimestamp(secs, tz=timezone.utc).date().isoformat()
        text = str(value).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return text
        text = text.replace("Z", "+00:00")
        return datetime.fromisoformat(text).date().isoformat()
    except (ValueError, OverflowError, OSError):
        m = re.match(r"(\d{4}-\d{2}-\d{2})", str(value))
        return m.group(1) if m else ""


def money(lo, hi, currency="", interval="") -> str:
    """Format a salary range; empty when nothing is known."""
    if lo in (None, "") and hi in (None, ""):
        return ""
    def fmt(v):
        try:
            return f"{float(v):,.0f}"
        except (TypeError, ValueError):
            return str(v)
    span = fmt(lo) if hi in (None, "", lo) else (fmt(hi) if lo in (None, "") else f"{fmt(lo)}–{fmt(hi)}")
    parts = [currency.upper() if currency else "", span, f"per {interval.lower().replace('per-', '').replace('_', ' ')}" if interval else ""]
    return " ".join(p for p in parts if p).strip()


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=config.HTTP_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"},
    )


async def request_json(client: httpx.AsyncClient, method: str, url: str, **kwargs):
    """GET/POST returning parsed JSON, with two retries on transient failures."""
    last: Exception | None = None
    for attempt in range(3):
        try:
            resp = await client.request(method, url, **kwargs)
            if resp.status_code == 404:
                raise BoardError(f"not found (404) at {url}")
            if resp.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
            resp.raise_for_status()
            return resp.json()
        except BoardError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            last = exc
            await asyncio.sleep(1.5 * (attempt + 1))
    raise BoardError(f"{type(last).__name__}: {last}"[:200])


def today() -> date:
    return date.today()
