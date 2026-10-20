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
POSTING_CHARS = 12000

# USD per million tokens, for the cost estimate printed after a run (not billing).
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0),
          "claude-haiku-5-5": (0.10, 0.50)}
CACHE_READ = {"claude-opus-5-5": 0.05, "claude-sonnet-5-5": 0.05}  # share of input price
SEARCH_PRICE = 0.01


class TailorError(Exception):
    """A reply that can't be used (no tool call, refused, cut off)."""


# ---------------------------------------------------------------- tool schemas

_BULLETS = {"type": "array", "items": {
    "type": "object",
    "properties": {"from": {"type": "string", "description": "id of the source bullet"},
                   "text": {"type": "string"}},
    "required": ["from", "text"]}}

CV_TOOL = {
    "name": "submit_cv",
    "description": "Submit the complete tailored CV. Call exactly once.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "skills": {"type": "array", "items": {
                "type": "object",
                "properties": {"label": {"type": "string"},
                               "items": {"type": "array", "items": {"type": "string"}}},
                "required": ["label", "items"]}},
            "experience": {"type": "array", "items": {
                "type": "object", "properties": {"id": {"type": "string"}, "bullets": _BULLETS},
                "required": ["id", "bullets"]}},
            "projects": {"type": "array", "items": {
                "type": "object", "properties": {"id": {"type": "string"}, "bullets": _BULLETS},
                "required": ["id", "bullets"]}},
            "coursework": {"type": "array", "items": {"type": "string"}},
            "gaps": {"type": "array", "items": {"type": "string"},
                     "description": "Main posting requirements the source can't support"},
        },
        "required": ["summary", "skills", "experience", "projects", "coursework", "gaps"],
    },
}

REVIEW_TOOL = {
    "name": "submit_review",
    "description": "Submit the review. Call exactly once.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["ready", "revise"]},
            "grounding": {"type": "array", "items": {
                "type": "object",
                "properties": {"where": {"type": "string"}, "quote": {"type": "string"},
                               "problem": {"type": "string"}},
                "required": ["where", "problem"]}},
            "coverage": {"type": "array", "items": {
                "type": "object",
                "properties": {"requirement": {"type": "string"},
                               "source_bullet": {"type": "string"}},
                "required": ["requirement"]}},
            "emphasis": {"type": "array", "items": {
                "type": "object",
                "properties": {"where": {"type": "string"}, "suggestion": {"type": "string"}},
                "required": ["suggestion"]}},
            "overall": {"type": "string"},
        },
        "required": ["verdict", "grounding", "coverage", "emphasis", "overall"],
    },
}

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
    return json.dumps({k: v for k, v in cv.items() if k != "gaps"}, indent=1, ensure_ascii=False)


def draft_request(source: dict, posting: dict) -> dict:
    return {
        "model": DRAFT_MODEL, "max_tokens": 4096,
        "system": _system(prompts.DRAFTER, source),
        "tools": [CV_TOOL], "tool_choice": {"type": "tool", "name": "submit_cv"},
        "messages": [{"role": "user", "content": _posting_block(posting)}],
    }


def research_request(posting: dict) -> dict:
    excerpt = posting["text"][:2000]
    return {
        "model": REVIEW_MODEL, "max_tokens": 2000,
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
        "model": REVIEW_MODEL, "max_tokens": 3000,
        "system": _system(prompts.CRITIC, source),
        "tools": [REVIEW_TOOL], "tool_choice": {"type": "tool", "name": "submit_review"},
        "messages": [{"role": "user", "content": "\n\n".join(parts)}],
    }


def revise_request(source: dict, posting: dict, cv: dict, review: dict) -> dict:
    return {
        "model": DRAFT_MODEL, "max_tokens": 4096,
        "system": _system(prompts.DRAFTER, source),
        "tools": [CV_TOOL], "tool_choice": {"type": "tool", "name": "submit_cv"},
        "messages": [{"role": "user", "content": "\n\n".join([
            _posting_block(posting),
            f"<draft_cv>\n{_cv_json(cv)}\n</draft_cv>",
            f"<review>\n{json.dumps(review, indent=1, ensure_ascii=False)}\n</review>",
            prompts.REVISE])}],
    }


def repair_request(source: dict, posting: dict, cv: dict, problems: list) -> dict:
    listed = "\n".join(f"- {p.where}: {p.message}" for p in problems)
    return {
        "model": DRAFT_MODEL, "max_tokens": 4096,
        "system": _system(prompts.DRAFTER, source),
        "tools": [CV_TOOL], "tool_choice": {"type": "tool", "name": "submit_cv"},
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


def tool_input(message, name: str) -> dict:
    stop = field(message, "stop_reason")
    if stop == "max_tokens":
        raise TailorError("reply was cut off (max_tokens)")
    if stop == "refusal":
        raise TailorError("the model declined this request")
    for block in _blocks(message):
        if field(block, "type") == "tool_use" and field(block, "name") == name:
            data = field(block, "input")
            return dict(data) if data is not None else {}
    raise TailorError(f"reply did not call {name} (stop_reason: {stop})")


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


def cost(usages: list[dict]) -> float:
    """Rough USD at standard (not batch) prices."""
    total = 0.0
    for u in usages:
        model = next((m for m in PRICES if str(u.get("model", "")).startswith(m)), DRAFT_MODEL)
        pin, pout = PRICES[model]
        total += (u["input"] * pin + u["output"] * pout
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
