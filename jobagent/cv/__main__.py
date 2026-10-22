"""Tailored CVs from the command line.

    python -m jobagent.cv tailor URL [URL ...]          # research, draft, review, render
    python -m jobagent.cv tailor URL --posting-file F    # when the job board can't be read
    python -m jobagent.cv tailor URL --letter            # always write a cover letter too
    python -m jobagent.cv batch --max 40                 # every untailored job, newest first
    python -m jobagent.cv apply-updates                  # write finished CV paths to jobs.csv
    python -m jobagent.cv baseline --out OUT_DIR         # your full CV, no model calls
    python -m jobagent.cv check TAILORED.json --out OUT_DIR

`tailor` needs ANTHROPIC_API_KEY. Exit status is 0 only if everything succeeded.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .. import config
from .content import baseline, check_content, load_source
from .render import build


def _print_problems(problems) -> int:
    for p in problems:
        print("   ", p)
    return sum(p.level == "error" for p in problems)


def _render(args, source) -> int:
    if args.cmd == "baseline":
        cv = baseline(source)
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
    errors = _print_problems(problems)
    print("PASS" if not errors else f"FAIL ({errors} error{'s' * (errors != 1)})")
    return 1 if errors else 0


def _tailor(args, source) -> int:
    import anthropic

    from .tailor import tailor_url

    if args.posting_file and len(args.urls) > 1:
        print("--posting-file works with one URL at a time.", file=sys.stderr)
        return 2
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Set ANTHROPIC_API_KEY first.", file=sys.stderr)
        return 2
    client = anthropic.Anthropic()
    failed, spent = 0, 0.0
    for url in args.urls:
        print(f"\n{url}")
        try:
            result = tailor_url(client, source, url, posting_file=args.posting_file,
                                company=args.company, role=args.role, force_letter=args.letter)
        except anthropic.APIError as exc:
            print(f"  API error: {exc}")
            failed += 1
            continue
        spent += result.cost
        _print_problems(result.problems)
        if result.ok:
            print(f"  OK  {result.pdf}  (~${result.cost:.2f})")
            if result.letter_pdf:
                print(f"      {result.letter_pdf}")
            elif result.letter_note:
                print(f"      cover letter: {result.letter_note}")
        else:
            failed += 1
            details = result.out_dir / "review.json"
            where = f"; details in {details}" if details.exists() else ""
            print(f"  FAILED: {result.error}  (~${result.cost:.2f}{where})")
    print(f"\n{len(args.urls) - failed} of {len(args.urls)} tailored, ~${spent:.2f} at standard prices.")
    return 1 if failed else 0


def _batch(args, source) -> int:
    import anthropic

    from . import batch

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Set ANTHROPIC_API_KEY first.", file=sys.stderr)
        return 2
    summary = batch.run(anthropic.Anthropic(), source, max_jobs=args.max,
                        deadline_seconds=args.deadline_minutes * 60, poll_seconds=args.poll_seconds)
    batch.write_step_summary(summary)
    print(f"\nTailored {summary['tailored']} ({summary.get('letters', 0)} with a cover letter), "
          f"failed {len(summary['failed'])}, "
          f"skipped {summary['closed']} closed, {summary['missing_text']} without posting text, "
          f"{summary['in_progress']} still in progress. ~${summary['cost_usd']:.2f} at batch prices.")
    for company, role, error in summary["failed"]:
        print(f"  failed: {company}, {role}: {error}")
    return 0  # failures are recorded per job; they shouldn't fail the workflow


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", type=Path, default=config.CV_SOURCE_PATH)
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("tailor", help="tailor the CV for one or more tracked postings")
    t.add_argument("urls", nargs="+")
    t.add_argument("--posting-file", type=Path, help="posting text, when its board can't be read")
    t.add_argument("--company", default="", help="when the URL isn't in the tracker")
    t.add_argument("--role", default="", help="when the URL isn't in the tracker")
    t.add_argument("--letter", action="store_true",
                   help="write a cover letter even if the posting doesn't ask for one")

    b = sub.add_parser("batch", help="tailor untailored tracker jobs through the Batch API")
    b.add_argument("--max", type=int, default=int(os.environ.get("MAX_TAILOR", "40")),
                   help="jobs to take on this run (default: MAX_TAILOR or 40)")
    b.add_argument("--deadline-minutes", type=float, default=300,
                   help="stop waiting after this long; a running batch is collected next time")
    b.add_argument("--poll-seconds", type=float, default=60)
    sub.add_parser("apply-updates", help="write finished CV paths into jobs.csv")

    for name in ("baseline", "check"):
        p = sub.add_parser(name, help="render without model calls")
        if name == "check":
            p.add_argument("tailored", help="tailored selection (JSON)")
        p.add_argument("--out", required=True, type=Path)
        p.add_argument("--max-pages", type=int, default=None,
                       help="overrides layout.max_pages in the source")

    args = ap.parse_args()
    if args.cmd == "apply-updates":
        from .batch import apply_updates
        print(f"Recorded {apply_updates()} result(s) in jobs.csv.")
        return 0
    if not args.source.exists():
        print(f"Missing {args.source}. Set JOBAGENT_HOME to your private folder.", file=sys.stderr)
        return 2
    source = load_source(args.source)
    if args.cmd == "batch":
        return _batch(args, source)
    return _tailor(args, source) if args.cmd == "tailor" else _render(args, source)


if __name__ == "__main__":
    sys.exit(main())
