from jobagent import filters
from jobagent.ats import mycareersfuture as mcf
from jobagent.harvest import to_candidate
from jobagent.registry import Registry
from tests.fixtures import payloads as P


def test_company_names_are_cleaned():
    assert mcf.company_name("CFGI SINGAPORE PTE. LTD.") == "CFGI Singapore"
    assert mcf.company_name("Grab Holdings Pte Ltd") == "Grab Holdings"


def test_search_parse_and_agency_note():
    cfgi, agency = mcf.parse(P.MCF_SEARCH)
    assert (cfgi.company, cfgi.location, cfgi.posted) == ("CFGI Singapore", "Singapore (Raffles Place)", "2026-10-14")
    assert cfgi.compensation == "SGD 6,000–9,000 per month"
    assert cfgi.url.endswith("abc123") and cfgi.needs_detail
    assert filters.market_of(cfgi.location) == "Singapore"
    assert "recruitment agency" in agency.extra["note"]
    assert agency.location == "Singapore"


def test_detail_adds_applicants_and_experience():
    p = mcf.parse(P.MCF_SEARCH)[0]
    mcf.parse_detail(P.MCF_DETAIL, p)
    c = to_candidate(p, mcf.BOARD)
    assert c.applicants == 7 and c.summary()["applicants_so_far"] == 7
    assert filters.experience_range(c.experience_hints) == "3+ yrs"
    assert "LLM tools" in c.description and not p.extra.get("closed")
    mcf.parse_detail({"status": {"jobStatus": "Closed"}}, p)
    assert p.extra["closed"]


def test_registry_always_includes_mcf(tmp_path, monkeypatch):
    from jobagent import config
    monkeypatch.setattr(config, "BOARDS_CSV", tmp_path / "boards.csv")
    assert "mycareersfuture:sg" in Registry.load().entries
