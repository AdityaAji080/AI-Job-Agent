"""Posting text for jobs found by open search, and releasing a market's unfilled slots."""
import asyncio

import httpx
import pytest

from jobagent import agent, config, postings, store
from jobagent.ats import Board
from jobagent.cv import batch
from tests.test_agent_tools import _pool, call, setup_run

LONG = ("You will build retrieval and evaluation pipelines for LLM agents used by insurers. "
        "Requirements: Python, PyTorch, 3+ years building production ML systems, "
        "experience with vector databases and model evaluation. ") * 6


@pytest.fixture
def data(tmp_path, monkeypatch):
    for attr, name in [("JOBS_CSV", "jobs.csv"), ("SEEN_CSV", "seen.csv"), ("BOARDS_CSV", "boards.csv")]:
        monkeypatch.setattr(config, attr, tmp_path / name)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return tmp_path


# ---------------------------------------------------------------- posting text from the agent

def test_open_search_job_keeps_the_text_the_agent_read(data, monkeypatch):
    run, tools = setup_run(data, monkeypatch, n=0)
    url = "https://careers.example-insurer.com/jobs/4411"
    res = call(tools, "add_jobs", {"jobs": [{"company": "Example Insurer", "role": "Applied AI Engineer",
                                             "location": "Singapore", "url": url, "why": "x",
                                             "posting_text": LONG}]})
    assert res["added"] == 1 and "note" not in res
    saved = postings.load(url)
    assert saved["source"] == "agent" and "vector databases" in saved["text"]
    assert "posting_text" not in store.read_jobs()[0]


def test_open_search_job_without_text_gets_a_note(data, monkeypatch):
    run, tools = setup_run(data, monkeypatch, n=0)
    url = "https://careers.example.com/jobs/1"
    res = call(tools, "add_jobs", {"jobs": [{"company": "Example", "role": "ML Engineer",
                                             "location": "Singapore", "url": url, "why": "x",
                                             "posting_text": "Too short."}]})
    assert res["added"] == 1 and "No posting_text for: ML Engineer" in res["note"]
    assert postings.load(url) is None


def test_board_text_wins_over_the_agents_copy(data, monkeypatch):
    run, tools = setup_run(data, monkeypatch, n=1)
    cid = next(iter(run.pool))
    call(tools, "add_jobs", {"jobs": [{"candidate_id": cid, "why": "x", "posting_text": LONG}]})
    saved = postings.load(run.pool[cid].url)
    assert saved["source"] == "lever:acme" and "2+ years of experience" in saved["text"]


def test_pool_candidate_without_a_description_takes_the_agents_text(data, monkeypatch):
    run, tools = setup_run(data, monkeypatch, n=1)
    cid = next(iter(run.pool))
    run.pool[cid].description = ""
    call(tools, "add_jobs", {"jobs": [{"candidate_id": cid, "why": "x", "posting_text": LONG}]})
    assert postings.load(run.pool[cid].url)["source"] == "agent"


# ---------------------------------------------------------------- reading the page itself

LD_PAGE = """<html><head><title>Careers</title>
<script type="application/ld+json">{"@context": "https://schema.org", "@graph": [
  {"@type": "Organization", "name": "Dyson"},
  {"@type": "JobPosting", "title": "Robotics Software Engineer",
   "description": "<p>%s</p><ul><li>C++ on embedded targets</li></ul>"}]}</script>
<script type="application/ld+json">{"@type": "JobPosting", "title": "Finance Analyst",
   "description": "<p>%s %s</p>"}</script>
</head><body><nav>Home Jobs About</nav><div id="app"></div></body></html>""" % (LONG, LONG, LONG)


def test_page_text_prefers_the_jobposting_for_this_role():
    text = postings.page_text(LD_PAGE, role="Robotics Software Engineer")
    assert text.startswith("Robotics Software Engineer") and "C++ on embedded targets" in text
    assert "Finance Analyst" not in text  # the longer block is a different job


def test_page_text_falls_back_to_visible_text_naming_the_role():
    html = f"<html><script>var x=1;</script><nav>Menu</nav><h1>Applied AI Engineer</h1><p>{LONG}</p></html>"
    text = postings.page_text(html, role="Applied AI Engineer")
    assert text.startswith("Applied AI Engineer") and "var x" not in text and "Menu" not in text


def test_page_text_rejects_shells_and_pages_about_something_else():
    assert postings.page_text("<html><div id='root'></div></html>", role="ML Engineer") == ""
    assert postings.page_text(f"<html><h1>Our culture</h1><p>{LONG}</p></html>",
                              role="Senior Perception Engineer") == ""


