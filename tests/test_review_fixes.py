"""Regression tests for the fixes from the 2026-10-23 code review."""
import asyncio
import csv
from pathlib import Path

import pytest
import yaml

from jobagent import agent, config, filters, postings, store
from jobagent.ats import Board, Posting
from jobagent.ats import smartrecruiters as sr
from jobagent.ats.base import BoardError
from jobagent.harvest import BoardResult, prefilter, to_candidate
from tests.fixtures import payloads as P
from tests.test_agent_tools import call, setup_run

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def data(tmp_path, monkeypatch):
    for attr, name in [("JOBS_CSV", "jobs.csv"), ("SEEN_CSV", "seen.csv"),
                       ("BOARDS_CSV", "boards.csv"), ("RUNS_CSV", "runs.csv")]:
        monkeypatch.setattr(config, attr, tmp_path / name)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return tmp_path


# ---------------------------------------------------------------- 1. the run starts where the prompt says

def test_the_kickoff_starts_with_the_first_market_the_prompt_names():
    from jobagent.prompt import SYSTEM_PROMPT
    assert agent.kickoff_start() == "Begin with Singapore."
    assert "**Singapore first.**" in SYSTEM_PROMPT


# ---------------------------------------------------------------- 2. runs.csv keeps its columns

ROW = {"date": "2026-10-24", "boards": "74", "boards_failed": "0", "postings": "12000", "pool": "2800",
       "recorded": "7", "skipped": "5", "registered": "3", "probed": "120", "cost_usd": "1.10",
       "turns": "50", "outcome": "success", "summary": "Recorded 7, with a comma, here."}
OLD_HEADER = [k for k in ROW if k != "probed"]


