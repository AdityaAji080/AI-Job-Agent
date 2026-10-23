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
    monkeypatch.setattr(config, "MAX_PER_COMPANY", 3)
    monkeypatch.setattr(config, "COMPANY_COOLDOWN_DAYS", 14)
    monkeypatch.setattr(config, "MAX_CROWDED_PER_RUN", 2)
    monkeypatch.setattr(config, "LARGE_EMPLOYER_POSTINGS", 150)
    monkeypatch.setattr(config, "CROWDED_APPLICANTS", 100)
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


def test_market_slots_follow_the_shares(monkeypatch):
    assert config.market_slots(5) == {"Singapore": 3, "India": 1, "Canada": 1}
    assert config._parse_shares("Singapore=0.5, India=0.5, bad, Canada=x") == {"Singapore": 0.5, "India": 0.5}
    assert config._parse_shares("") == config.DEFAULT_MARKET_SHARES


def test_singapore_takes_its_share_then_spare_slots_flow_over(tmp_path, monkeypatch):
    run, tools = setup_run(tmp_path, monkeypatch, n=0)
    monkeypatch.setattr(config, "MAX_NEW_JOBS", 5)  # Singapore 3, India 1, Canada 1
    monkeypatch.setattr(config, "MAX_PER_COMPANY", 10)
    _pool(run, Board("lever", "sg"), [(f"Sg{i}", "ML Engineer", "Singapore") for i in range(4)])
    _pool(run, Board("lever", "in"), [(f"In{i}", "ML Engineer", "Bengaluru") for i in range(3)])
    sg = [c.id for c in run.open_candidates("Singapore")]
    res = call(tools, "add_jobs", {"jobs": [{"candidate_id": i, "why": "x"} for i in sg]})
    assert res["added"] == 3 and "judge India next" in res["rejected"][0]["reason"]
    assert len(run.open_candidates("Singapore")) == 1  # the refused one stays for later
    # India takes its one slot, then the spare one too: Singapore has used its share and
    # Canada, still under its share, has no candidates.
    india = [c.id for c in run.open_candidates("India")]
    res = call(tools, "add_jobs", {"jobs": [{"candidate_id": i, "why": "x"} for i in india]})
    assert res["added"] == 2 and res["rejected"][0]["reason"] == "run limit reached"


def test_an_under_share_market_waits_only_on_candidates_it_could_record(tmp_path, monkeypatch):
    run, tools = setup_run(tmp_path, monkeypatch, n=0)
    monkeypatch.setattr(config, "MAX_NEW_JOBS", 5)
    monkeypatch.setattr(config, "MAX_PER_COMPANY", 1)
    _pool(run, Board("lever", "sg"), [("Sg", "ML Engineer", "Singapore"), ("Sg", "AI Engineer", "Singapore")])
    _pool(run, Board("lever", "in"), [(f"In{i}", "ML Engineer", "Bengaluru") for i in range(3)])
    sg = [c.id for c in run.open_candidates("Singapore")]
    res = call(tools, "add_jobs", {"jobs": [{"candidate_id": i, "why": "x"} for i in sg]})
    assert res["added"] == 1 and "already has 1 this run" in res["rejected"][0]["reason"]
    # Singapore's last candidate is from a company at its cap, so India isn't held back.
    india = [c.id for c in run.open_candidates("India")]
    assert call(tools, "add_jobs", {"jobs": [{"candidate_id": i, "why": "x"} for i in india]})["added"] == 3


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


