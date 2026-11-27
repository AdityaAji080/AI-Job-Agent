"""Search breadth: stale postings never reach the model, new employers come first, level
variants of one role count once, the company cap matches employer name variants, and
market detection covers provinces, states and secondary cities."""
import asyncio
from datetime import date, timedelta

import pytest

from jobagent import agent, config, filters, store
from jobagent.ats import Board, Posting
from jobagent.harvest import harvest_board

from tests.test_agent_tools import _board_pool, call, setup_run


class _FakeAdapter:
    def __init__(self, posts):
        self.posts = posts

    async def fetch(self, client, board):
        return self.posts


def test_stale_postings_are_dropped_before_the_model(monkeypatch):
    from jobagent import harvest
    old = (date.today() - timedelta(days=200)).isoformat()
    posts = [Posting("lever", "acme", "Acme", "stale", "ML Engineer", "https://jobs.lever.co/acme/stale",
                     location="Singapore", posted=old, description="Build models."),
             Posting("lever", "acme", "Acme", "intake", "ML Engineer, Graduate", "https://jobs.lever.co/acme/intake",
                     location="Singapore", posted=old,
                     description=f"The programme starts in July {date.today().year + 1}."),
             Posting("lever", "acme", "Acme", "fresh", "AI Engineer", "https://jobs.lever.co/acme/fresh",
                     location="Singapore", posted=date.today().isoformat(), description="Build agents.")]
    monkeypatch.setitem(harvest.ADAPTERS, "lever", _FakeAdapter(posts))
    result = asyncio.run(harvest_board(None, Board("lever", "acme", "Acme"), set(), asyncio.Semaphore(1)))
    kept = sorted(c.id.rsplit(":", 1)[-1] for c in result.candidates)
    assert kept == ["fresh", "intake"]
    assert sum(n for reason, n in result.dropped.items() if reason.startswith("stale")) == 1


def test_new_employers_rank_before_returning_ones(tmp_path, monkeypatch):
    run, tools = setup_run(tmp_path, monkeypatch, n=0)
    monkeypatch.setattr(config, "COMPANY_COOLDOWN_DAYS", 14)
    store.mark_seen([{"url": "https://old.com/1", "decision": "recorded", "company": "Oldco Pte Ltd"}])
    when = (date.today() - timedelta(days=40)).isoformat()
    config.SEEN_CSV.write_text(config.SEEN_CSV.read_text().replace(date.today().isoformat(), when))
    run = agent.Run(agent.Registry([]))
    _board_pool(run, Board("lever", "oldco"), [("Oldco", "ML Engineer", "Singapore")])
    _board_pool(run, Board("lever", "newco"), [("Newco", "ML Engineer", "Singapore")])
    tools = agent.build_tools(run)
    listing = call(tools, "get_candidates", {"market": "Singapore"})["candidates"]
    assert [c["company"] for c in listing] == ["Newco", "Oldco"]
    assert listing[1]["employer_recorded_before"] == when and "employer_recorded_before" not in listing[0]


@pytest.mark.parametrize("a,b,same", [
    ("Applied Scientist II", "Applied Scientist III", True),
    ("Senior Staff Engineer, Embedded Software", "Staff Engineer - Embedded Software", True),
    ("Lead Perception Engineer", "Perception Engineer", True),
    ("Perception Engineer", "Planning Engineer", False),
    ("C++ Developer", "C Developer", False),
    ("Senior", "Staff", False),  # nothing left after levels: compare whole titles
])
def test_level_variants_are_one_role(a, b, same):
    assert (store.role_key("Glance", a) == store.role_key("Glance", b)) is same


def test_level_variant_of_a_tracked_job_is_rejected(tmp_path, monkeypatch):
    setup_run(tmp_path, monkeypatch, n=0)
    first = store.append_jobs([{"company": "Glance", "role": "Applied Scientist II",
                                "url": "https://x.com/1", "market": "India"}])
    again = store.append_jobs([{"company": "Glance", "role": "Applied Scientist III",
                                "url": "https://x.com/2", "market": "India"}])
    assert first["added"] == 1 and again["added"] == 0


