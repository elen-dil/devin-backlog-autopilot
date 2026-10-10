"""FastAPI entrypoint: observability endpoints and background loops."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .config import Settings
from .devin import DevinClient, SimulatedDevinClient
from .github import GitHubClient, NullGitHubClient
from .service import AutopilotService, compute_metrics, issues_index
from .store import RunStore

logger = logging.getLogger("autopilot")


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
            try:
                # Backfill: pr_open runs whose PRs were closed or merged while
                # the service was down resolve immediately instead of waiting
                # up to MERGE_CHECK_SECONDS for the first scheduled tick.
                await service.merge_check_tick()
            except Exception:
                logger.exception("startup PR status backfill failed")
            tasks = [
                asyncio.create_task(_loop(service.dispatch_tick, settings.dispatch_seconds)),
                asyncio.create_task(
                    _loop(service.session_poll_tick, settings.session_poll_seconds)
                ),
                asyncio.create_task(
                    _loop(service.merge_check_tick, settings.merge_check_seconds)
                ),
                asyncio.create_task(
                    _loop(service.acu_backfill_tick, settings.acu_backfill_seconds)
                ),
            ]
            if not settings.simulate:
                # Polling labeled issues is the only ingress; simulate mode
                # injects issues via POST /simulate/issue instead.
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

    @app.get("/api/v1/issues")
    async def list_issues():
        return {
            "issues": issues_index(
                store.all(include_simulated=settings.simulate),
                store.events_for,
            )
        }

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
            approved: bool = False

        @app.post("/simulate/issue")
        async def simulate_issue(issue: SimulatedIssue):
            if issue.outcome and hasattr(devin, "plan_outcome"):
                devin.plan_outcome(issue.number, issue.outcome)
            url = f"https://github.com/{settings.github_repo}/issues/{issue.number}"
            result = await service.enqueue_issue(
                issue.number,
                issue.title,
                url,
                issue.body,
                source="simulate",
                approved=issue.approved,
            )
            return {"result": result}

    return app


app = create_app()
