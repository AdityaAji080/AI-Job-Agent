"""One run: harvest every registered board, then have Claude judge, discover and record.

    python -m jobagent.agent              # full run
    python -m jobagent.agent --dry-run    # harvest only, print the pool, no model calls
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from collections import Counter
from datetime import date

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock,
                              create_sdk_mcp_server, query, tool)

from . import config, filters, store
from .ats import detect_board, unsupported_system
from .ats.base import make_client
from .discover import probe
from .harvest import Candidate, BoardResult, harvest, harvest_board, rank
from .prompt import KICKOFF, SYSTEM_PROMPT
from .registry import Registry


class Run:
    """State shared by the tools during one run."""

    def __init__(self, registry: Registry):
        self.registry = registry
        self.pool: dict[str, Candidate] = {}
        self.decided: set[str] = set()
        self.recorded = 0
        self.skipped = Counter()
        self.recorded_by_market = Counter()
        self.recorded_by_company = Counter()
        self.by_role: dict[str, str] = {}
        self.registered: list[str] = []
        self.board_results: list[BoardResult] = []
        self.probed = 0
        self.harvest_text = ""  # snapshot of the harvest, taken before the model starts

    def absorb(self, results: list[BoardResult]) -> None:
        """Add harvested candidates, keeping one copy of a job listed in several places.

        A role on both a company's own board and MyCareersFuture is kept as the board
        version (fuller detail); a role already in the tracker is dropped.
        """
        tracked = {store.role_key(j.get("company", ""), j.get("role", "")) + "|" + (j.get("market") or "")
                   for j in store.read_jobs()}
        for r in results:
            self.board_results.append(r)
            self.registry.record_check(r.board.key, ok=r.ok, status=r.error,
                                       postings=r.postings, candidates=len(r.candidates))
            for c in r.candidates:
                rk = store.role_key(c.company, c.title) + "|" + c.market
                if rk in tracked or c.id in self.pool:
                    continue
                other = self.by_role.get(rk)
                if other and other in self.pool:
                    if self.pool[other].board_key.startswith("mycareersfuture") and \
                            not c.board_key.startswith("mycareersfuture"):
                        del self.pool[other]
                    else:
                        continue
                self.pool[c.id] = c
                self.by_role[rk] = c.id

    def open_candidates(self, market: str = "") -> list[Candidate]:
        return rank([c for c in self.pool.values() if c.id not in self.decided
                     and (not market or c.market.lower() == market.lower())])

    def market_counts(self) -> Counter:
        return Counter(c.market for c in self.open_candidates())


def text_result(payload) -> dict:
    body = payload if isinstance(payload, str) else json.dumps(payload, indent=1, ensure_ascii=False)
    return {"content": [{"type": "text", "text": body}]}


ADD_JOBS_SCHEMA = {
    "type": "object",
    "properties": {"jobs": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "candidate_id": {"type": "string",
                             "description": "The pool id, when the job came from get_candidates or "
                                            "register_board. Missing fields are filled from the pool."},
            "company": {"type": "string"},
            "role": {"type": "string", "description": "Exact job title as posted"},
            "location": {"type": "string", "description": "City, or 'Remote'"},
            "posted": {"type": "string", "description": "Date the listing says it was posted. "
                                                        "Empty if not stated; never guess."},
            "deadline_or_start": {"type": "string", "description": "Labelled, e.g. 'deadline 2026-11-30' "
                                                                   "or 'starts Jan 2027'. Empty if neither."},
            "compensation": {"type": "string", "description": "Salary range if stated, else empty"},
            "experience": {"type": "string", "description": "Stated experience requirement in short form, "
                                                            "e.g. '5–10 yrs', '3+ yrs preferred'. Empty if "
                                                            "not stated. For pool candidates it is filled "
                                                            "from the posting if you leave it empty."},
            "url": {"type": "string", "description": "Direct link to the posting; only URLs you opened "
                                                     "or that came from the pool"},
            "why": {"type": "string", "description": "One sentence on why this fits, naming any gap"},
        },
        "required": ["why"],
    }}},
    "required": ["jobs"],
}

SKIP_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"candidate_id": {"type": "string"},
                       "reason": {"type": "string", "description": "Which skip-list criterion, briefly"}},
        "required": ["candidate_id", "reason"],
    }}},
    "required": ["items"],
}


def build_tools(run: Run):
    @tool("get_candidates", "List undecided candidates from the harvested pool for one market, best "
          "first. Markets: India, Singapore, Canada, Remote (unspecified), Unknown, United States. "
          "Optionally only those from one source (e.g. 'mycareersfuture').",
          {"type": "object", "properties": {
              "market": {"type": "string"},
              "source": {"type": "string", "description": "Optional: an adapter name, e.g. 'mycareersfuture'"},
              "offset": {"type": "integer", "default": 0},
              "limit": {"type": "integer", "default": 15}},
           "required": ["market"]})
    async def get_candidates(args):
        items = run.open_candidates(args["market"])
        if args.get("source"):
            items = [c for c in items if c.board_key.startswith(args["source"].lower() + ":")]
        off, lim = int(args.get("offset") or 0), min(int(args.get("limit") or 15), 30)
        return text_result({"market": args["market"], "total_open": len(items), "offset": off,
                            "candidates": [c.summary() for c in items[off: off + lim]],
                            "other_markets_open": dict(run.market_counts())})

    @tool("get_posting", "Full text of a pool candidate's posting.",
          {"type": "object", "properties": {"candidate_id": {"type": "string"}},
           "required": ["candidate_id"]})
    async def get_posting(args):
        c = run.pool.get(args["candidate_id"])
        if not c:
            return text_result("No such candidate id.")
        return text_result({"id": c.id, "company": c.company, "title": c.title,
                            "location": c.location, "posted": c.posted or c.posted_note,
                            "compensation": c.compensation, "url": c.url, "apply_url": c.apply_url,
                            "description": c.description[: config.DESCRIPTION_CHARS]
                            or "(The board API gave no description. Open the url with WebFetch.)"})

    @tool("add_jobs", "Record jobs that fit in the tracker. Duplicates are rejected automatically. "
          f"Refuses anything past this run's limit, United States jobs past {config.MAX_US_JOBS}, "
          f"and more than {config.MAX_PER_COMPANY} from one company.", ADD_JOBS_SCHEMA)
    async def add_jobs(args):
        room = config.MAX_NEW_JOBS - run.recorded
        us_room = config.MAX_US_JOBS - run.recorded_by_market["United States"]
        company_used = Counter(run.recorded_by_company)
        market_used = Counter(run.recorded_by_market)
        share = max(1, int(config.MAX_NEW_JOBS * config.TOP_MARKET_SHARE))
        batch_ids = {j.get("candidate_id") for j in args.get("jobs") or []}
        jobs, refused = [], []
        for job in args.get("jobs") or []:
            cid = job.pop("candidate_id", "") or ""
            c = run.pool.get(cid)
            if c:
                defaults = {"company": c.company, "role": c.title, "location": c.location,
                            "posted": c.posted, "compensation": c.compensation, "url": c.url,
                            "experience": filters.experience_range(c.experience_hints)}
                for k, v in defaults.items():
                    if not job.get(k):
                        job[k] = v
                job["market"] = c.market
            else:
                job["market"] = filters.market_of(job.get("location", ""))
            if not job.get("url") or not job.get("company") or not job.get("role"):
                refused.append({"role": job.get("role"), "reason": "need company, role and url "
                                                                   "(or a valid candidate_id)"})
                continue
            if len(jobs) >= room:
                refused.append({"role": job.get("role"), "reason": "run limit reached"})
                continue
            if job["market"] == "United States":
                if us_room <= 0:
                    refused.append({"role": job.get("role"),
                                    "reason": f"United States cap ({config.MAX_US_JOBS}) reached for this run"})
                    continue
                us_room -= 1
            if job["market"] in config.TOP_MARKETS and market_used[job["market"]] >= share:
                other = next(m for m in config.TOP_MARKETS if m != job["market"])
                if any(c2.id not in batch_ids for c2 in run.open_candidates(other)):
                    refused.append({"role": job.get("role"),
                                    "reason": f"{job['market']} has its share ({share}) while {other} "
                                              f"still has candidates; judge {other} next"})
                    continue
            company = store.company_key(job["company"])
            if company_used[company] >= config.MAX_PER_COMPANY:
                refused.append({"role": job.get("role"),
                                "reason": f"{job['company']} already has {config.MAX_PER_COMPANY} this run; "
                                          "leave its other roles undecided for a later run"})
                continue
            company_used[company] += 1
            market_used[job["market"]] += 1
            job["_company"] = company
            job["_board"] = c.board_key if c else ""
            jobs.append(job)
            if c:
                run.decided.add(cid)  # only once accepted; refused ones stay for next run
        result = store.append_jobs(jobs)
        added_urls = {store.job_key(j["url"]) for j in store.read_jobs()[-result["added"]:]} \
            if result["added"] else set()
        for job in jobs:
            if store.job_key(job["url"]) in added_urls:
                run.recorded_by_market[job["market"]] += 1
                run.recorded_by_company[job["_company"]] += 1
                if job["_board"]:
                    run.registry.record_recorded(job["_board"])
        run.recorded += result["added"]
        result["rejected"] = result["rejected"] + refused
        result["recorded_this_run"] = run.recorded
        result["remaining_slots"] = max(0, config.MAX_NEW_JOBS - run.recorded)
        result["remaining_us_slots"] = max(0, min(result["remaining_slots"],
                                                  config.MAX_US_JOBS - run.recorded_by_market["United States"]))
        return text_result(result)

    @tool("skip_candidates", "Mark pool candidates as judged and not a fit, with the reason. "
          "They won't be shown again on later runs.", SKIP_SCHEMA)
    async def skip_candidates(args):
        rows = []
        for item in args.get("items") or []:
            c = run.pool.get(item.get("candidate_id", ""))
            if not c:
                continue
            run.decided.add(c.id)
            run.skipped[c.market] += 1
            rows.append({"url": c.url, "decision": "skipped", "reason": item.get("reason", ""),
                         "company": c.company, "role": c.title})
        n = store.mark_seen(rows)
        return text_result({"skipped": n})

    @tool("register_board", "Add a company's job board to the registry from any URL on it (a posting "
          "or the board itself), harvest it now, and return its new candidates. Works for "
          "Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee and Workday.",
          {"type": "object", "properties": {
              "url": {"type": "string"},
              "company": {"type": "string", "description": "Company name as you'd want it shown"}},
           "required": ["url", "company"]})
    async def register_board(args):
        url, company = args["url"], args.get("company", "")
        board = detect_board(url, company)
        if board is None:
            system = unsupported_system(url)
            return text_result(f"Not a supported job board{f' ({system})' if system else ''}. "
                               "Treat roles there with Step 3 (open search and verification).")
        entry, new = run.registry.add(board, company, source="discovered")
        if not new and any(r.board.key == entry.key for r in run.board_results):
            open_here = [c for c in run.open_candidates() if c.board_key == entry.key]
            return text_result({"status": "already registered and harvested this run",
                                "open_candidates": [c.summary() for c in open_here[:15]]})
        async with make_client() as client:
            result = await harvest_board(client, entry.board, store.known_keys(), asyncio.Semaphore(4))
        run.absorb([result])
        if not result.ok:
            return text_result(f"Registered, but the board could not be read: {result.error}")
        run.registered.append(f"{entry.company} ({entry.ats})")
        found = rank(result.candidates)
        return text_result({"status": "registered" if new else "re-harvested",
                            "board": entry.key, "postings_on_board": result.postings,
                            "new_candidates": len(found),
                            "candidates": [c.summary() for c in found[:20]],
                            "note": "More via get_candidates." if len(found) > 20 else ""})

    @tool("find_boards", "Given company names, check Greenhouse, Lever, Ashby, SmartRecruiters, Workable "
          "and Recruitee for a job board with open postings, by trying likely slugs. Returns each hit's "
          "board_url, posting count and sample titles and locations. Nothing is registered: check the "
          "sample looks like that company, then pass board_url to register_board. Up to 15 names a call.",
          {"type": "object", "properties": {"companies": {"type": "array", "items": {"type": "string"}}},
           "required": ["companies"]})
    async def find_boards(args):
        names = [n for n in (args.get("companies") or []) if n.strip()][:15]
        result = await probe(names, known_keys=set(run.registry.entries))
        run.probed += len(names)
        return text_result(result)

    @tool("is_known", "Check whether posting URLs are already tracked or already judged.",
          {"type": "object", "properties": {"urls": {"type": "array", "items": {"type": "string"}}},
           "required": ["urls"]})
    async def is_known(args):
        known = store.known_keys()
        return text_result({u: (store.job_key(u) in known) for u in args.get("urls") or []})

    return [get_candidates, get_posting, add_jobs, skip_candidates, register_board, find_boards, is_known]


def harvest_summary(run: Run) -> str:
    ok = [r for r in run.board_results if r.ok]
    failed = [r for r in run.board_results if not r.ok]
    lines = [f"- Boards read: {len(ok)} of {len(run.board_results)}; "
             f"{sum(r.postings for r in ok)} postings scanned; {len(run.pool)} candidates in the pool."]
    counts = run.market_counts()
    lines.append("- Pool by market: " + (", ".join(f"{m} {counts[m]}" for m in filters.MARKET_ORDER
                                                    if counts.get(m)) or "empty"))
    for r in ok:
        for reason, n in r.dropped.items():
            lines.append(f"- Dropped from {r.board.company or r.board.slug}: {n} ({reason}).")
    for r in failed:
        lines.append(f"- Board failed: {r.board.company or r.board.slug} ({r.board.ats}): {r.error}")
    if not run.board_results:
        lines.append("- The registry is empty, so the pool is empty: each market is discovery only.")
    return "\n".join(lines)


def log_run(run: Run, result: ResultMessage | None, final_text: str) -> None:
    row = {
        "date": date.today().isoformat(),
        "boards": len(run.board_results),
        "boards_failed": sum(1 for r in run.board_results if not r.ok),
        "postings": sum(r.postings for r in run.board_results),
        "pool": len(run.pool),
        "recorded": run.recorded,
        "skipped": sum(run.skipped.values()),
        "registered": len(run.registered),
        "probed": run.probed,
        "cost_usd": f"{(result.total_cost_usd or 0):.2f}" if result else "",
        "turns": result.num_turns if result else "",
        "outcome": result.subtype if result else "no result",
        "summary": " ".join(final_text.split())[:500],
    }
    new = not config.RUNS_CSV.exists()
    config.RUNS_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(config.RUNS_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(f"## Job agent run, {row['date']}\n\n")
            f.write(f"Recorded **{run.recorded}**, skipped {row['skipped']}, "
                    f"registered {row['registered']} new boards. "
                    f"Cost ${row['cost_usd'] or '?'} over {row['turns']} turns ({row['outcome']}).\n\n")
            if run.recorded_by_market:
                f.write("By market: " + ", ".join(f"{m} {n}" for m, n in run.recorded_by_market.items()) + "\n\n")
            f.write("### Harvest (before the agent started)\n\n" + run.harvest_text + "\n\n")
            left = run.market_counts()
            f.write("Left undecided for later runs: " + (", ".join(
                f"{m} {left[m]}" for m in filters.MARKET_ORDER if left.get(m)) or "none") + "\n\n")
            f.write(f"Companies probed for boards: {run.probed}.\n\n")
            if final_text:
                f.write("### Agent summary\n\n" + final_text + "\n")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="harvest and print the pool; no model calls")
    args = ap.parse_args()

    for path in (config.RESUME_PATH, config.CRITERIA_PATH):
        if not path.exists():
            print(f"Missing {path}. Set JOBAGENT_HOME to your private folder.", file=sys.stderr)
            return 2

    registry = Registry.load()
    run = Run(registry)
    run.absorb(await harvest([e.board for e in registry.active()], store.known_keys()))
    run.harvest_text = harvest_summary(run)
    print(run.harvest_text, flush=True)

    if args.dry_run:
        no_desc = sum(1 for c in run.pool.values() if not c.description)
        stale = sum(1 for c in run.pool.values() if c.looks_stale)
        print(f"- Without a description: {no_desc}. Flagged stale: {stale}.")
        for c in run.open_candidates()[:60]:
            flags = (" | STALE" if c.looks_stale else "") + ("" if c.description else " | NO DESCRIPTION")
            print(f"  [{c.market}] {c.company}: {c.title} | {c.location} | "
                  f"{c.posted or c.posted_note or 'undated'}{flags}")
        return 0

    registry.save()  # keep board health even if the model step fails
    system_prompt = SYSTEM_PROMPT.format(
        today=date.today().isoformat(), max_new=config.MAX_NEW_JOBS, max_us=config.MAX_US_JOBS,
        max_company=config.MAX_PER_COMPANY,
        share=max(1, int(config.MAX_NEW_JOBS * config.TOP_MARKET_SHARE)),
        resume=config.RESUME_PATH.read_text(encoding="utf-8"),
        criteria=config.CRITERIA_PATH.read_text(encoding="utf-8"))

    server = create_sdk_mcp_server(name="jobs", version="2.0.0", tools=build_tools(run))
    tool_names = [f"mcp__jobs__{n}" for n in ("get_candidates", "get_posting", "add_jobs",
                                              "skip_candidates", "register_board", "find_boards",
                                              "is_known")]
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        mcp_servers={"jobs": server},
        tools=tool_names + ["WebSearch", "WebFetch"],
        allowed_tools=tool_names + ["WebSearch", "WebFetch"],
        permission_mode="dontAsk",
        model=config.MODEL,
        setting_sources=[],
        strict_mcp_config=True,
        max_budget_usd=config.MAX_BUDGET_USD,
    )

    final_text, result = "", None
    try:
        start = "Begin with India."
        kickoff = KICKOFF.format(harvest_summary=run.harvest_text, start=start)
        async for msg in query(prompt=kickoff, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        print(block.text, flush=True)
                        final_text = block.text
            elif isinstance(msg, ResultMessage):
                result = msg
                print(f"\nCost: ${msg.total_cost_usd or 0:.2f}  Turns: {msg.num_turns}  ({msg.subtype})")
    finally:
        registry.save()
        log_run(run, result, final_text)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
