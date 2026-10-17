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
    assert agency.extra["agency"] and "note" not in agency.extra
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


def _job(posted_by, hiring=None, uuid="u1"):
    job = {"uuid": uuid, "title": "AI Engineer", "postedCompany": {"name": posted_by},
           "metadata": {"newPostingDate": "2026-10-16"}, "address": {}}
    if hiring:
        job["hiringCompany"] = {"name": hiring}
    return mcf.parse({"results": [job]})[0]


def test_agency_without_named_employer_is_dropped():
    p = _job("RANDSTAD PTE. LIMITED")
    mcf.parse_detail({"description": "<p>Our client is a leading bank.</p>"}, p)
    assert p.extra["drop"] == "recruitment agency, employer not named"


def test_licence_number_marks_an_agency():
    p = _job("3X SOLUTION PTE. LTD.")
    mcf.parse_detail({"description": "<p>Great role.</p><p>EA Licence No. 16C8209 | Reg No: R1872345</p>"}, p)
    assert p.extra.get("drop")


def test_agency_naming_the_employer_is_kept_under_the_employer():
    p = _job("RECRUIT EXPRESS PTE LTD", hiring="DBS BANK LTD.")
    mcf.parse_detail({"description": "<p>EA Licence No. 99C4599</p>"}, p)
    assert not p.extra.get("drop")
    assert p.company == "DBS Bank" and "on behalf of DBS Bank" in p.extra["note"]


def test_direct_employer_with_job_id_is_kept():
    p = _job("CFGI SINGAPORE PTE. LTD.")
    mcf.parse_detail({"description": "<p>Job ID: R1234567. Build LLM tools.</p>"}, p)
    assert not p.extra.get("drop")
