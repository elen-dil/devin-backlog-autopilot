"""FastAPI entrypoint: webhook, observability endpoints, background loops."""

import asyncio
import hashlib
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from .config import Settings
from .devin import DevinClient, SimulatedDevinClient
from .github import GitHubClient, NullGitHubClient
from .service import AutopilotService, compute_metrics
from .store import RunStore

logger = logging.getLogger("autopilot")


def _verify_signature(secret: str, body: bytes, header: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header, expected)


async def _loop(tick, seconds: int) -> None:
    while True:
        try:
            await tick()
        except Exception:
            logger.exception("background tick failed")
        await asyncio.sleep(seconds)


def create_app(
    settings: Settings | None = None,
    *,
    store: RunStore | None = None,
    devin=None,
    github=None,
    run_loops: bool = True,
) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()

    store = store or RunStore(settings.database_path)
    if devin is None:
        devin = (
            SimulatedDevinClient()
            if settings.simulate
            else DevinClient(settings.devin_org_id, settings.devin_api_key)
        )
    if github is None:
        github = (
            NullGitHubClient()
            if settings.simulate
            else GitHubClient(settings.github_repo, settings.github_token)
        )
    service = AutopilotService(settings, store, devin, github)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = []
        if run_loops:
            tasks = [
                asyncio.create_task(_loop(service.dispatch_tick, settings.dispatch_seconds)),
                asyncio.create_task(
                    _loop(service.session_poll_tick, settings.session_poll_seconds)
                ),
                asyncio.create_task(
                    _loop(service.merge_check_tick, settings.merge_check_seconds)
                ),
            ]
            if not settings.simulate:
                # The webhook is the primary trigger; this is the fallback.
                tasks.append(
                    asyncio.create_task(
                        _loop(service.issue_poll_tick, settings.issue_poll_seconds)
                    )
                )
        yield
        for task in tasks:
            task.cancel()

    app = FastAPI(title="devin-backlog-autopilot", lifespan=lifespan)
    app.state.service = service

    @app.post("/webhooks/github")
    async def github_webhook(request: Request):
        body = await request.body()
        if settings.github_webhook_secret:
            signature = request.headers.get("x-hub-signature-256", "")
            if not _verify_signature(settings.github_webhook_secret, body, signature):
                raise HTTPException(status_code=401, detail="bad signature")

        event = request.headers.get("x-github-event", "")
        if event == "ping":
            return {"result": "pong"}
        if event == "issues":
            payload = await request.json()
            issue = payload.get("issue", {})
            if (
                payload.get("action") == "labeled"
                and payload.get("label", {}).get("name") == settings.trigger_label
                and issue.get("state") == "open"
            ):
                result = await service.enqueue_issue(
                    issue["number"],
                    issue["title"],
                    issue["html_url"],
                    issue.get("body") or "",
                    source="webhook",
                )
                return {"result": result}
        return {"result": "ignored"}

    @app.get("/api/v1/runs")
    async def list_runs():
        # Outside SIMULATE mode, hide simulated runs so metrics reflect reality.
        return {"runs": store.all(include_simulated=settings.simulate)}

    @app.get("/api/v1/runs/{run_id}")
    async def get_run(run_id: int):
        run = store.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return {"run": run}

    @app.get("/api/v1/runs/{run_id}/events")
    async def run_events(run_id: int):
        if store.get(run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        return {"events": store.events_for(run_id)}

    @app.get("/api/v1/metrics")
    async def metrics():
        return compute_metrics(
            store.all(include_simulated=settings.simulate), settings
        )

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "simulate": settings.simulate}

    if settings.simulate:

        class SimulatedIssue(BaseModel):
            number: int
            title: str
            body: str = ""
            outcome: str | None = None

        @app.post("/simulate/issue")
        async def simulate_issue(issue: SimulatedIssue):
            if issue.outcome and hasattr(devin, "plan_outcome"):
                devin.plan_outcome(issue.number, issue.outcome)
            url = f"https://github.com/{settings.github_repo}/issues/{issue.number}"
            result = await service.enqueue_issue(
                issue.number, issue.title, url, issue.body, source="simulate"
            )
            return {"result": result}

    return app


app = create_app()
