from datetime import date

import pytest

from jobagent import config, filters, store
from jobagent.ats import Board, Posting
from jobagent.harvest import prefilter, rank, to_candidate


@pytest.mark.parametrize("loc,market", [
    ("Bangalore, Karnataka, India", "India"), ("Noida", "India"), ("Toronto, ON", "Canada"),
    ("Remote - Canada", "Canada"), ("NTU Main Campus, Singapore", "Singapore"),
    ("Sunnyvale, CA", "United States"), ("Remote - US (open to Canada)", "Canada"),
    ("London, UK", "Other"), ("Berlin / Remote", "Other"), ("London; Bengaluru", "India"),
    ("Remote", "Remote (unspecified)"), ("", "Unknown"), ("HQ", "Unknown"),
    ("Toronto / Singapore", "Singapore"), ("Singapore / Bengaluru", "India"),
    ("Singapore (D08 Little India, Farrer Park)", "Singapore"),
])
def test_market_of(loc, market):
    assert filters.market_of(loc) == market


def test_market_priority():
    r = filters.market_rank
    assert r("India") == r("Singapore") < r("Canada") < r("Unknown") < r("United States")
    assert filters.MARKET_ORDER[:3] == ["India", "Singapore", "Canada"]
    assert filters.MARKET_ORDER[-1] == "United States"


@pytest.mark.parametrize("hint,expected", [
    ("You have 5-10 years of professional experience", "5–10 yrs"),
    ("8+ years of industry experience required", "8+ yrs"),
    ("at least 3 years working with PyTorch", "3+ yrs"),
])
def test_experience_range(hint, expected):
    assert filters.experience_range([hint]) == expected
    assert filters.experience_range([]) == ""


@pytest.mark.parametrize("title,ok", [
    ("Machine Learning Engineer", True), ("Forward Deployed Engineer", True),
    ("Member of Technical Staff", True), ("Research Scientist, Evals", True),
    ("Senior Software Engineer, Backend", True), ("Solutions Architect", True),
    ("Software Engineering Intern", False), ("Account Executive, AI", False),
    ("Technical Recruiter", False), ("Data Analyst", False), ("Product Manager, AI", False),
    ("Terminal Operations Manager", False), ("Office Manager", False),
    ("Staff Machine Learning Manager", True), ("Data & AI - Manager", True),  # left to the model
    ("Director, Fleet Solutions", False), ("Manager, Software Engineering", False),
    ("Lead Salesforce Developer, GTM Systems", False), ("AI Success Manager (US East)", False),
    ("Content Systems Engineer, Technical Documentation", False), ("Engineering Manager II", False),
    ("Lead Site Reliability Engineer", True), ("Staff Engineer I", True), ("Applied AI Architect", True),
    ("Research Fellow (Organic Chemistry)", False), ("Part-time Lecturer, EEE", False),
    ("Project Officer (Engineering)", False), ("Senior Assistant Manager, Research Development", False),
    ("Research Engineer (Robotics)", True), ("Research Scientist, Evals", True),
])
def test_title_ok(title, ok):
    assert filters.title_ok(title) is ok


def test_experience_and_phd_hints():
    text = "We were founded 10 years ago. You bring 7+ years of industry experience. PhD required."
    hints = filters.experience_hints(text)
    assert len(hints) == 1 and "7+ years of industry experience" in hints[0]
    assert "PhD required" in filters.phd_hint(text)


def test_stale_flag_respects_future_dates():
    today = date.today()
    old = date(today.year - 1, 1, 1).isoformat()
    b = Board("greenhouse", "x", "X")
    stale = to_candidate(Posting("greenhouse", "x", "X", "1", "ML Engineer", "https://a/1",
                                 posted=old, description="Join us."), b)
    intake = to_candidate(Posting("greenhouse", "x", "X", "2", "ML Engineer", "https://a/2", posted=old,
                                  description=f"Starts January {today.year + 1}."), b)
    boilerplate = to_candidate(Posting("greenhouse", "x", "X", "3", "ML Engineer", "https://a/3", posted=old,
                                       description=f"Named to the Forbes AI 50 in {today.year}. Join us."), b)
    assert stale.looks_stale and not intake.looks_stale and boilerplate.looks_stale


def test_prefilter_and_rank():
    def p(i, title, loc, posted=""):
        return Posting("lever", "x", "X", str(i), title, f"https://jobs.lever.co/x/{i}", location=loc, posted=posted)
    posts = [p(1, "ML Engineer", "San Francisco, CA"), p(2, "ML Engineer", "Bengaluru, India"),
             p(3, "Sales Engineer Manager", "Mumbai"), p(4, "ML Engineer", "London"),
             p(5, "AI Engineer", "Toronto, ON"), p(6, "AI Engineer", "Mumbai", "2026-01-01")]
    known = {store.job_key("https://jobs.lever.co/x/6")}
    kept = prefilter(posts, known)
    assert [k.job_id for k in kept] == ["1", "2", "5"]
    ranked = rank([to_candidate(k, Board("lever", "x")) for k in kept])
    assert [c.market for c in ranked] == ["India", "Canada", "United States"]


