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
    monkeypatch.setattr(config, "CV_SOURCE_PATH", tmp_path / "missing.yaml")
    resume = tmp_path / "resume.md"
    resume.write_text("Plain resume", encoding="utf-8")
    monkeypatch.setattr(config, "RESUME_PATH", resume)
    assert agent.background_text() == "Plain resume"
