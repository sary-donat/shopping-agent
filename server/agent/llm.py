"""The single place that talks to Claude.
  ask()    -> a validated Pydantic object (structured output)
  search() -> a text answer based on a web search, plus the URLs that the search really returned
"""
from functools import cache
from typing import TypeVar

import anthropic
from langsmith import traceable
from pydantic import BaseModel

from config import MODEL

T = TypeVar("T", bound=BaseModel)

# Models that support server-side refusal fallbacks and the newer web search tool (Haiku does not)
NEWER_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}


@cache
def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment / .env


def _send(method: str, **request):
    """Call messages.parse / messages.create, with refusal fallbacks where the model supports them."""
    request = {"model": MODEL, "max_tokens": 16000, **request}
    if MODEL in NEWER_MODELS:
        api = _client().beta.messages
        request |= {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    else:
        api = _client().messages
    response = getattr(api, method)(**request)
    if response.stop_reason == "refusal":
        raise RuntimeError("The model declined this request.")
    return response


@traceable(run_type="llm", name="claude")
def ask(system: str, user: str, schema: type[T]) -> T:
    response = _send("parse", system=system, messages=[{"role": "user", "content": user}], output_format=schema)
    return response.parsed_output


@traceable(run_type="llm", name="claude-web-search")
def search(system: str, user: str, max_searches: int = 2) -> tuple[str, list[str]]:
    tool_type = "web_search_20260209" if MODEL in NEWER_MODELS else "web_search_20250305"
    tools = [{"type": tool_type, "name": "web_search", "max_uses": max_searches}]
    messages = [{"role": "user", "content": user}]

    response = _send("create", system=system, messages=messages, tools=tools)
    blocks = list(response.content)
    for _ in range(3):  # the server pauses long searches; send the turn back to let it continue
        if response.stop_reason != "pause_turn":
            break
        response = _send("create", system=system, tools=tools,
                         messages=[messages[0], {"role": "assistant", "content": blocks}])
        blocks += response.content

    text = "".join(b.text for b in blocks if b.type == "text")
    urls = [
        r.url
        for b in blocks if b.type == "web_search_tool_result" and isinstance(b.content, list)
        for r in b.content
    ]
    return text, urls