def test_job_key_normalises():
    a = store.job_key("https://www.Example.com/careers/123/?utm_source=x&gh_jid=99")
    b = store.job_key("https://example.com/careers/123?gh_jid=99")
    c = store.job_key("https://example.com/careers/123?gh_jid=100")
    assert a == b != c
    assert store.job_key("https://jobs.lever.co/x/1/apply") == store.job_key("https://jobs.lever.co/x/1")


def test_append_dedupes_and_marks_seen(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_CSV", tmp_path / "jobs.csv")
    monkeypatch.setattr(config, "SEEN_CSV", tmp_path / "seen.csv")
    job = {"company": "Acme", "role": "ML Engineer", "location": "Pune", "url": "https://a.com/1", "why": "x"}
    assert store.append_jobs([job, dict(job, url="https://a.com/1?utm=2")])["added"] == 1
    assert store.append_jobs([dict(job, url="https://a.com/other")])["added"] == 0  # same company+role+location
    assert store.job_key("https://a.com/1") in store.known_keys()
    store.mark_seen([{"url": "https://b.com/2", "reason": "7+ years"}])
    assert store.job_key("https://b.com/2") in store.known_keys()


def test_old_tracker_is_upgraded_in_place(tmp_path, monkeypatch):
    """A jobs.csv from before the experience column keeps its rows, edits and extra columns."""
    path = tmp_path / "jobs.csv"
    monkeypatch.setattr(config, "JOBS_CSV", path)
    monkeypatch.setattr(config, "SEEN_CSV", tmp_path / "seen.csv")
    old = ["date_added", "company", "role", "location", "market", "posted", "deadline_or_start",
           "compensation", "url", "why", "status", "applied", "my_notes"]
    path.write_text(",".join(old) + "\n2026-09-20,Acme,ML Engineer,Pune,India,,,,https://a.com/1,fits,"
                    "interviewing,Yes,called recruiter\n", encoding="utf-8")
    store.append_jobs([{"company": "Beta", "role": "AI Engineer", "location": "Singapore",
                        "url": "https://b.com/1", "why": "x", "experience": "5–10 yrs"}])
    rows = store.read_jobs()
    assert list(rows[0]) == store.COLUMNS + ["my_notes"]
    assert (rows[0]["status"], rows[0]["applied"], rows[0]["my_notes"], rows[0]["experience"]) == (
        "interviewing", "Yes", "called recruiter", "")
    assert (rows[1]["company"], rows[1]["experience"], rows[1]["my_notes"]) == ("Beta", "5–10 yrs", "")


def test_fit_score_and_ranking(tmp_path, monkeypatch):
    rules = tmp_path / "fit.txt"
    rules.write_text("# comment\n3 computer vision|perception\n2 c\\+\\+\nbad line\n1 pytorch\n", encoding="utf-8")
    monkeypatch.setattr(config, "FIT_KEYWORDS_PATH", rules)
    monkeypatch.setattr(filters, "_FIT_RULES", None)
    score, terms = filters.fit_score("Computer Vision Engineer", "C++ and PyTorch.")
    assert score == 3 * 3 + 2 + 1 and terms == ["computer vision", "c++", "pytorch"]

    b = Board("lever", "x", "X")
    today = date.today()
    # Both fresh: the stronger match comes first even though it is a few days older.
    fresher_weak = to_candidate(Posting("lever", "x", "X", "1", "Backend Engineer", "https://a/1",
                                        location="Bengaluru", posted=today.isoformat()), b)
    older_strong = to_candidate(Posting("lever", "x", "X", "2", "Computer Vision Engineer", "https://a/2",
                                        location="Bengaluru",
                                        posted=date.fromordinal(today.toordinal() - 5).isoformat(),
                                        description="C++ and PyTorch"), b)
    assert rank([fresher_weak, older_strong])[0].id.endswith(":2")
    monkeypatch.setattr(filters, "_FIT_RULES", None)


def test_no_fit_file_means_no_reordering(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FIT_KEYWORDS_PATH", tmp_path / "missing.txt")
    monkeypatch.setattr(filters, "_FIT_RULES", None)
    assert filters.fit_score("Computer Vision Engineer", "") == (0, [])
    monkeypatch.setattr(filters, "_FIT_RULES", None)


def test_rank_alternates_fit_and_freshness(tmp_path, monkeypatch):
    rules = tmp_path / "fit.txt"
    rules.write_text("3 computer vision\n", encoding="utf-8")
    monkeypatch.setattr(config, "FIT_KEYWORDS_PATH", rules)
    monkeypatch.setattr(filters, "_FIT_RULES", None)
    b = Board("lever", "x", "X")
    today = date.today().toordinal()
    def cand(i, title, days_old):
        posted = date.fromordinal(today - days_old).isoformat()
        return to_candidate(Posting("lever", "x", "X", str(i), title, f"https://a/{i}",
                                    location="Bengaluru", posted=posted), b)
    # All in the recent tier (8-30 days): strong matches and fresher generic roles alternate.
    strong = [cand(i, "Computer Vision Engineer", 20 + i) for i in range(3)]
    generic = [cand(10 + i, "AI Engineer", 9 + i) for i in range(3)]  # fresher, no keyword match
    order = [c.title for c in rank(strong + generic)]
    assert order[:4] == ["Computer Vision Engineer", "AI Engineer", "Computer Vision Engineer", "AI Engineer"]
    assert len(order) == 6
    monkeypatch.setattr(filters, "_FIT_RULES", None)


def _ranked(tmp_path, monkeypatch, specs):
    """specs: (id, title, days_old or None, posted_note). Returns ids in rank order."""
    rules = tmp_path / "fit.txt"
    rules.write_text("3 computer vision\n", encoding="utf-8")
    monkeypatch.setattr(config, "FIT_KEYWORDS_PATH", rules)
    monkeypatch.setattr(filters, "_FIT_RULES", None)
    b = Board("lever", "x", "X")
    today = date.today().toordinal()
    cands = []
    for i, title, days, note in specs:
        posted = date.fromordinal(today - days).isoformat() if days is not None else ""
        cands.append(to_candidate(Posting("lever", "x", "X", i, title, f"https://a/{i}", location="Bengaluru",
                                          posted=posted, posted_note=note), b))
    order = [c.id.rsplit(":", 1)[-1] for c in rank(cands)]
    monkeypatch.setattr(filters, "_FIT_RULES", None)
    return order, cands


def test_fresh_postings_come_first_then_recent_then_old(tmp_path, monkeypatch):
    order, _ = _ranked(tmp_path, monkeypatch, [
        ("old_strong", "Computer Vision Engineer", 45, ""),
        ("recent_strong", "Computer Vision Engineer", 12, ""),
        ("fresh_generic", "AI Engineer", 6, ""),
        ("fresh_strong", "Computer Vision Engineer", 2, ""),
    ])
    assert order == ["fresh_strong", "fresh_generic", "recent_strong", "old_strong"]


def test_undated_postings_rank_as_recent_unless_the_board_says_old(tmp_path, monkeypatch):
    from jobagent.harvest import OLD, RECENT, freshness
    order, cands = _ranked(tmp_path, monkeypatch, [
        ("workday_old", "AI Engineer", None, "30+ days ago"),
        ("undated", "AI Engineer", None, ""),
        ("old", "AI Engineer", 40, ""),
        ("fresh", "AI Engineer", 1, ""),
    ])
    tiers = {c.id.rsplit(":", 1)[-1]: freshness(c) for c in cands}
    assert tiers["undated"] == RECENT and tiers["workday_old"] == OLD
    assert order[:2] == ["fresh", "undated"]


def test_stale_postings_rank_with_the_old(tmp_path, monkeypatch):
    from jobagent.harvest import OLD, freshness
    _, cands = _ranked(tmp_path, monkeypatch, [("stale", "AI Engineer", 120, "")])
    assert cands[0].looks_stale and freshness(cands[0]) == OLD


def test_summary_marks_fresh_postings(tmp_path, monkeypatch):
    _, cands = _ranked(tmp_path, monkeypatch, [("a", "AI Engineer", 3, ""), ("b", "AI Engineer", 10, "")])
    assert cands[0].summary()["fresh"] is True and "fresh" not in cands[1].summary()


def test_market_still_comes_before_freshness(tmp_path, monkeypatch):
    b = Board("lever", "x", "X")
    today = date.today()
    us_fresh = to_candidate(Posting("lever", "x", "X", "us", "AI Engineer", "https://a/us",
                                    location="Seattle, WA", posted=today.isoformat()), b)
    india_old = to_candidate(Posting("lever", "x", "X", "in", "AI Engineer", "https://a/in", location="Pune",
                                     posted=date.fromordinal(today.toordinal() - 40).isoformat()), b)
    assert [c.market for c in rank([us_fresh, india_old])] == ["India", "United States"]
