"""Devin API client (v3 organizations API) plus a simulated stand-in.

Docs: https://docs.devin.ai/api-reference/v3/sessions/post-organizations-sessions
      https://docs.devin.ai/api-reference/v3/sessions/get-organizations-session
"""

import asyncio
import uuid

import httpx

API_BASE = "https://api.devin.ai/v3/organizations"
MAX_RETRIES = 5


class DevinAPIError(Exception):
    pass


class DevinClient:
    def __init__(self, org_id: str, api_key: str, http: httpx.AsyncClient | None = None):
        self._base = f"{API_BASE}/{org_id}"
        self._http = http or httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"}, timeout=30
        )

    async def _request(self, method: str, path: str, **kwargs):
        """Retry with exponential backoff, honoring Retry-After.

        429 is retried for all methods (the request was rejected, not
        executed). 5xx and transport errors are retried for GET only: the
        sessions API has no idempotency keys, so retrying a POST could
        create a duplicate session server-side.
        """
        delay = 1.0
        resp = None
        for _ in range(MAX_RETRIES):
            try:
                resp = await self._http.request(
                    method, f"{self._base}{path}", **kwargs
                )
            except httpx.TransportError as exc:
                if method != "GET":
                    raise DevinAPIError(f"{method} {path} failed: {exc}") from exc
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if resp.status_code < 400:
                return resp.json()
            if resp.status_code == 429 or (
                method == "GET" and resp.status_code >= 500
            ):
                retry_after = resp.headers.get("retry-after")
                await asyncio.sleep(float(retry_after) if retry_after else delay)
                delay = min(delay * 2, 30)
                continue
            break
        raise DevinAPIError(
            f"{method} {path} failed after retries: "
            f"{resp.status_code if resp is not None else 'no response'} "
            f"{resp.text[:200] if resp is not None else ''}"
        )

    async def create_session(
        self,
        *,
        prompt: str,
        title: str,
        repos: list,
        tags: list,
        max_acu_limit: int,
        structured_output_schema: dict,
    ):
        return await self._request(
            "POST",
            "/sessions",
            json={
                "prompt": prompt,
                "title": title,
                "repos": repos,
                "tags": tags,
                "max_acu_limit": max_acu_limit,
                "structured_output_schema": structured_output_schema,
                "structured_output_required": True,
            },
        )

    async def get_session(self, session_id: str):
        return await self._request("GET", f"/sessions/{session_id}")


class SimulatedDevinClient:
    """Fake Devin API for SIMULATE mode. Sessions finish after a few polls."""

    POLLS_TO_FINISH = 2

    def __init__(self):
        self._sessions = {}
        self._planned_outcomes = {}

    def plan_outcome(self, issue_number: int, outcome: str) -> None:
        self._planned_outcomes[issue_number] = outcome

    async def create_session(
        self,
        *,
        prompt: str,
        title: str,
        repos: list,
        tags: list,
        max_acu_limit: int,
        structured_output_schema: dict,
    ):
        session_id = f"devin-sim-{uuid.uuid4().hex[:8]}"
        issue_number = int(
            next(t.split("-", 1)[1] for t in tags if t.startswith("issue-"))
        )
        self._sessions[session_id] = {"polls": 0, "issue_number": issue_number}
        return {
            "session_id": session_id,
            "url": f"https://app.devin.ai/sessions/{session_id}",
            "status": "running",
            "status_detail": "working",
            "acus_consumed": 0.0,
            "pull_requests": [],
            "tags": list(tags),
        }

    async def get_session(self, session_id: str):
        session = self._sessions[session_id]
        session["polls"] += 1
        url = f"https://app.devin.ai/sessions/{session_id}"
        if session["polls"] < self.POLLS_TO_FINISH:
            return {
                "session_id": session_id,
                "url": url,
                "status": "running",
                "status_detail": "working",
                "acus_consumed": 0.5 * session["polls"],
                "pull_requests": [],
            }
        issue_number = session["issue_number"]
        outcome = self._planned_outcomes.get(issue_number, "fixed")
        pr_url = (
            f"https://github.com/elen-dil/superset-ali/pull/{9000 + issue_number}"
            if outcome == "fixed"
            else None
        )
        return {
            "session_id": session_id,
            "url": url,
            "status": "running",
            "status_detail": "finished",
            "acus_consumed": 2.5,
            "pull_requests": [{"pr_url": pr_url}] if pr_url else [],
            "structured_output": {
                "outcome": outcome,
                "summary": f"[SIMULATED] outcome={outcome} for issue #{issue_number}",
                "root_cause": "[SIMULATED] see issue body",
                "files_changed": ["[SIMULATED] path/to/file.py"],
                "tests_run": 3,
                "tests_passed": 3,
                "risk_notes": "[SIMULATED] none",
                "blockers": (
                    ""
                    if outcome == "fixed"
                    else "[SIMULATED] requires a human decision"
                ),
            },
        }
