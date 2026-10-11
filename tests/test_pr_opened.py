"""Time-to-PR anchors on the pr_opened event: session_poll_tick stamps it
the moment a running session reports a PR, and the startup backfill fills
runs that predate the event with the PR's GitHub created_at."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

from app.service import AutopilotService, compute_metrics

BASE = datetime(2024, 1, 1, tzinfo=timezone.utc)
PR_URL = "https://github.com/elen-dil/superset-ali/pull/9001"


def run(coro):
    return asyncio.run(coro)


def _ts(minutes: float) -> str:
    return (BASE + timedelta(minutes=minutes)).isoformat()


class _SessionStub:
    """Devin client whose get_session returns one canned payload."""

    def __init__(self, session):
        self._session = session
        self.terminated = []

    async def get_session(self, session_id):
        return self._session

    async def terminate_session(self, session_id):
        self.terminated.append(session_id)
        return {"session_id": session_id, "status": "exit"}


def _running_run(store, issue_number=1, session_id="s1"):
    run_id = store.enqueue(issue_number, "t", f"https://x/{issue_number}", "b")
    store.update(
        run_id,
        state="running",
        session_id=session_id,
        session_url="u",
        started_at="2024-01-01T00:00:00+00:00",
    )
    return run_id


def test_poll_records_pr_opened_before_finalize(settings, components):
    """A still-running session that already opened a PR gets pr_url and a
    pr_opened event immediately, and only once."""
    store, _, github = components
    devin = _SessionStub(
        {
            "session_id": "s1",
            "status": "running",
            "status_detail": "working",
            "acus_consumed": 0.5,
            "pull_requests": [{"pr_url": PR_URL}],
        }
    )
    service = AutopilotService(settings, store, devin, github)
    run_id = _running_run(store)

    run(service.session_poll_tick())

    run_row = store.get(run_id)
    assert run_row["state"] == "running"
    assert run_row["pr_url"] == PR_URL
    opened = [e for e in store.events_for(run_id) if e["event"] == "pr_opened"]
    assert len(opened) == 1
    assert opened[0]["detail"] == PR_URL

    # A second poll reporting the same PR does not double-record.
    run(service.session_poll_tick())
    opened = [e for e in store.events_for(run_id) if e["event"] == "pr_opened"]
    assert len(opened) == 1


def test_backfill_stamps_pr_opened_with_pr_created_at(settings, components):
    """Runs that predate the pr_opened event get it at startup, timestamped
    at the PR's real creation time."""
    store, devin, github = components
    service = AutopilotService(settings, store, devin, github)
    run_id = store.enqueue(1, "t", "https://x/1", "b")
    store.update(
        run_id,
        state="merged",
        pr_url=PR_URL,
        started_at=_ts(10),
        finished_at=_ts(70),
    )
    github.pr_created_ats[PR_URL] = _ts(30)

    run(service.review_backfill_tick())

    opened = [e for e in store.events_for(run_id) if e["event"] == "pr_opened"]
    assert len(opened) == 1
    assert opened[0]["timestamp"] == _ts(30)
    assert opened[0]["detail"] == PR_URL

    # Idempotent: a second backfill does not add another event.
    run(service.review_backfill_tick())
    opened = [e for e in store.events_for(run_id) if e["event"] == "pr_opened"]
    assert len(opened) == 1


def test_backfill_skips_when_created_at_unavailable(settings, components):
    """A failed lookup records nothing; metrics then fall back to
    finished_at."""
    store, devin, github = components
    service = AutopilotService(settings, store, devin, github)
    run_id = store.enqueue(1, "t", "https://x/1", "b")
    store.update(run_id, state="merged", pr_url=PR_URL, finished_at=_ts(70))

    run(service.review_backfill_tick())

    assert not [
        e for e in store.events_for(run_id) if e["event"] == "pr_opened"
    ]


def test_label_to_pr_anchors_on_pr_opened_event(settings):
    runs = [
        {
            "id": 1,
            "issue_number": 1,
            "state": "merged",
            "session_id": "s-1",
            "pr_url": PR_URL,
            "acus": None,
            "created_at": _ts(0),
            "started_at": _ts(10),
            "finished_at": _ts(70),
        }
    ]
    events = [
        {"event": "pr_opened", "run_id": 1, "timestamp": _ts(30)},
    ]
    m = compute_metrics(runs, settings, events=events)
    assert m["median_label_to_pr_minutes"] == 30.0

    # Without the event the same run falls back to finished_at.
    m = compute_metrics(runs, settings)
    assert m["median_label_to_pr_minutes"] == 70.0


def test_rejected_run_backfills_rejection_reason(settings, components):
    """Rejected rows that predate the rejection_reason column pick up the
    PR's latest comment at startup."""
    store, devin, github = components
    service = AutopilotService(settings, store, devin, github)
    run_id = store.enqueue(1, "t", "https://x/1", "b")
    store.update(
        run_id,
        state="rejected",
        pr_url=PR_URL,
        finished_at=_ts(70),
    )
    github.pr_comments[PR_URL] = [{"body": "closing this, wrong approach"}]

    run(service.review_backfill_tick())

    assert store.get(run_id)["rejection_reason"] == "closing this, wrong approach"