def test_company_cap_matches_name_variants(tmp_path, monkeypatch):
    run, tools = setup_run(tmp_path, monkeypatch, n=0)
    monkeypatch.setattr(config, "MAX_NEW_JOBS", 5)
    monkeypatch.setattr(config, "MAX_PER_COMPANY", 1)
    res = call(tools, "add_jobs", {"jobs": [
        {"company": "Grab", "role": "ML Engineer", "location": "Singapore", "url": "https://grab.com/1", "why": "x"},
        {"company": "Grab Taxi Holdings Pte Ltd", "role": "Perception Engineer", "location": "Singapore",
         "url": "https://grab.com/2", "why": "x"}]})
    assert res["added"] == 1 and "already has 1" in res["rejected"][0]["reason"]


@pytest.mark.parametrize("loc,market", [
    ("Mississauga", "Canada"), ("Markham, Ontario", "Canada"), ("British Columbia (remote-first)", "Canada"),
    ("Kanata", "Canada"), ("Toronto, Ontario, CAN", "Canada"), ("Ontario, CA", "United States"),
    ("Lucknow", "India"), ("Karnataka", "India"), ("IN-KA-Bangalore", "India"), ("Bangalore, IND", "India"),
    ("SG", "Singapore"), ("London, UK", "Other"), ("Indianapolis, IN", "United States"),
])
def test_market_detection_covers_provinces_states_and_cities(loc, market):
    assert filters.market_of(loc) == market


def test_background_comes_from_cv_source(tmp_path, monkeypatch):
    src = tmp_path / "cv_source.yaml"
    src.write_text("name: A B\nheadline: ML Engineer\nsummary: Builds things.\n"
                   "contact: {email: a@b.c}\n"
                   "skills: [{label: Languages, items: [Python]}]\n"
                   "experience: [{id: x, title: Engineer, org: Acme, dates: 2024, "
                   "bullets: [{id: a, text: Shipped a thing.}]}]\n"
                   "projects: [{id: p, title: Paper, venue: ICLR, bullets: [{id: b, text: Wrote it.}]}]\n"
                   "education: [{degree: MSc, school: NTU, coursework: [CV]}]\n", encoding="utf-8")
    monkeypatch.setattr(config, "CV_SOURCE_PATH", src)
    text = agent.background_text()
    for part in ("ML Engineer", "Languages: Python", "Engineer | Acme | 2024", "Shipped a thing.",
                 "Paper | ICLR", "MSc | NTU. Coursework: CV"):
        assert part in text
    assert "a@b.c" not in text


# ---------------------------------------------------------------- unproductive boards

def test_boards_that_never_pay_off_are_retired(tmp_path, monkeypatch):
    from jobagent.harvest import BoardResult, to_candidate
    run, _ = setup_run(tmp_path, monkeypatch, n=0)
    monkeypatch.setattr(config, "BOARD_RETIRE_AFTER", 3)
    store.mark_seen([{"url": "https://k.com/1", "decision": "recorded", "company": "Kept Co"}])
    run = agent.Run(agent.Registry([]))
    boards = {name: Board("lever", name.lower().replace(" ", ""), name)
              for name in ("Dead", "Paying", "Kept Co", "Young")}
    for b in boards.values():
        run.registry.add(b, b.company)
    run.registry.record_recorded(boards["Paying"].key)

    def feed(board, n):
        posts = [Posting("lever", board.slug, board.company, f"{board.slug}{i}", "ML Engineer",
                         f"https://x.com/{board.slug}/{i}", location="Singapore") for i in range(n)]
        run.absorb([BoardResult(board, ok=True, postings=n, candidates=[to_candidate(p, board) for p in posts])])

    for name in ("Dead", "Paying", "Kept Co"):
        feed(boards[name], 3)
    feed(boards["Young"], 2)
    enabled = {e.company: e.enabled for e in run.registry.entries.values()}
    assert enabled == {"Dead": "no", "Paying": "yes", "Kept Co": "yes", "Young": "yes"}
    assert run.retired == ["Dead"] and "Dead" in agent.harvest_summary(run)
    monkeypatch.setattr(config, "BOARD_RETIRE_AFTER", 0)
    assert not run.registry.retire_if_unproductive(boards["Young"].key)


