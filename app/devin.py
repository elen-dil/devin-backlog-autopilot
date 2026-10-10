"""Devin API client (v3 organizations API) plus a simulated stand-in.

Docs: https://docs.devin.ai/api-reference/v3/sessions/post-organizations-sessions
      https://docs.devin.ai/api-reference/v3/sessions/get-organizations-session
"""

import asyncio
import json
import time
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
        """POST /sessions, deduplicating on ambiguous failures.

        The API has no idempotency keys, so a 5xx or transport error leaves
        the request's fate unknown. Before retrying we look for a session
        carrying our `issue-<n>` tag created after this attempt began and
        adopt it if found, so a retried POST can never double a session.
        """
        body = {
            "prompt": prompt,
            "title": title,
            "repos": repos,
            "tags": tags,
            "max_acu_limit": max_acu_limit,
            "structured_output_schema": structured_output_schema,
            "structured_output_required": True,
        }
        issue_tag = next((t for t in tags if t.startswith("issue-")), None)
        attempt_started = int(time.time()) - 60  # clock-skew buffer
        delay = 1.0
        last_error = "unknown"
        for _ in range(MAX_RETRIES):
            try:
                resp = await self._http.post(f"{self._base}/sessions", json=body)
            except httpx.TransportError as exc:
                last_error = str(exc)
            else:
                if resp.status_code < 400:
                    return resp.json()
                if resp.status_code == 429:
                    # Rejected, not executed: safe to retry immediately.
                    retry_after = resp.headers.get("retry-after")
                    await asyncio.sleep(
                        float(retry_after) if retry_after else delay
                    )
                    delay = min(delay * 2, 30)
                    continue
                if resp.status_code < 500:
                    raise DevinAPIError(
                        f"POST /sessions failed: {resp.status_code} "
                        f"{resp.text[:200]}"
                    )
                last_error = f"{resp.status_code} {resp.text[:200]}"
            adopted = await self._find_session_by_tag(issue_tag, attempt_started)
            if adopted is not None:
                return adopted
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
        raise DevinAPIError(f"POST /sessions failed after retries: {last_error}")

    async def _find_session_by_tag(self, tag: str, created_after: int):
        """Return a session carrying `tag` created at/after `created_after`."""
        if not tag:
            return None
        qs = {"tags": [tag], "created_after": created_after, "first": 5}
        resp = await self._request(
            "GET", "/sessions", params={"qs": json.dumps(qs)}
        )
        # Verify the tag ourselves: protects against the filter being ignored.
        for item in resp.get("items", []):
            if tag in (item.get("tags") or []):
                return item
        return None

    async def get_session(self, session_id: str):
        return await self._request("GET", f"/sessions/{session_id}")

    async def terminate_session(self, session_id: str):
        """DELETE /sessions/{id}: ends the session; it cannot be resumed."""
        return await self._request("DELETE", f"/sessions/{session_id}")


class SimulatedDevinClient:
    """Fake Devin API for SIMULATE mode. Sessions finish after a few polls."""

    POLLS_TO_FINISH = 2

    def __init__(self):
        self._sessions = {}
        self._planned_outcomes = {}
        self.terminated = []
        # Every prompt passed to create_session, for test assertions.
        self.prompts = []

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
        self.prompts.append(prompt)
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

    async def terminate_session(self, session_id: str):
        self.terminated.append(session_id)
        return {"session_id": session_id, "status": "exit"}
