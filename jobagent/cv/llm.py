"""Messages API requests for tailoring, and parsing of their replies.

Every builder returns plain request parameters (a dict for `client.messages.create`).
The one-job command sends them directly; a batch run can submit the same dicts as
batch requests, so what is tested by hand is exactly what runs overnight.

Replies are read through `field()`, which accepts SDK objects and plain dicts alike,
so tests can use recorded JSON responses.
"""
from __future__ import annotations

import json
import os

import yaml

from . import content, prompts

DRAFT_MODEL = os.environ.get("TAILOR_DRAFT_MODEL", "claude-opus-5-5")
REVIEW_MODEL = os.environ.get("TAILOR_REVIEW_MODEL", "claude-sonnet-5-5")
RESEARCH_SEARCHES = 3
# Ceilings, not targets: only tokens actually written are billed. A review of a long
# posting overran 3,000 in the first live run.
DRAFT_MAX_TOKENS = 8000
REVIEW_MAX_TOKENS = 8000
RESEARCH_MAX_TOKENS = 3000
POSTING_CHARS = 12000

# USD per million tokens, for the cost estimate printed after a run (not billing).
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0),
          "claude-haiku-5-5": (0.10, 0.50)}
CACHE_READ = {"claude-opus-5-5": 0.05, "claude-sonnet-5-5": 0.05}  # share of input price
SEARCH_PRICE = 0.01


class TailorError(Exception):
    """A reply that can't be used (no tool call, refused, cut off)."""


# ---------------------------------------------------------------- tool schemas

def _obj(props: dict) -> dict:
    """Structured outputs need every object closed; listing every property as required
    keeps the shape exact (an optional field comes back as an empty string or list)."""
    return {"type": "object", "properties": props, "required": list(props),
            "additionalProperties": False}


_STR = {"type": "string"}
_STRS = {"type": "array", "items": _STR}
_BULLETS = {"type": "array", "items": _obj({
    "from": {"type": "string", "description": "id of the source bullet"}, "text": _STR})}
_ENTRIES = {"type": "array", "items": _obj({"id": _STR, "bullets": _BULLETS})}

CV_SCHEMA = _obj({
    "summary": _STR,
    "skills": {"type": "array", "items": _obj({"label": _STR, "items": _STRS})},
    "experience": _ENTRIES,
    "projects": _ENTRIES,
    "coursework": _STRS,
    "headline": {"type": "string",
                 "description": "One of the source's headline options, or empty for the default"},
    "section_order": {"type": "string", "enum": list(content.SECTION_ORDERS)},
    "gaps": {"type": "array", "items": _STR,
             "description": "Main posting requirements the source can't support"},
})

LETTER_DECISIONS = ("required", "invited", "not_needed")
# The first draft also decides whether the application needs a cover letter, so the
# decision costs no extra request. Revisions and repairs return the plain CV_SCHEMA.
DRAFT_SCHEMA = {**CV_SCHEMA, "properties": {**CV_SCHEMA["properties"], "cover_letter": _obj({
    "decision": {"type": "string", "enum": list(LETTER_DECISIONS)}, "reason": _STR})},
    "required": [*CV_SCHEMA["required"], "cover_letter"]}

LETTER_SCHEMA = _obj({
    "greeting": _STR,
    "paragraphs": {"type": "array", "items": _STR, "description": "The body, one string per paragraph"},
    "closing": _STR,
})

REVIEW_SCHEMA = _obj({
    "verdict": {"type": "string", "enum": ["ready", "revise"]},
    "grounding": {"type": "array", "items": _obj({"where": _STR, "quote": _STR, "problem": _STR})},
    "coverage": {"type": "array", "items": _obj({"requirement": _STR, "source_bullet": _STR})},
    "emphasis": {"type": "array", "items": _obj({"where": _STR, "suggestion": _STR})},
    "overall": _STR,
})


def _json_output(schema: dict) -> dict:
    # Opus 5.5 and Sonnet 5.5 reject forced tool use; structured outputs guarantee the shape.
    return {"format": {"type": "json_schema", "schema": schema}}


WEB_SEARCH = {"type": "web_search_20250305", "name": "web_search", "max_uses": RESEARCH_SEARCHES}


# ---------------------------------------------------------------- building requests

def source_text(source: dict) -> str:
    """The source as the model sees it: no contact details or layout settings."""
    return yaml.safe_dump({k: v for k, v in source.items() if k not in ("contact", "layout")},
                          sort_keys=False, allow_unicode=True, width=100)


