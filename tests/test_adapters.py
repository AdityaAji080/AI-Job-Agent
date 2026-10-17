import pytest

from jobagent.ats import (Board, ashby, detect_board, greenhouse, lever, recruitee, smartrecruiters,
                          unsupported_system, workable, workday)
from tests.fixtures import payloads as P


@pytest.mark.parametrize("url,ats,slug", [
    ("https://job-boards.greenhouse.io/torcrobotics/jobs/8651349002", "greenhouse", "torcrobotics"),
    ("https://boards.greenhouse.io/embed/job_board?for=gleanwork", "greenhouse", "gleanwork"),
    ("https://boards-api.greenhouse.io/v1/boards/anthropic/jobs", "greenhouse", "anthropic"),
    ("https://jobs.lever.co/palantir/dab396d4-2f14", "lever", "palantir"),
    ("https://jobs.eu.lever.co/acme/123", "lever", "eu/acme"),
    ("https://jobs.ashbyhq.com/realitydefender/364ad643", "ashby", "realitydefender"),
    ("https://jobs.smartrecruiters.com/BoschGroup/744000012345-cv", "smartrecruiters", "BoschGroup"),
    ("https://apply.workable.com/acme-ai/j/ABC123/", "workable", "acme-ai"),
    ("https://acme.recruitee.com/o/ml-engineer", "recruitee", "acme"),
    ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/x_JR1",
     "workday", "nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"),
    ("https://bb.wd3.myworkdayjobs.com/BlackBerry", "workday", "bb.wd3.myworkdayjobs.com/BlackBerry"),
])
def test_detect_board(url, ats, slug):
    board = detect_board(url)
    assert board is not None and (board.ats, board.slug) == (ats, slug)


@pytest.mark.parametrize("url", ["https://www.linkedin.com/jobs/view/1", "https://sarvam.ai/careers/jobs/2d72",
                                 "https://www.naukri.com/job-listings-x", "https://apply.workable.com/j/ABC123"])
def test_detect_board_rejects_non_boards(url):
    assert detect_board(url) is None


def test_unsupported_systems_are_named():
    assert unsupported_system("https://razorpay.darwinbox.in/ms/candidate/careers") == "darwinbox"
    assert unsupported_system("https://acme.keka.com/careers/") == "keka"
    assert unsupported_system("https://jobs.lever.co/x") is None


def test_greenhouse_parse():
    jobs = greenhouse.parse(P.GREENHOUSE, Board("greenhouse", "torcrobotics", "Torc Robotics"))
    j = jobs[0]
    assert (j.title, j.location, j.posted, j.company) == (
        "ML Engineer, II - Perception", "Toronto, ON", "2026-09-20", "Torc Robotics")
    assert "2+ years of experience" in j.description and "<" not in j.description


def test_lever_parse():
    j = lever.parse(P.LEVER, Board("lever", "palantir", "Palantir"))[0]
    assert j.location == "New York, NY / Toronto, ON"
    assert j.compensation.startswith("USD 135,000–200,000")
    assert "1+ years of industry experience" in j.description
    assert j.posted == "2025-09-16"


def test_ashby_parse_skips_unlisted():
    jobs = ashby.parse(P.ASHBY, Board("ashby", "realitydefender"))
    assert len(jobs) == 1
    assert jobs[0].location == "New York / Remote - US" and jobs[0].compensation == "$150K - $190K"


def test_smartrecruiters_list_then_detail():
    j = smartrecruiters.parse(P.SMARTRECRUITERS_LIST, Board("smartrecruiters", "BoschGroup"))[0]
    assert j.needs_detail and j.location == "Bengaluru, KA, India"
    smartrecruiters.parse_detail(P.SMARTRECRUITERS_DETAIL, j)
    assert "3-5 years" in j.description and j.url.endswith("computer-vision-engineer")


def test_workable_parse():
    j = workable.parse(P.WORKABLE, Board("workable", "acme-ai"))[0]
    assert j.company == "Acme AI" and j.location == "Mumbai, MH, India (remote)" and j.job_id == "ABC123"


def test_recruitee_parse():
    j = recruitee.parse(P.RECRUITEE, Board("recruitee", "acme"))[0]
    assert j.compensation == "SGD 6,000–9,000 per month" and "2+ years" in j.description


def test_workday_parse_and_detail():
    board = Board("workday", "nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite", "NVIDIA")
    jobs = workday.parse(P.WORKDAY_LIST, board)
    assert jobs[0].url == ("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
                           "/job/India-Bengaluru/Senior-DL-Engineer_JR1999")
    assert jobs[0].posted and jobs[1].posted == "" and jobs[1].posted_note == "30+ days ago"
    workday.parse_detail(P.WORKDAY_DETAIL, jobs[1])
    assert jobs[1].posted == "2026-10-13" and "CUDA" in jobs[1].description
