"""The tools the model calls, exercised without the model or the network."""
import asyncio
import json

from jobagent import agent, config, store
from jobagent.ats import Board, Posting
from jobagent.harvest import BoardResult, to_candidate
from jobagent.registry import Registry


def call(tools, name, args):
    t = next(t for t in tools if t.name == name)
    out = asyncio.run(t.handler(args))
    text = out["content"][0]["text"]
    try:
        return json.loads(text)
    except ValueError:
        return text


def setup_run(tmp_path, monkeypatch, n=3):
    for attr, fname in [("JOBS_CSV", "jobs.csv"), ("SEEN_CSV", "seen.csv"), ("BOARDS_CSV", "boards.csv")]:
        monkeypatch.setattr(config, attr, tmp_path / fname)
    monkeypatch.setattr(config, "MAX_NEW_JOBS", 2)
    board = Board("lever", "acme", "Acme")
    reg = Registry([])
    reg.add(board, "Acme", source="seed")
    posts = [Posting("lever", "acme", "Acme", str(i), f"ML Engineer {i}", f"https://jobs.lever.co/acme/{i}",
                     location="Bengaluru, India", description="2+ years of experience") for i in range(n)]
    run = agent.Run(reg)
    run.absorb([BoardResult(board, ok=True, postings=n, candidates=[to_candidate(p, board) for p in posts])])
    return run, agent.build_tools(run)


def test_candidates_record_skip_and_limit(tmp_path, monkeypatch):
    run, tools = setup_run(tmp_path, monkeypatch, n=4)
    listing = call(tools, "get_candidates", {"market": "India"})
    assert listing["total_open"] == 4
    ids = [c["id"] for c in listing["candidates"]]

    res = call(tools, "add_jobs", {"jobs": [{"candidate_id": ids[0], "why": "fits"},
                                            {"candidate_id": ids[1], "why": "fits"},
                                            {"candidate_id": ids[2], "why": "fits"}]})
    assert res["added"] == 2 and res["remaining_slots"] == 0
    assert any(r["reason"] == "run limit reached" for r in res["rejected"])
    rows = store.read_jobs()
    assert rows[0]["market"] == "India" and rows[0]["url"].startswith("https://jobs.lever.co/acme/")

    assert call(tools, "skip_candidates", {"items": [{"candidate_id": ids[3], "reason": "7+ years"}]})["skipped"] == 1
    assert call(tools, "get_candidates", {"market": "India"})["total_open"] == 1  # the refused one stays open
    assert run.registry.entries["lever:acme"].recorded_total == "2"


def test_us_cap_and_experience_default(tmp_path, monkeypatch):
    run, tools = setup_run(tmp_path, monkeypatch, n=1)
    monkeypatch.setattr(config, "MAX_NEW_JOBS", 10)
    monkeypatch.setattr(config, "MAX_US_JOBS", 2)
    india_id = next(iter(run.pool))
    us_jobs = [{"company": f"US{i}", "role": "ML Engineer", "location": "Seattle, WA",
                "url": f"https://us.com/{i}", "why": "fits"} for i in range(3)]
    res = call(tools, "add_jobs", {"jobs": us_jobs + [{"candidate_id": india_id, "why": "fits"}]})
    assert res["added"] == 3  # two US plus the India one; the third US job is refused
    assert [r["reason"] for r in res["rejected"]] == ["United States cap (2) reached for this run"]
    assert res["remaining_us_slots"] == 0
    india = next(j for j in store.read_jobs() if j["market"] == "India")
    assert india["experience"] == "2+ yrs"  # filled from the posting's own wording


def test_company_cap_leaves_the_rest_undecided(tmp_path, monkeypatch):
    run, tools = setup_run(tmp_path, monkeypatch, n=5)
    monkeypatch.setattr(config, "MAX_NEW_JOBS", 10)
    monkeypatch.setattr(config, "MAX_PER_COMPANY", 3)
    ids = [c["id"] for c in call(tools, "get_candidates", {"market": "India", "limit": 10})["candidates"]]
    res = call(tools, "add_jobs", {"jobs": [{"candidate_id": i, "why": "fits"} for i in ids]})
    assert res["added"] == 3
    assert sum("already has 3 this run" in r["reason"] for r in res["rejected"]) == 2
    assert call(tools, "get_candidates", {"market": "India"})["total_open"] == 2
    # a later call in the same run still respects the cap
    res = call(tools, "add_jobs", {"jobs": [{"company": "ACME", "role": "AI Engineer", "location": "Pune",
                                             "url": "https://acme.com/9", "why": "fits"}]})
    assert res["added"] == 0