def _system(template: str, source: dict, **extra) -> list[dict]:
    text = template.format(
        source=source_text(source), max_pages=(source.get("layout") or {}).get("max_pages", 1),
        max_bullets=content.MAX_BULLETS_PER_ENTRY, bullet_words=content.BULLET_MAX_WORDS,
        summary_words=content.SUMMARY_MAX_WORDS, max_searches=RESEARCH_SEARCHES, **extra)
    # Identical for every job, so the prefix is cached across a run.
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _posting_block(posting: dict) -> str:
    text = posting["text"][:POSTING_CHARS]
    return (f"<posting>\nCompany: {posting.get('company', '')}\nRole: {posting.get('role', '')}\n"
            f"URL: {posting.get('url', '')}\n\n{text}\n</posting>")


def _cv_json(cv: dict) -> str:
    return json.dumps({k: v for k, v in cv.items() if k not in ("gaps", "cover_letter")},
                      indent=1, ensure_ascii=False)


def _json(data: dict) -> str:
    return json.dumps(data, indent=1, ensure_ascii=False)


def draft_request(source: dict, posting: dict) -> dict:
    return {
        "model": DRAFT_MODEL, "max_tokens": DRAFT_MAX_TOKENS,
        "system": _system(prompts.DRAFTER, source),
        "output_config": _json_output(DRAFT_SCHEMA),
        "messages": [{"role": "user", "content": _posting_block(posting)}],
    }


def research_request(posting: dict) -> dict:
    excerpt = posting["text"][:2000]
    return {
        "model": REVIEW_MODEL, "max_tokens": RESEARCH_MAX_TOKENS,
        "system": prompts.RESEARCH.format(max_searches=RESEARCH_SEARCHES),
        "tools": [WEB_SEARCH],
        "messages": [{"role": "user", "content":
                      f"Company: {posting.get('company', '')}\nRole: {posting.get('role', '')}\n\n"
                      f"<posting_excerpt>\n{excerpt}\n</posting_excerpt>"}],
    }


def critique_request(source: dict, posting: dict, cv: dict, notes: str = "") -> dict:
    parts = [_posting_block(posting)]
    if notes:
        parts.append(f"<company_research>\n{notes}\n</company_research>")
    parts.append(f"<draft_cv>\n{_cv_json(cv)}\n</draft_cv>")
    return {
        "model": REVIEW_MODEL, "max_tokens": REVIEW_MAX_TOKENS,
        "system": _system(prompts.CRITIC, source),
        "output_config": _json_output(REVIEW_SCHEMA),
        "messages": [{"role": "user", "content": "\n\n".join(parts)}],
    }


def revise_request(source: dict, posting: dict, cv: dict, review: dict) -> dict:
    return {
        "model": DRAFT_MODEL, "max_tokens": DRAFT_MAX_TOKENS,
        "system": _system(prompts.DRAFTER, source),
        "output_config": _json_output(CV_SCHEMA),
        "messages": [{"role": "user", "content": "\n\n".join([
            _posting_block(posting),
            f"<draft_cv>\n{_cv_json(cv)}\n</draft_cv>",
            f"<review>\n{json.dumps(review, indent=1, ensure_ascii=False)}\n</review>",
            prompts.REVISE])}],
    }


def repair_request(source: dict, posting: dict, cv: dict, problems: list) -> dict:
    listed = "\n".join(f"- {p.where}: {p.message}" for p in problems)
    return {
        "model": DRAFT_MODEL, "max_tokens": DRAFT_MAX_TOKENS,
        "system": _system(prompts.DRAFTER, source),
        "output_config": _json_output(CV_SCHEMA),
        "messages": [{"role": "user", "content": "\n\n".join([
            _posting_block(posting),
            f"<draft_cv>\n{_cv_json(cv)}\n</draft_cv>",
            prompts.REPAIR.format(problems=listed)])}],
    }


# ---------------------------------------------------------------- reading replies