def test_mcf_link_is_refused_and_employer_page_is_recorded(tmp_path, monkeypatch):
    from jobagent import postings
    from jobagent.ats import mycareersfuture as mcf
    from jobagent.harvest import BoardResult, to_candidate
    run, tools = setup_run(tmp_path, monkeypatch, n=0)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    board = Board("mycareersfuture", "sg")
    mcf_url = "https://www.mycareersfuture.gov.sg/job/engineering/ai-engineer-cfgi-0123456789abcdef0123456789abcdef"
    p = Posting("mycareersfuture", "sg", "CFGI", "0123456789abcdef0123456789abcdef", "AI Engineer", mcf_url,
                location="Singapore", compensation="SGD 7,000–9,000 / month", description="Build agents.")
    run.absorb([BoardResult(board, ok=True, postings=1, candidates=[to_candidate(p, board)])])
    cand = run.open_candidates("Singapore")[0]
    assert "citizens and PRs" in cand.summary()["apply_via"]

    # Recording it with the pool's own link is refused, and the candidate stays open.
    res = call(tools, "add_jobs", {"jobs": [{"candidate_id": cand.id, "why": "fits"}]})
    assert res["added"] == 0 and res["rejected"][0]["reason"] == mcf.NOT_A_ROUTE
    assert [c.id for c in run.open_candidates("Singapore")] == [cand.id]

    # A hand-entered MyCareersFuture link is refused the same way.
    res = call(tools, "add_jobs", {"jobs": [{"company": "CFGI", "role": "AI Engineer", "location": "Singapore",
                                             "url": mcf_url, "why": "fits"}]})
    assert res["added"] == 0 and res["rejected"][0]["reason"] == mcf.NOT_A_ROUTE

    # With the employer's own page as url, it records, keeping the pool's other fields.
    own = "https://careers.cfgi.com/jobs/ai-engineer-sg"
    res = call(tools, "add_jobs", {"jobs": [{"candidate_id": cand.id, "url": own, "why": "fits"}]})
    assert res["added"] == 1
    row = store.read_jobs()[0]
    assert row["url"] == own and row["market"] == "Singapore" and row["compensation"].startswith("SGD 7,000")
    saved = postings.load(own)
    assert saved["text"] == "Build agents." and saved["source"] == "mycareersfuture:sg"
    assert run.open_candidates("Singapore") == []


def test_mcf_listing_urls_are_recognised():
    from jobagent.ats import mycareersfuture as mcf
    assert mcf.is_listing("https://www.MyCareersFuture.gov.sg/job/x")
    assert mcf.is_listing("https://api.mycareersfuture.gov.sg/v2/jobs/abc")
    assert not mcf.is_listing("https://jobs.lever.co/acme/1") and not mcf.is_listing("")


def test_system_prompt_formats_with_freshness_settings():
    from jobagent.prompt import SYSTEM_PROMPT
    text = SYSTEM_PROMPT.format(today="2026-10-22", resume="r", criteria="c", max_us=1, max_new=5,
                                max_company=2, fresh_days=config.FRESH_DAYS, old_days=config.OLD_DAYS,
                                sg_slots=3, shares="Singapore 3, India 1, Canada 1", cooldown_days=14,
                                large_postings=150, crowded_applicants=100, max_crowded=2,
                                release_after=15)
    assert f"up to {config.FRESH_DAYS} days old" in text and "{" not in text
    flat = " ".join(text.split())
    assert "last 14 days is held back" in flat and "at most 2 can be recorded per run" in flat
    # A link the agent couldn't open is never recorded (the Dyson case).
    assert "Never record a link you couldn't open" in flat and "never record one you couldn't open" in flat
    assert "Aim discovery at employers most applicants never think of" in flat


# ---------------------------------------------------------------- many employers, few crowds

def _board_pool(run, board, specs, postings=None, applicants=None):
    """specs: (company, title, location). `postings` is the board's total open roles."""
    from jobagent.harvest import BoardResult, to_candidate
    posts = [Posting(board.ats, board.slug, comp, f"{board.slug}{i}", title, f"https://x.com/{board.slug}/{i}",
                     location=loc, description="Build things.",
                     extra={"applicants": applicants} if applicants is not None else {})
             for i, (comp, title, loc) in enumerate(specs)]
    run.absorb([BoardResult(board, ok=True, postings=postings or len(posts),
                            candidates=[to_candidate(p, board) for p in posts])])