def _runs():
    with open(config.RUNS_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_runs_csv_written_fresh_and_appended(data):
    agent.append_run_row(dict(ROW))
    agent.append_run_row({**ROW, "date": "2026-10-25"})
    rows = _runs()
    assert [r["date"] for r in rows] == ["2026-10-24", "2026-10-25"]
    assert rows[1]["cost_usd"] == "1.10" and rows[1]["probed"] == "120"


def test_runs_csv_from_an_older_version_is_repaired_once(data):
    # As in the live file: an old header without `probed`, one row written under it, and
    # one row appended later with the newer columns (one field longer, so shifted).
    with open(config.RUNS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(OLD_HEADER)
        w.writerow(["2026-10-17", "18", "0", "2593", "997", "25", "55", "0", "0.25", "19", "success", "first"])
        w.writerow(["2026-10-17", "34", "0", "4546", "1723", "40", "52", "15", "75", "0.92", "39", "success", "x, y"])
    agent.append_run_row(dict(ROW))
    rows = _runs()
    assert list(rows[0]) == list(ROW)
    assert (rows[0]["cost_usd"], rows[0]["turns"], rows[0]["probed"]) == ("0.25", "19", "")
    assert (rows[1]["cost_usd"], rows[1]["turns"], rows[1]["probed"]) == ("0.92", "39", "75")
    assert rows[1]["summary"] == "x, y"
    assert rows[2]["cost_usd"] == "1.10"


# ---------------------------------------------------------------- 3. a skipped SmartRecruiters posting stays skipped

def test_a_skipped_smartrecruiters_posting_does_not_come_back(data):
    board = Board("smartrecruiters", "boschgroup", "Bosch")
    p = sr.parse(P.SMARTRECRUITERS_LIST, board)[0]
    sr.parse_detail(P.SMARTRECRUITERS_DETAIL, p)  # the url becomes /BoschGroup/<id>-<title>
    assert p.url.endswith("-computer-vision-engineer")
    store.mark_seen([{"url": p.url, "decision": "skipped", "reason": "not a fit"}])
    tomorrow = sr.parse(P.SMARTRECRUITERS_LIST, board)  # listed again as /boschgroup/<id>
    assert prefilter(tomorrow, store.known_keys()) == []


def test_smartrecruiters_spellings_dedupe_in_the_tracker_and_is_known(data, monkeypatch):
    run, tools = setup_run(data, monkeypatch, n=0)
    long = "https://jobs.smartrecruiters.com/Grab/744000156252394-senior-ai-engineer"
    short = "https://jobs.smartrecruiters.com/grab/744000156252394"
    assert store.append_jobs([{"company": "Grab", "role": "Senior AI Engineer", "url": long,
                               "market": "Singapore"}])["added"] == 1
    again = store.append_jobs([{"company": "Grab", "role": "AI Engineer II", "url": short, "market": "India"}])
    assert again["added"] == 0 and again["rejected"][0]["reason"] == "already tracked"
    assert call(tools, "is_known", {"urls": [short, long]}) == {short: True, long: True}
    assert store.mark_seen([{"url": short, "decision": "skipped"}]) == 0


def test_other_urls_with_a_number_and_dash_are_not_aliased():
    assert store.key_aliases("jobs.lever.co/acme/123-abc") == {"jobs.lever.co/acme/123-abc"}
    assert store.key_aliases("") == set()


# ---------------------------------------------------------------- 4. no tailoring after failed tests

def test_tailor_job_needs_the_tests_to_pass():
    wf = yaml.safe_load((ROOT / ".github/workflows/run.yml").read_text(encoding="utf-8"))
    tailor = wf["jobs"]["tailor"]
    assert "tests" in tailor["needs"] and "run" in tailor["needs"]
    assert "needs.tests.result == 'success'" in tailor["if"]


# ---------------------------------------------------------------- 5. country codes are not US states

@pytest.mark.parametrize("loc", ["Berlin, DE", "Tel Aviv, IL", "Jakarta, ID", "Bogotá, CO",
                                 "Buenos Aires, AR"])
def test_country_codes_after_foreign_cities_are_other(loc):
    assert filters.market_of(loc) == "Other"


@pytest.mark.parametrize("loc,market", [
    ("Austin, TX", "United States"), ("Boston, MA", "United States"), ("Dublin, CA", "United States"),
    ("Pittsburgh, PA", "United States"), ("Manchester, NH", "United States"),
    ("Toronto, ON", "Canada"), ("Bengaluru, IN", "India"), ("Singapore / Berlin, DE", "Singapore"),
    ("New York, NY / London, UK", "United States")])
def test_state_codes_still_mean_the_us(loc, market):
    assert filters.market_of(loc) == market


# ---------------------------------------------------------------- 6. open or closed, without a truncated list

def _check(url, company="Bosch"):
    return asyncio.run(postings.check(url, company))


def test_smartrecruiters_posting_is_read_by_id_not_found_in_a_capped_list(data, monkeypatch):
    seen = {}

    async def no_list(client, board):
        raise AssertionError("the 2,000-posting list must not decide this")

    async def detail(client, posting):
        seen["id"] = posting.job_id
        posting.description = "Perception work"
    monkeypatch.setattr(postings.ADAPTERS["smartrecruiters"], "fetch", no_list)
    monkeypatch.setattr(postings.ADAPTERS["smartrecruiters"], "detail", detail)
    status, p = _check("https://jobs.smartrecruiters.com/BoschGroup/744000155415659-research-scientist")
    assert status == postings.OPEN and seen["id"] == "744000155415659" and p.description == "Perception work"


def test_smartrecruiters_404_on_the_posting_is_closed(data, monkeypatch):
    async def detail(client, posting):
        raise BoardError("not found (404) at https://api.smartrecruiters.com/...")
    monkeypatch.setattr(postings.ADAPTERS["smartrecruiters"], "detail", detail)
    assert _check("https://jobs.smartrecruiters.com/BoschGroup/744000155415659-x")[0] == postings.CLOSED


def test_a_404_from_a_board_found_by_company_name_is_not_closed(data, monkeypatch):
    from jobagent.registry import Registry
    reg = Registry([])
    reg.add(Board("lever", "acme-old", "Acme"), "Acme", source="seed")
    reg.save()

    async def gone(client, board):
        raise BoardError("not found (404) at https://api.lever.co/v0/postings/acme-old")
    monkeypatch.setattr(postings.ADAPTERS["lever"], "fetch", gone)
    assert _check("https://acme.com/careers/42", company="Acme")[0] == postings.UNKNOWN


# ---------------------------------------------------------------- 7. register_board shows only what the pool kept

def test_register_board_lists_only_candidates_the_pool_kept(data, monkeypatch):
    run, tools = setup_run(data, monkeypatch, n=0)
    store.append_jobs([{"company": "Held", "role": "Old role", "url": "https://held.com/1",
                        "market": "Singapore"}])
    run = agent.Run(agent.Registry([]))  # "Held" is now on hold-back
    tools = agent.build_tools(run)
    board = Board("lever", "mixed", "Mixed")
    posts = [Posting("lever", "mixed", "Held", "1", "ML Engineer", "https://jobs.lever.co/mixed/1",
                     location="Singapore", description="x"),
             Posting("lever", "mixed", "Fresh Co", "2", "ML Engineer", "https://jobs.lever.co/mixed/2",
                     location="Singapore", description="x")]

    async def harvest_board(client, b, known, sem):
        return BoardResult(board, ok=True, postings=2, candidates=[to_candidate(p, board) for p in posts])
    monkeypatch.setattr(agent, "harvest_board", harvest_board)
    res = call(tools, "register_board", {"url": "https://jobs.lever.co/mixed", "company": "Mixed"})
    assert [c["company"] for c in res["candidates"]] == ["Fresh Co"]
    assert res["new_candidates"] == 1 and "1 more left out" in res["note"]