def field(obj, name, default=None):
    """Attribute or key access, so SDK objects and recorded JSON both work."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _blocks(message) -> list:
    return field(message, "content") or []


def reply_json(message) -> dict:
    """The JSON object a structured-output reply carries in its text."""
    stop = field(message, "stop_reason")
    if stop == "max_tokens":
        raise TailorError("reply was cut off (max_tokens)")
    if stop == "refusal":
        raise TailorError("the model declined this request")
    text = text_of(message)
    try:
        data = json.loads(text)
    except ValueError:
        raise TailorError(f"reply was not JSON (stop_reason: {stop}): {text[:120]!r}")
    if not isinstance(data, dict):
        raise TailorError("reply JSON was not an object")
    return data


def text_of(message) -> str:
    return "\n".join(field(b, "text") or "" for b in _blocks(message)
                     if field(b, "type") == "text").strip()


def usage_of(message) -> dict:
    u = field(message, "usage") or {}
    server = field(u, "server_tool_use") or {}
    return {"model": field(message, "model", ""),
            "input": field(u, "input_tokens", 0) or 0,
            "output": field(u, "output_tokens", 0) or 0,
            "cache_read": field(u, "cache_read_input_tokens", 0) or 0,
            "cache_write": field(u, "cache_creation_input_tokens", 0) or 0,
            "searches": field(server, "web_search_requests", 0) or 0}


def cost(usages: list[dict], batch: bool = False) -> float:
    """Rough USD. Batch requests pay half for tokens; web searches cost the same."""
    total = 0.0
    token_share = 0.5 if batch else 1.0
    for u in usages:
        model = next((m for m in PRICES if str(u.get("model", "")).startswith(m)), DRAFT_MODEL)
        pin, pout = PRICES[model]
        total += token_share * (u["input"] * pin + u["output"] * pout
                                + u["cache_read"] * pin * CACHE_READ.get(model, 0.1)
                                + u["cache_write"] * pin * 1.25) / 1e6
        total += u["searches"] * SEARCH_PRICE
    return total


def send(client, params: dict, max_continues: int = 3) -> tuple[list, list[dict]]:
    """One request, continuing a paused server-tool turn (web search) if needed.

    Returns every reply in order (the last one is final) and their usage.
    """
    message = client.messages.create(**params)
    replies = [message]
    for _ in range(max_continues):
        if field(message, "stop_reason") != "pause_turn":
            break
        params = {**params, "messages": [*params["messages"],
                                         {"role": "assistant", "content": _blocks(message)}]}
        message = client.messages.create(**params)
        replies.append(message)
    return replies, [usage_of(m) for m in replies]


# ---------------------------------------------------------------- cover letters

def _letter_system(template: str, source: dict) -> list[dict]:
    from . import letter
    return _system(template, source, min_words=letter.MIN_WORDS, max_words=letter.MAX_WORDS)


def _letter_context(posting: dict, cv: dict, notes: str) -> list[str]:
    parts = [_posting_block(posting)]
    if notes:
        parts.append(f"<company_research>\n{notes}\n</company_research>")
    parts.append(f"<tailored_cv>\n{_cv_json(cv)}\n</tailored_cv>")
    return parts


def letter_request(source: dict, posting: dict, cv: dict, notes: str = "", why: str = "") -> dict:
    parts = _letter_context(posting, cv, notes)
    if why:
        parts.append(f"Why this application has a letter: {why}")
    return {
        "model": DRAFT_MODEL, "max_tokens": DRAFT_MAX_TOKENS,
        "system": _letter_system(prompts.LETTER_WRITER, source),
        "output_config": _json_output(LETTER_SCHEMA),
        "messages": [{"role": "user", "content": "\n\n".join(parts)}],
    }


def letter_critique_request(source: dict, posting: dict, cv: dict, letter: dict, notes: str = "") -> dict:
    parts = _letter_context(posting, cv, notes) + [f"<cover_letter>\n{_json(letter)}\n</cover_letter>"]
    return {
        "model": REVIEW_MODEL, "max_tokens": REVIEW_MAX_TOKENS,
        "system": _letter_system(prompts.LETTER_CRITIC, source),
        "output_config": _json_output(REVIEW_SCHEMA),
        "messages": [{"role": "user", "content": "\n\n".join(parts)}],
    }


def letter_revise_request(source: dict, posting: dict, cv: dict, letter: dict, review: dict,
                          notes: str = "") -> dict:
    parts = _letter_context(posting, cv, notes) + [
        f"<cover_letter>\n{_json(letter)}\n</cover_letter>",
        f"<review>\n{_json(review)}\n</review>", prompts.LETTER_REVISE]
    return {
        "model": DRAFT_MODEL, "max_tokens": DRAFT_MAX_TOKENS,
        "system": _letter_system(prompts.LETTER_WRITER, source),
        "output_config": _json_output(LETTER_SCHEMA),
        "messages": [{"role": "user", "content": "\n\n".join(parts)}],
    }


def letter_repair_request(source: dict, posting: dict, cv: dict, letter: dict, problems: list,
                          notes: str = "") -> dict:
    listed = "\n".join(f"- {p.where}: {p.message}" for p in problems)
    parts = _letter_context(posting, cv, notes) + [
        f"<cover_letter>\n{_json(letter)}\n</cover_letter>", prompts.LETTER_REPAIR.format(problems=listed)]
    return {
        "model": DRAFT_MODEL, "max_tokens": DRAFT_MAX_TOKENS,
        "system": _letter_system(prompts.LETTER_WRITER, source),
        "output_config": _json_output(LETTER_SCHEMA),
        "messages": [{"role": "user", "content": "\n\n".join(parts)}],
    }
