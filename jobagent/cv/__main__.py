"""Render and check a CV without any model calls.

    python -m jobagent.cv baseline --out OUT_DIR          # every source bullet, as written
    python -m jobagent.cv check TAILORED.json --out OUT_DIR

Exit 0 when there are no errors (warnings are printed), 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import config
from .content import baseline, check_content, load_source
from .render import build


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["baseline", "check"])
    ap.add_argument("tailored", nargs="?", help="tailored selection (JSON), for 'check'")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--source", type=Path, default=config.CV_SOURCE_PATH)
    ap.add_argument("--max-pages", type=int, default=None, help="overrides layout.max_pages in the source")
    args = ap.parse_args()

    source = load_source(args.source)
    if args.what == "baseline":
        cv = baseline(source)
    elif not args.tailored:
        ap.error("check needs a tailored JSON file")
    else:
        cv = json.loads(Path(args.tailored).read_text(encoding="utf-8"))

    problems = check_content(source, cv)
    if any(p.level == "error" for p in problems):
        print("Content check failed; not rendering.")
    else:
        pdf, more = build(source, cv, args.out, args.max_pages)
        problems += more
        if pdf:
            print(f"Rendered {pdf}")
    for p in problems:
        print(" ", p)
    errors = sum(p.level == "error" for p in problems)
    print("PASS" if not errors else f"FAIL ({errors} error{'s' * (errors != 1)})")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