def _client(status=200, body=LD_PAGE, ctype="text/html; charset=utf-8"):
    return httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, text=body, headers={"content-type": ctype})))


def test_read_page_never_raises():
    url = "https://careers.dyson.com/job/123"
    assert "embedded targets" in asyncio.run(postings.read_page(url, "Robotics Software Engineer", _client()))
    assert asyncio.run(postings.read_page(url, "Robotics Software Engineer", _client(status=403))) == ""
    assert asyncio.run(postings.read_page(url, "x", _client(ctype="application/pdf"))) == ""

    def boom(request):
        raise httpx.ConnectError("down")
    broken = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    assert asyncio.run(postings.read_page(url, "x", broken)) == ""


def test_batch_reads_the_page_when_no_board_can(data, monkeypatch):
    async def unknown(url, company=""):
        return postings.UNKNOWN, None

    async def page(url, role="", client=None):
        return LONG if "careers.example" in url else ""
    monkeypatch.setattr(postings, "check", unknown)
    monkeypatch.setattr(postings, "read_page", page)
    rows = [{"url": "https://careers.example.com/jobs/7", "company": "Example", "role": "AI Engineer"},
            {"url": "https://spa.example.org/jobs/8", "company": "Spa", "role": "ML Engineer"}]
    ready, closed, missing = asyncio.run(batch._prepare(rows, need=5, log=lambda *_: None))
    assert [r["url"] for r in ready] == [rows[0]["url"]] and missing == 1
    assert postings.load(rows[0]["url"])["source"] == "page"


def test_ensure_falls_back_to_the_page(data, monkeypatch):
    async def unknown(url, company=""):
        return postings.UNKNOWN, None

    async def page(url, role="", client=None):
        return LONG
    monkeypatch.setattr(postings, "check", unknown)
    monkeypatch.setattr(postings, "read_page", page)
    got = asyncio.run(postings.ensure("https://careers.example.com/jobs/9", "Example", "AI Engineer"))
    assert got["source"] == "page" and got["company"] == "Example"


def test_ensure_does_not_read_the_page_of_a_closed_posting(data, monkeypatch):
    async def closed(url, company=""):
        return postings.CLOSED, None

    async def page(url, role="", client=None):
        raise AssertionError("a closed posting needs no CV")
    monkeypatch.setattr(postings, "check", closed)
    monkeypatch.setattr(postings, "read_page", page)
    assert asyncio.run(postings.ensure("https://jobs.lever.co/acme/1", "Acme", "MLE")) is None


# ---------------------------------------------------------------- a market's unfilled share is released

def _two_markets(data, monkeypatch, india_skipped):
    run, tools = setup_run(data, monkeypatch, n=0)
    monkeypatch.setattr(config, "MAX_NEW_JOBS", 5)  # Singapore 3, India 1, Canada 1
    monkeypatch.setattr(config, "MAX_PER_COMPANY", 10)
    monkeypatch.setattr(config, "MARKET_RELEASE_AFTER", 15)
    _pool(run, Board("lever", "sg"), [(f"Sg{i}", "ML Engineer", "Singapore") for i in range(5)])
    _pool(run, Board("lever", "in"), [(f"In{i}", "ML Engineer", "Bengaluru") for i in range(40)])
    sg = [c.id for c in run.open_candidates("Singapore")]
    assert call(tools, "add_jobs", {"jobs": [{"candidate_id": i, "why": "x"} for i in sg[:3]]})["added"] == 3
    india = [c.id for c in run.open_candidates("India")]
    call(tools, "skip_candidates", {"items": [{"candidate_id": i, "reason": "PhD required"}
                                              for i in india[:india_skipped]]})
    return run, tools, sg


def test_a_market_that_found_too_little_after_a_fair_look_releases_its_slot(data, monkeypatch):
    run, tools, sg = _two_markets(data, monkeypatch, india_skipped=15)
    res = call(tools, "add_jobs", {"jobs": [{"candidate_id": sg[3], "why": "x"}]})
    assert res["added"] == 1  # India still has 25 open candidates, but had its fair look


def test_before_the_fair_look_the_slot_is_held_and_the_refusal_says_how_many_more(data, monkeypatch):
    run, tools, sg = _two_markets(data, monkeypatch, india_skipped=14)
    res = call(tools, "add_jobs", {"jobs": [{"candidate_id": sg[3], "why": "x"}]})
    assert res["added"] == 0
    assert "judge India next" in res["rejected"][0]["reason"]
    assert "still to judge: India 1" in res["rejected"][0]["reason"]


def test_release_threshold_reaches_the_prompt():
    from jobagent.prompt import SYSTEM_PROMPT
    assert "{release_after}" in SYSTEM_PROMPT and "Spare slots." in SYSTEM_PROMPT