def test_a_company_recorded_recently_is_held_back(tmp_path, monkeypatch):
    from datetime import date, timedelta
    _, _ = setup_run(tmp_path, monkeypatch, n=0)
    monkeypatch.setattr(config, "MAX_NEW_JOBS", 5)
    recent = (date.today() - timedelta(days=3)).isoformat()
    old = (date.today() - timedelta(days=20)).isoformat()
    store.mark_seen([{"url": "https://a.com/1", "decision": "recorded", "company": "Grab Holdings Pte Ltd"},
                     {"url": "https://b.com/1", "decision": "recorded", "company": "Stripe"}])
    rows = config.SEEN_CSV.read_text().replace(date.today().isoformat(), recent, 1)
    config.SEEN_CSV.write_text(rows.replace(date.today().isoformat(), old, 1))
    run = agent.Run(run_registry := agent.Registry([]))
    tools = agent.build_tools(run)
    _board_pool(run, Board("lever", "grab"), [("Grab", "ML Engineer", "Singapore")])
    _board_pool(run, Board("lever", "stripe"), [("Stripe", "ML Engineer", "Singapore")])
    # Grab (3 days ago) is out of the pool; Stripe (20 days ago) is back.
    assert [c.company for c in run.open_candidates()] == ["Stripe"]
    assert run.held_back == {"Grab": 1} and "Held back" in agent.harvest_summary(run)
    res = call(tools, "add_jobs", {"jobs": [{"company": "Grab", "role": "AI Engineer", "location": "Singapore",
                                             "url": "https://grab.careers/1", "why": "x"}]})
    assert res["added"] == 0 and "held back for 14 days" in res["rejected"][0]["reason"]


def test_crowded_postings_rank_last_and_are_capped(tmp_path, monkeypatch):
    run, tools = setup_run(tmp_path, monkeypatch, n=0)
    monkeypatch.setattr(config, "MAX_NEW_JOBS", 10)
    monkeypatch.setattr(config, "MAX_PER_COMPANY", 10)
    _board_pool(run, Board("lever", "big"), [(f"BigCo", f"ML Engineer {i}", "Singapore") for i in range(3)],
                postings=900)
    _board_pool(run, Board("lever", "small"), [("SmallCo", "ML Engineer", "Singapore")], postings=12)
    _board_pool(run, Board("mycareersfuture", "sg"), [("Popular Pte Ltd", "AI Engineer", "Singapore")],
                applicants=250)
    order = run.open_candidates("Singapore")
    assert order[0].company == "SmallCo" and not order[0].crowded
    assert {c.crowded for c in order[1:]} == {"large employer (900 open roles)", "250 applicants so far"}
    assert "large employer" in order[1].summary()["crowded"]
    res = call(tools, "add_jobs", {"jobs": [{"candidate_id": c.id, "why": "x"} for c in order]})
    assert res["added"] == 3  # SmallCo plus two crowded
    assert sum("crowded posting" in r["reason"] for r in res["rejected"]) == 2
    assert run.recorded_crowded == 2


def test_a_mcf_ad_from_a_large_employer_counts_as_crowded(tmp_path, monkeypatch):
    run, tools = setup_run(tmp_path, monkeypatch, n=0)
    _board_pool(run, Board("mycareersfuture", "sg"), [("GRAB TAXI HOLDINGS PTE. LTD.", "AI Engineer", "Singapore")])
    _board_pool(run, Board("smartrecruiters", "grab"), [("Grab Taxi Holdings", "Data Engineer", "Singapore")],
                postings=441)
    mcf = next(c for c in run.open_candidates() if c.board_key.startswith("mycareersfuture"))
    assert mcf.crowded == "large employer (441 open roles)"


def test_spare_slots_skip_a_market_left_with_only_crowded_candidates(tmp_path, monkeypatch):
    run, tools = setup_run(tmp_path, monkeypatch, n=0)
    monkeypatch.setattr(config, "MAX_NEW_JOBS", 5)  # Singapore 3, India 1, Canada 1
    monkeypatch.setattr(config, "MAX_CROWDED_PER_RUN", 0)
    _board_pool(run, Board("lever", "big"), [("BigCo", "ML Engineer", "Singapore")], postings=900)
    _board_pool(run, Board("lever", "in"), [(f"In{i}", "ML Engineer", "Bengaluru") for i in range(3)])
    india = [c.id for c in run.open_candidates("India")]
    # Singapore's only candidate is crowded and the cap is 0, so India may take the spare slots.
    assert call(tools, "add_jobs", {"jobs": [{"candidate_id": i, "why": "x"} for i in india]})["added"] == 3
