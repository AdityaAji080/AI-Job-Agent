import asyncio

import httpx

from jobagent.discover import probe, slug_variants
from tests.fixtures import payloads as P


def test_slug_variants():
    assert slug_variants("Sarvam AI") == ["sarvamai", "sarvam-ai", "sarvam"]
    assert slug_variants("Qure.ai") == ["qureai", "qure-ai", "qure"]
    assert slug_variants("Applied Intuition")[:2] == ["appliedintuition", "applied-intuition"]
    assert "open" not in slug_variants("Open Robotics")  # short first words collide
    assert slug_variants("") == []


def test_probe_finds_only_boards_with_postings():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://api.lever.co/v0/postings/sarvam?"):
            return httpx.Response(200, json=P.LEVER)
        if url == "https://api.ashbyhq.com/posting-api/job-board/sarvamai":
            return httpx.Response(200, json={"jobs": []})  # exists but empty: not a hit
        if "smartrecruiters" in url:
            return httpx.Response(200, json={"totalFound": 0, "content": []})
        return httpx.Response(404, json={})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await probe(["Sarvam AI", "Nobody Corp"], client=client,
                               known_keys={"greenhouse:sarvamai"})

    out = asyncio.run(go())
    hits = out["found"]["Sarvam AI"]
    assert [(h["system"], h["slug"], h["board_url"]) for h in hits] == [
        ("lever", "sarvam", "https://jobs.lever.co/sarvam")]
    assert hits[0]["postings"] == 1 and "Forward Deployed" in hits[0]["sample"][0]
    assert out["already_registered"] == {"Sarvam AI": ["greenhouse:sarvamai"]}
    assert out["not_found"] == ["Nobody Corp"]
