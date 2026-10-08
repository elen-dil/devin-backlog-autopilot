import asyncio

import httpx

from app.devin import DevinClient

SESSION = {
    "session_id": "devin-abc",
    "url": "https://app.devin.ai/sessions/devin-abc",
    "status": "running",
    "status_detail": "working",
    "acus_consumed": 0,
    "pull_requests": [],
    "tags": ["autopilot", "issue-42"],
}

KWARGS = dict(
    prompt="p",
    title="t",
    repos=["elen-dil/superset-ali"],
    tags=["autopilot", "issue-42"],
    max_acu_limit=1,
    structured_output_schema={},
)


def run(coro):
    return asyncio.run(coro)


def _client(handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return DevinClient("org-1", "key", http=http)


def test_adopts_existing_session_after_ambiguous_5xx():
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(1)
            return httpx.Response(500, json={"detail": "boom"})
        return httpx.Response(200, json={"items": [SESSION]})

    result = run(_client(handler).create_session(**KWARGS))
    assert result["session_id"] == "devin-abc"
    assert len(posts) == 1  # adopted via dedup lookup, POST never retried


def test_retries_post_when_dedup_finds_nothing():
    calls = {"post": 0}

    def handler(request):
        if request.method == "POST":
            calls["post"] += 1
            if calls["post"] == 1:
                return httpx.Response(500, json={"detail": "boom"})
            return httpx.Response(200, json=SESSION)
        return httpx.Response(200, json={"items": []})

    result = run(_client(handler).create_session(**KWARGS))
    assert result["session_id"] == "devin-abc"
    assert calls["post"] == 2


def test_429_retried_without_dedup_lookup():
    gets = []

    def handler(request):
        if request.method == "GET":
            gets.append(1)
            return httpx.Response(200, json={"items": []})
        if len(gets) == 0 and not getattr(handler, "posted", False):
            handler.posted = True
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=SESSION)

    result = run(_client(handler).create_session(**KWARGS))
    assert result["session_id"] == "devin-abc"
    assert not gets  # a rejected POST does not trigger a dedup query


def test_ignores_sessions_missing_our_tag():
    other = {**SESSION, "session_id": "devin-old", "tags": ["issue-99"]}

    def handler(request):
        if request.method == "POST":
            if getattr(handler, "n", 0) == 0:
                handler.n = 1
                return httpx.Response(500, json={"detail": "boom"})
            return httpx.Response(200, json=SESSION)
        # The tag filter being ignored must not cause a wrong adoption.
        return httpx.Response(200, json={"items": [other]})

    result = run(_client(handler).create_session(**KWARGS))
    assert result["session_id"] == "devin-abc"
