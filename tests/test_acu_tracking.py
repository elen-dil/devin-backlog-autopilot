"""ACU usage is recorded live while a session runs and corrected after
finalize, because Devin posts usage to the session object asynchronously."""

import asyncio
from datetime import datetime, timedelta, timezone

from app.service import ACU_SETTLE_SECONDS, AutopilotService, compute_metrics

BASE = datetime(2024, 1, 1, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def _now(offset_seconds: float = 0) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    ).isoformat()


def _iso(minutes: float) -> str:
    return (BASE + timedelta(minutes=minutes)).isoformat()


class _SessionStub:
    """Devin client whose get_session returns canned payloads."""

    def __init__(self, session):
        self._session = session
        self.calls = 0
        self.terminated = []

    async def get_session(self, session_id):
        self.calls += 1
        if isinstance(self._session, Exception):
            raise self._session
        return self._session

    async def terminate_session(self, session_id):
        self.terminated.append(session_id)
        return {"session_id": session_id, "status": "exit"}


def _running_run(store, session_id="s1", acus=None):
    run_id = store.enqueue(1, "t", "https://x/1", "b")
    store.update(
        run_id,
        state="running",
        session_id=session_id,
        session_url="u",
        started_at=_now(),
        acus=acus,
    )
    return run_id


def test_acus_recorded_live_while_running(settings, components):
    store, devin, github = components
    service = AutopilotService(settings, store, devin, github)
    run(service.enqueue_issue(1, "t", "u", "b", source="test"))
    run(service.dispatch_tick())

    run(service.session_poll_tick())  # sim session reports acus_consumed=0.5

    run_row = store.by_state("running")[0]
    assert run_row["acus"] == 0.5

    run(service.session_poll_tick())  # finishes: finalize stores 2.5

    assert store.by_state("pr_open")[0]["acus"] == 2.5


def test_finalize_does_not_regress_acus(settings, components):
    store, _, github = components
    devin = _SessionStub(
        {
            "session_id": "s1",
            "status": "exit",
            "status_detail": None,
            "acus_consumed": 0.0,
            "pull_requests": [{"pr_url": "https://x/pr/1"}],
            "structured_output": {"outcome": "fixed", "summary": "done"},
        }
    )
    service = AutopilotService(settings, store, devin, github)
    run_id = _running_run(store, acus=1.5)

    run(service.session_poll_tick())

    # The terminal snapshot under-reported, but the live reading wins.
    assert store.get(run_id)["acus"] == 1.5


def test_backfill_corrects_stale_finalize_value(settings, components):
    store, _, github = components
    devin = _SessionStub({"session_id": "s1", "acus_consumed": 4.2})
    service = AutopilotService(settings, store, devin, github)
    run_id = store.enqueue(1, "t", "u", "b")
    store.update(
        run_id,
        state="pr_open",
        session_id="s1",
        acus=0.0,
        finished_at=_now(),
    )

    run(service.acu_backfill_tick())

    assert store.get(run_id)["acus"] == 4.2
    events = [e["event"] for e in store.events_for(run_id)]
    assert "acus_updated" in events


def test_backfill_marks_exit_and_stops_polling(settings, components):
    store, _, github = components
    devin = _SessionStub(
        {"session_id": "s1", "status": "exit", "acus_consumed": 4.2}
    )
    service = AutopilotService(settings, store, devin, github)
    run_id = store.enqueue(1, "t", "u", "b")
    store.update(
        run_id,
        state="merged",
        session_id="s1",
        acus=0.0,
        finished_at=_now(),
    )

    run(service.acu_backfill_tick())

    assert store.get(run_id)["acus"] == 4.2
    events = [e["event"] for e in store.events_for(run_id)]
    assert "acus_updated" in events
    assert "session_ended" in events

    # An exited session won't change: the marker ends the re-polling.
    run(service.acu_backfill_tick())
    assert devin.calls == 1


def test_finalize_terminates_session_still_running(settings, components):
    store, _, github = components
    devin = _SessionStub(
        {
            "session_id": "s1",
            "status": "running",
            "status_detail": "waiting_for_user",
            "acus_consumed": 0.0,
            "pull_requests": [{"pr_url": "https://x/pr/1"}],
            "structured_output": {"outcome": "fixed", "summary": "done"},
        }
    )
    service = AutopilotService(settings, store, devin, github)
    run_id = _running_run(store)

    run(service.session_poll_tick())

    # waiting_for_user sessions would linger suspended; finalize ends them.
    assert devin.terminated == ["s1"]
    events = [e["event"] for e in store.events_for(run_id)]
    assert "session_terminated" in events


def test_finalize_does_not_terminate_exited_session(settings, components):
    store, _, github = components
    devin = _SessionStub(
        {
            "session_id": "s1",
            "status": "exit",
            "status_detail": None,
            "acus_consumed": 1.0,
            "pull_requests": [],
            "structured_output": {"outcome": "needs_human", "blockers": "x"},
        }
    )
    service = AutopilotService(settings, store, devin, github)
    _running_run(store)

    run(service.session_poll_tick())

    assert devin.terminated == []


def _metric_run(**kw):
    base = {
        "state": "pr_open",
        "issue_number": 1,
        "pr_url": "https://x/pr/1",
        "acus": None,
        "created_at": _iso(0),
        "started_at": _iso(10),
        "finished_at": _iso(40),  # 30-minute session
    }
    base.update(kw)
    return base


def test_metrics_exclude_pending_from_acu_averages(settings):
    runs = [
        _metric_run(issue_number=1, acus=0.0),  # pending
        _metric_run(issue_number=2, acus=2.0, pr_url="https://x/pr/2"),
    ]
    m = compute_metrics(runs, settings)
    assert m["acus_pending"] == 1
    # Only the measured run counts toward the averages.
    assert m["acus_per_run"] == 2.0
    assert m["acus_per_pr"] == 2.0
    assert m["total_session_minutes"] == 60.0
    assert m["session_minutes_per_run"] == 30.0


def test_metrics_session_minutes_as_cost_proxy(settings):
    runs = [
        _metric_run(issue_number=1, state="merged", acus=1.0),
        _metric_run(issue_number=2, state="failed", acus=None, pr_url=None),
    ]
    m = compute_metrics(runs, settings)
    assert m["total_session_minutes"] == 60.0
    assert m["session_minutes_per_run"] == 30.0
    assert m["session_minutes_per_merged_fix"] == 60.0
    assert m["acus_per_merged_fix"] == 1.0
    assert m["acus_pending"] == 1


def test_backfill_skips_runs_past_the_settle_window(settings, components):
    store, _, github = components
    devin = _SessionStub({"session_id": "s1", "acus_consumed": 4.2})
    service = AutopilotService(settings, store, devin, github)
    run_id = store.enqueue(1, "t", "u", "b")
    store.update(
        run_id,
        state="needs_human",
        session_id="s1",
        acus=0.0,
        finished_at=_now(-ACU_SETTLE_SECONDS - 60),
    )

    run(service.acu_backfill_tick())

    assert devin.calls == 0
    assert store.get(run_id)["acus"] == 0.0


def test_backfill_tolerates_poll_failures(settings, components):
    store, _, github = components
    devin = _SessionStub(RuntimeError("devin api down"))
    service = AutopilotService(settings, store, devin, github)
    run_id = store.enqueue(1, "t", "u", "b")
    store.update(
        run_id,
        state="failed",
        session_id="s1",
        acus=0.0,
        finished_at=_now(),
    )

    run(service.acu_backfill_tick())

    assert store.get(run_id)["acus"] == 0.0