def test_register_board_rejects_unsupported(tmp_path, monkeypatch):
    _, tools = setup_run(tmp_path, monkeypatch)
    msg = call(tools, "register_board", {"url": "https://razorpay.darwinbox.in/ms/candidate/careers",
                                         "company": "Razorpay"})
    assert "Not a supported job board (darwinbox)" in msg


def test_is_known(tmp_path, monkeypatch):
    _, tools = setup_run(tmp_path, monkeypatch)
    store.mark_seen([{"url": "https://x.com/1"}])
    assert call(tools, "is_known", {"urls": ["https://x.com/1", "https://x.com/2"]}) == {
        "https://x.com/1": True, "https://x.com/2": False}


def _pool(run, board, specs):
    from jobagent.harvest import BoardResult, to_candidate
    posts = [Posting(board.ats, board.slug, comp, str(i), title, f"https://x.com/{board.slug}/{i}", location=loc,
                     description="Build things.") for i, (comp, title, loc) in enumerate(specs)]
    run.absorb([BoardResult(board, ok=True, postings=len(posts), candidates=[to_candidate(p, board) for p in posts])])


def test_india_singapore_share_with_overflow(tmp_path, monkeypatch):
    run, tools = setup_run(tmp_path, monkeypatch, n=0)
    monkeypatch.setattr(config, "MAX_NEW_JOBS", 10)
    monkeypatch.setattr(config, "TOP_MARKET_SHARE", 0.2)  # share = 2
    monkeypatch.setattr(config, "MAX_PER_COMPANY", 10)
    _pool(run, Board("lever", "in"), [(f"In{i}", "ML Engineer", "Bengaluru") for i in range(4)])
    _pool(run, Board("lever", "sg"), [("Sg", "ML Engineer", "Singapore")])
    india = [c.id for c in run.open_candidates("India")]
    res = call(tools, "add_jobs", {"jobs": [{"candidate_id": i, "why": "x"} for i in india]})
    assert res["added"] == 2 and "judge Singapore next" in res["rejected"][0]["reason"]
    sg = run.open_candidates("Singapore")[0].id
    call(tools, "add_jobs", {"jobs": [{"candidate_id": sg, "why": "x"}]})
    # Singapore is exhausted, so India's spare candidates may now overflow its share.
    res = call(tools, "add_jobs", {"jobs": [{"candidate_id": i, "why": "x"} for i in india[2:]]})
    assert res["added"] == 2


def test_same_role_on_board_and_mcf_is_kept_once(tmp_path, monkeypatch):
    run, tools = setup_run(tmp_path, monkeypatch, n=0)
    _pool(run, Board("mycareersfuture", "sg"), [("CFGI Singapore Pte Ltd", "AI Engineer", "Singapore")])
    _pool(run, Board("lever", "cfgi"), [("CFGI", "AI Engineer", "Singapore")])
    pool = run.open_candidates("Singapore")
    assert len(pool) == 1 and pool[0].board_key == "lever:cfgi"  # the company's own board wins
    call(tools, "add_jobs", {"jobs": [{"candidate_id": pool[0].id, "why": "x"}]})
    run2 = agent.Run(run.registry)  # next run: the MCF copy of a tracked job is not offered again
    _pool(run2, Board("mycareersfuture", "sg"), [("CFGI SINGAPORE PTE. LTD.", "AI Engineer", "Singapore")])
    assert run2.open_candidates() == []


def test_emptied_files_start_fresh(tmp_path, monkeypatch):
    run, tools = setup_run(tmp_path, monkeypatch, n=1)
    config.JOBS_CSV.write_text("", encoding="utf-8")
    config.SEEN_CSV.write_text("", encoding="utf-8")
    store.mark_seen([{"url": "https://s.com/1", "reason": "x"}])
    assert store.read_seen()[0]["reason"] == "x"
    call(tools, "add_jobs", {"jobs": [{"candidate_id": next(iter(run.pool)), "why": "x"}]})
    assert config.JOBS_CSV.read_text(encoding="utf-8").startswith("date_added,company")
    assert len(store.read_jobs()) == 1
