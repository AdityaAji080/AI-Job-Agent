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
