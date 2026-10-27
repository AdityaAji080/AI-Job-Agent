"""The tracker (jobs.csv) and the list of postings already judged (seen.csv)."""
from __future__ import annotations

import csv
import re
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from . import config

COLUMNS = ["date_added", "company", "role", "location", "market", "posted", "deadline_or_start",
           "compensation", "experience", "url", "why", "status", "applied", "cv", "cover_letter"]
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


# SmartRecruiters lists a posting as /<company>/<id> but its own page (and the tracker)
# has /<company>/<id>-<title-slug>. Both forms are the same job.
_SR_ID = re.compile(r"^(jobs\.smartrecruiters\.com/[^/?]+/\d+)-[^/?]*$")


def key_aliases(key: str) -> set[str]:
    """A job_key plus any other spelling of the same posting's key."""
    if not key:
        return set()
    m = _SR_ID.match(key)
    return {key, m.group(1)} if m else {key}


def _with_aliases(keys) -> set[str]:
    return {a for k in keys for a in key_aliases(k)}


_LEGAL = re.compile(r"\b(pte|private|ltd|limited|llp|llc|inc|corp|corporation|co|plc|gmbh|"
                   r"singapore|india|canada|technologies|technology)\b")


def company_key(company: str) -> str:
    """'CFGI Singapore Pte. Ltd.' and 'CFGI' compare equal."""
    text = re.sub(r"[^a-z0-9]+", " ", (company or "").lower())
    core = _LEGAL.sub(" ", text).split()
    return " ".join(core) or text.strip()


# Words that don't tell employers apart, for same_employer().
_GENERIC = {"holdings", "holding", "group", "operations", "asia", "pacific", "sea", "apac", "emea",
            "international", "global", "services", "solutions", "systems", "labs", "lab", "ai", "tech",
            "the", "and", "of", "company", "enterprises", "ventures", "digital", "software", "data",
            "research", "taxi", "health", "financial", "capital", "partners", "engineering", "robotics"}


def _distinct_words(company: str) -> set[str]:
    return {w for w in company_key(company).split() if len(w) >= 3 and w not in _GENERIC}


def same_employer(a: str, b: str) -> bool:
    """'Grab' and 'Grab Taxi Holdings', or 'Bosch' and 'Robert Bosch (SEA)': the same employer
    when one name's distinctive words all appear in the other's. 'Applied Intuition' and
    'Applied Materials' are not; nor are names with no distinctive word at all."""
    if company_key(a) == company_key(b):
        return bool(company_key(a))
    wa, wb = _distinct_words(a), _distinct_words(b)
    if not wa or not wb:
        return False
    return wa <= wb or wb <= wa


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


def update_job(url: str, **fields) -> bool:
    """Set columns on the tracker row for a URL. Everything else, your edits included, is kept.

    Written to a temporary file and swapped in, so an interrupted write can't truncate
    the tracker. Returns False when no row has that URL.
    """
    ensure_schema()
    path = config.JOBS_CSV
    rows = _read(path)
    key = job_key(url)
    hit = False
    for row in rows:
        if job_key(row.get("url", "")) == key:
            row.update(fields)
            hit = True
    if not hit:
        return False
    with open(path, newline="", encoding="utf-8") as f:
        header = next(csv.reader(f), []) or COLUMNS
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)
    return True


def read_jobs() -> list[dict]:
    return _read(config.JOBS_CSV)


def read_seen() -> list[dict]:
    return _read(config.SEEN_CSV)


def archive_csv() -> Path:
    """Jobs moved out of the tracker once applied to (see jobagent.archive)."""
    return config.DATA_DIR / "archive" / "applied.csv"


def read_archived() -> list[dict]:
    path = archive_csv()
    return _read(path) if path.exists() else []


def declined_csv() -> Path:
    """Jobs marked applied = no: moved out of the tracker, their CVs deleted."""
    return config.DATA_DIR / "archive" / "not_applied.csv"


def read_declined() -> list[dict]:
    path = declined_csv()
    return _read(path) if path.exists() else []


def _all_archived() -> list[dict]:
    """Both archives: a job applied to or turned down still counts as tracked."""
    return read_archived() + read_declined()


def tracked_role_keys() -> set[str]:
    """company+role+market of every job in the tracker or the archive: a repost of one under
    a new URL is the same job."""
    return {role_key(j.get("company", ""), j.get("role", "")) + "|" + (j.get("market") or "")
            for j in read_jobs() + _all_archived()}


def recently_recorded(days: int, today: date | None = None) -> dict[str, str]:
    """company_key -> the latest date a job of theirs was recorded, for companies recorded
    within the last `days` days (from seen.csv, the tracker and the archive)."""
    if days <= 0:
        return {}
    today = today or date.today()
    latest: dict[str, str] = {}
    dated = [(s.get("company", ""), s.get("date", "")) for s in read_seen() if s.get("decision") == "recorded"]
    dated += [(j.get("company", ""), j.get("date_added", "")) for j in read_jobs() + _all_archived()]
    for company, when in dated:
        try:
            age = (today - date.fromisoformat(when)).days
        except (TypeError, ValueError):
            continue
        key = company_key(company)
        if key and 0 <= age < days and when > latest.get(key, ""):
            latest[key] = when
    return latest


def known_keys() -> set[str]:
    """Every posting already tracked or already judged; the harvester skips these."""
    return _with_aliases({job_key(j.get("url", "")) for j in read_jobs()} | {s["key"] for s in read_seen()})


def append_jobs(jobs: list[dict]) -> dict:
    """Add jobs, rejecting duplicates by URL and by company+role+location."""
    ensure_schema()
    existing = read_jobs()
    keys = _with_aliases({job_key(j.get("url", "")) for j in existing + _all_archived()})
    roles = tracked_role_keys()
    added, rejected = [], []
    for job in jobs:
        key = job_key(job.get("url", ""))
        rk = role_key(job.get("company", ""), job.get("role", "")) + "|" + (job.get("market") or "")
        if not key:
            rejected.append({"role": job.get("role"), "reason": "no url"})
            continue
        if key_aliases(key) & keys or rk in roles:
            rejected.append({"role": job.get("role"), "reason": "already tracked"})
            continue
        keys |= key_aliases(key)
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
    have = _with_aliases(s["key"] for s in read_seen())
    rows = []
    for item in items:
        key = job_key(item.get("url", ""))
        if key and not key_aliases(key) & have:
            have |= key_aliases(key)
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
