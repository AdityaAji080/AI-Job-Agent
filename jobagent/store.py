"""The tracker (jobs.csv) and the list of postings already judged (seen.csv)."""
from __future__ import annotations

import csv
import re
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from . import config

COLUMNS = ["date_added", "company", "role", "location", "market", "posted", "deadline_or_start",
           "compensation", "experience", "url", "why", "status", "applied"]
SEEN_COLUMNS = ["key", "decision", "reason", "company", "role", "date"]

# Query parameters that identify a job (everything else, like utm_*, is noise).
_ID_PARAMS = {"gh_jid", "jid", "jobid", "job_id", "id", "jobreqid", "req", "reqid", "pid", "positionid"}


def job_key(url: str) -> str:
    """Normalised identity of a posting URL: host + path + any job-id parameter."""
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url if "://" in url else "https://" + url)
    host = parts.netloc.lower().removeprefix("www.")
    path = re.sub(r"/(apply|application)/?$", "", parts.path.lower().rstrip("/"))
    path = re.sub(r"^/[a-z]{2}-[a-z]{2}(?=/)", "", path)  # Workday locale prefixes
    ids = sorted((k.lower(), v) for k, v in parse_qsl(parts.query) if k.lower() in _ID_PARAMS)
    return host + path + ("?" + "&".join(f"{k}={v}" for k, v in ids) if ids else "")


_LEGAL = re.compile(r"\b(pte|private|ltd|limited|llp|llc|inc|corp|corporation|co|plc|gmbh|"
                   r"singapore|india|canada|technologies|technology)\b")


def company_key(company: str) -> str:
    """'CFGI Singapore Pte. Ltd.' and 'CFGI' compare equal."""
    text = re.sub(r"[^a-z0-9]+", " ", (company or "").lower())
    core = _LEGAL.sub(" ", text).split()
    return " ".join(core) or text.strip()


def role_key(company: str, role: str) -> str:
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    return f"{company_key(company)}|{norm(role)}"


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _append(path: Path, columns: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0  # an emptied file counts as new
    if not new:  # follow the file's own header, so columns you added stay aligned
        with open(path, newline="", encoding="utf-8") as f:
            columns = next(csv.reader(f), None) or columns
    with open(path, "w" if new else "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerows(rows)


def ensure_schema() -> None:
    """Rewrite jobs.csv with the current columns if it was written by an older version.

    Existing values (including your status and applied edits) are kept; new columns start empty.
    """
    path = config.JOBS_CSV
    if not path.exists() or path.stat().st_size == 0:
        return  # _append writes a fresh header
    with open(path, newline="", encoding="utf-8") as f:
        header = next(csv.reader(f), [])
    if header == COLUMNS:
        return
    rows = _read(path)
    extra = [c for c in header if c and c not in COLUMNS]  # keep any columns you added yourself
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS + extra, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_jobs() -> list[dict]:
    return _read(config.JOBS_CSV)


def read_seen() -> list[dict]:
    return _read(config.SEEN_CSV)


def known_keys() -> set[str]:
    """Every posting already tracked or already judged; the harvester skips these."""
    return {job_key(j.get("url", "")) for j in read_jobs()} | {s["key"] for s in read_seen()}


def append_jobs(jobs: list[dict]) -> dict:
    """Add jobs, rejecting duplicates by URL and by company+role+location."""
    ensure_schema()
    existing = read_jobs()
    keys = {job_key(j.get("url", "")) for j in existing}
    roles = {role_key(j.get("company", ""), j.get("role", "")) + "|" + (j.get("market") or "")
             for j in existing}
    added, rejected = [], []
    for job in jobs:
        key = job_key(job.get("url", ""))
        rk = role_key(job.get("company", ""), job.get("role", "")) + "|" + (job.get("market") or "")
        if not key:
            rejected.append({"role": job.get("role"), "reason": "no url"})
            continue
        if key in keys or rk in roles:
            rejected.append({"role": job.get("role"), "reason": "already tracked"})
            continue
        keys.add(key)
        roles.add(rk)
        row = {c: job.get(c, "") for c in COLUMNS}
        row.update(date_added=date.today().isoformat(), status=job.get("status") or "new")
        added.append(row)
    if added:
        _append(config.JOBS_CSV, COLUMNS, added)
        mark_seen([{"url": j["url"], "decision": "recorded", "company": j["company"],
                    "role": j["role"]} for j in added])
    return {"added": len(added), "rejected": rejected}


def mark_seen(items: list[dict]) -> int:
    """Remember judged postings so later runs don't spend tokens on them again."""
    have = {s["key"] for s in read_seen()}
    rows = []
    for item in items:
        key = job_key(item.get("url", ""))
        if key and key not in have:
            have.add(key)
            rows.append({"key": key, "decision": item.get("decision", "skipped"),
                         "reason": (item.get("reason") or "")[:300], "company": item.get("company", ""),
                         "role": item.get("role", ""), "date": date.today().isoformat()})
    if rows:
        _append(config.SEEN_CSV, SEEN_COLUMNS, rows)
    return len(rows)


def migrate_jobs_csv(src: Path, dst: Path) -> int:
    """Copy an old-format tracker (no market column) into the current format."""
    from .filters import market_of
    rows = _read(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            row.setdefault("market", market_of(row.get("location", "")))
            if not row.get("market"):
                row["market"] = market_of(row.get("location", ""))
            writer.writerow(row)
    return len(rows)
