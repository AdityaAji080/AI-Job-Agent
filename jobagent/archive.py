"""Move applied-to jobs out of the tracker and into data/archive/.

Mark a job applied in jobs.csv (the `applied` column: yes, y, x, done ...) and the next
search run moves it:

    data/archive/applied.csv               the tracker row, plus the date it was archived
    data/archive/<company>_<role>_<id>/    the CV, cover letter and review.json, plus
                                           posting.md: the posting as it was when judged

Postings usually vanish once a job closes, so posting.md is what to reread before an
interview. The job stays in seen.csv, so it is never recorded again; its company and role
also count for the duplicate check, so a repost under a new URL isn't recorded either.

    python -m jobagent.archive      # archive now, without a search run
"""
from __future__ import annotations

import csv
import shutil
import sys
from datetime import date
from pathlib import Path

from . import config, postings, store

APPLIED = {"yes", "y", "true", "1", "x", "applied", "done"}
EXTRA_COLUMNS = ["archived", "folder"]


def archive_dir() -> Path:
    return store.archive_csv().parent


def archive_csv() -> Path:
    return store.archive_csv()


def is_applied(row: dict) -> bool:
    return (row.get("applied") or "").strip().lower() in APPLIED


read_archived = store.read_archived


def _application_folder(row: dict) -> Path | None:
    """The job's application folder, from its cv or cover_letter path."""
    for col in ("cv", "cover_letter"):
        value = (row.get(col) or "").strip()
        if value.startswith("applications/"):
            folder = (config.DATA_DIR / value).parent
            if folder.is_dir():
                return folder
    return None


def _free(path: Path) -> Path:
    """`path`, or `path-2`, `path-3`... if it is taken."""
    if not path.exists():
        return path
    n = 2
    while path.with_name(f"{path.name}-{n}").exists():
        n += 1
    return path.with_name(f"{path.name}-{n}")


def _moved(value: str, old: Path, new: Path) -> str:
    """Point a cv/cover_letter path at the folder's new home."""
    rel_old = str(old.relative_to(config.DATA_DIR))
    if value.startswith(rel_old + "/"):
        return str(new.relative_to(config.DATA_DIR)) + value[len(rel_old):]
    return value


def archive_applied(today: date | None = None) -> list[dict]:
    """Archive every row marked applied. Returns the archived rows (with their new paths)."""
    from .cv.tailor import out_dir_for

    _sweep_late_arrivals()
    rows = store.read_jobs()
    done = [r for r in rows if is_applied(r)]
    if not done:
        return []
    archive_dir().mkdir(parents=True, exist_ok=True)
    archived = []
    for row in done:
        folder = _application_folder(row)
        if folder is not None:
            dest = _free(archive_dir() / folder.name)
            shutil.move(str(folder), str(dest))
            for col in ("cv", "cover_letter"):
                row[col] = _moved(row.get(col) or "", folder, dest)
        else:  # never tailored, or the folder is already gone: keep the posting anyway
            dest = _free(archive_dir() / out_dir_for(row).name)
            dest.mkdir(parents=True)
        saved = postings.path_for(row.get("url", "")) if row.get("url") else None
        if saved is not None and saved.exists():
            shutil.copy(saved, dest / "posting.md")
        row["archived"] = (today or date.today()).isoformat()
        row["folder"] = str(dest.relative_to(config.DATA_DIR))
        archived.append(row)

    _append_archive(archived)
    _rewrite_tracker([r for r in rows if not is_applied(r)])
    return archived


def _sweep_late_arrivals() -> None:
    """A CV batch that was running when its job was archived finishes into applications/;
    move what it wrote into the job's archive folder."""
    from .cv.tailor import out_dir_for

    for row in read_archived():
        late = config.DATA_DIR / "applications" / out_dir_for(row).name
        home = config.DATA_DIR / (row.get("folder") or "")
        if not late.is_dir() or not row.get("folder") or late == home:
            continue
        home.mkdir(parents=True, exist_ok=True)
        for item in late.iterdir():
            target = home / item.name
            if not target.exists():
                shutil.move(str(item), str(target))
        shutil.rmtree(late, ignore_errors=True)


def _append_archive(rows: list[dict]) -> None:
    path = archive_csv()
    existing = read_archived()
    header = list(store.COLUMNS) + EXTRA_COLUMNS
    for r in existing + rows:  # keep any columns you added yourself
        header += [c for c in r if c and c not in header]
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(existing + rows)
    tmp.replace(path)


def _rewrite_tracker(rows: list[dict]) -> None:
    path = config.JOBS_CSV
    with open(path, newline="", encoding="utf-8") as f:
        header = next(csv.reader(f), []) or store.COLUMNS
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def main() -> int:
    archived = archive_applied()
    for r in archived:
        print(f"archived  {r.get('company')}: {r.get('role')}  ->  {r['folder']}")
    print(f"{len(archived)} job(s) archived.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
