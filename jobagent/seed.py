"""Add boards to the registry by hand.

    python -m jobagent.seed URL [URL ...] [--company NAME]   # any posting or board URL
    python -m jobagent.seed --from-tracker                   # every board behind jobs.csv
"""
from __future__ import annotations

import argparse
import sys

from .ats import unsupported_system
from .registry import Registry
from .store import read_jobs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="*")
    ap.add_argument("--company", default="", help="company name (single URL only)")
    ap.add_argument("--from-tracker", action="store_true")
    args = ap.parse_args()

    pairs = [(u, args.company) for u in args.urls]
    if args.from_tracker:
        pairs += [(j.get("url", ""), j.get("company", "")) for j in read_jobs()]

    reg = Registry.load()
    added, unsupported, other = [], {}, 0
    for url, company in pairs:
        entry, new = reg.add_url(url, company, source="seed")
        if entry and new:
            added.append(f"{entry.company} ({entry.ats}: {entry.slug})")
        elif entry is None:
            system = unsupported_system(url)
            if system:
                unsupported.setdefault(system, []).append(company or url)
            else:
                other += 1
    reg.save()
    print(f"Added {len(added)} boards:" if added else "No new boards.")
    for a in added:
        print("  +", a)
    for system, names in unsupported.items():
        print(f"Recognised but unsupported ({system}): {', '.join(sorted(set(names)))}")
    if other:
        print(f"{other} URLs weren't on a known job-board system (company sites, aggregators).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