# ---------------------------------------------------------------- reserved discovery budget

class _Result:
    def __init__(self, cost, turns, subtype="success"):
        self.total_cost_usd, self.num_turns, self.subtype = cost, turns, subtype


def _fake_sessions(monkeypatch, costs):
    calls = []

    async def fake(prompt, options):
        calls.append((prompt, options.max_budget_usd))
        return f"summary {len(calls)}", _Result(costs[len(calls) - 1], 10)
    monkeypatch.setattr(agent, "_session", fake)
    return calls


@pytest.mark.parametrize("judging_cost,discovery", [(1.2, True), (2.9, False)])
def test_budget_is_split_between_judging_and_discovery(tmp_path, monkeypatch, judging_cost, discovery):
    import csv
    home = tmp_path / "home"
    (home / "profile").mkdir(parents=True)
    (home / "profile" / "criteria.md").write_text("criteria", encoding="utf-8")
    (home / "profile" / "cv_source.yaml").write_text("name: A\nheadline: ML Engineer\n", encoding="utf-8")
    for attr, rel in [("HOME", ""), ("DATA_DIR", "data"), ("PROFILE_DIR", "profile")]:
        monkeypatch.setattr(config, attr, home / rel)
    for attr, rel in [("JOBS_CSV", "data/jobs.csv"), ("SEEN_CSV", "data/seen.csv"),
                      ("BOARDS_CSV", "data/boards.csv"), ("RUNS_CSV", "data/runs.csv"),
                      ("CRITERIA_PATH", "profile/criteria.md"),
                      ("CV_SOURCE_PATH", "profile/cv_source.yaml")]:
        monkeypatch.setattr(config, attr, home / rel)
    monkeypatch.setattr(config, "MAX_BUDGET_USD", 3.0)
    monkeypatch.setattr(config, "DISCOVERY_RESERVE", 0.3)
    monkeypatch.setattr(agent.Registry, "load", classmethod(lambda cls: cls([])))

    async def no_harvest(boards, known):
        return []
    monkeypatch.setattr(agent, "harvest", no_harvest)
    monkeypatch.setattr("sys.argv", ["agent"])
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    calls = _fake_sessions(monkeypatch, [judging_cost, 0.8])
    assert asyncio.run(agent.main()) == 0
    assert calls[0][1] == pytest.approx(2.1)
    assert len(calls) == (2 if discovery else 1)
    row = list(csv.DictReader(open(config.RUNS_CSV, encoding="utf-8")))[-1]
    if discovery:
        assert calls[1][1] == pytest.approx(3.0 - judging_cost)
        assert "discovery session" in calls[1][0] and "India first" in calls[1][0]
        assert row["cost_usd"] == f"{judging_cost + 0.8:.2f}" and row["turns"] == "20"
        assert "Discovery session: summary 2" in row["summary"]
    else:
        assert row["cost_usd"] == f"{judging_cost:.2f}" and row["outcome"] == "success"


def _fake_query(monkeypatch, subtype):
    """query() as the SDK runs it: a reply, an error result, then the CLI's exit 1."""
    from claude_agent_sdk import AssistantMessage, ProcessError, ResultMessage, TextBlock

    async def fake(prompt, options):
        yield AssistantMessage(content=[TextBlock(text="Recorded 7 jobs.")], model="sonnet")
        yield ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=True,
                            num_turns=58, session_id="s", total_cost_usd=2.54)
        raise ProcessError("Claude Code returned an error result", exit_code=1)
    monkeypatch.setattr(agent, "query", fake)


@pytest.mark.parametrize("subtype", ["error_max_budget_usd", "error_max_turns"])
def test_reaching_a_limit_ends_the_session_normally(monkeypatch, subtype):
    _fake_query(monkeypatch, subtype)
    text, result = asyncio.run(agent._session("go", None))
    assert text == "Recorded 7 jobs." and result.subtype == subtype and result.total_cost_usd == 2.54


def test_other_session_errors_still_fail(monkeypatch):
    from claude_agent_sdk import ProcessError
    _fake_query(monkeypatch, "error_during_execution")
    with pytest.raises(ProcessError):
        asyncio.run(agent._session("go", None))
