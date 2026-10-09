import asyncio
import dataclasses
import hashlib
import hmac
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import create_app
from app.service import compute_metrics
from app.store import RunStore

SECRET = "test-secret"

BASE = datetime(2024, 1, 1, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def _ts(minutes: float) -> str:
    return (BASE + timedelta(minutes=minutes)).isoformat()


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _client(settings, components):
    store, devin, github = components
    app = create_app(settings, store=store, devin=devin, github=github, run_loops=False)
    return app, TestClient(app)


def _insert_run(
    store,
    issue_number,
    state,
    *,
    created,
    started=None,
    finished=None,
    acus=None,
    pr_url=None,
    is_simulated=False,
):
    """Insert a run with fully deterministic timestamps for metric math."""
    run_id = store.enqueue(
        issue_number, "t", f"https://x/{issue_number}", "b",
        is_simulated=is_simulated,
    )
    store.update(
        run_id,
        state=state,
        created_at=_ts(created),
        started_at=_ts(started) if started is not None else None,
        finished_at=_ts(finished) if finished is not None else None,
        acus=acus,
        pr_url=pr_url,
    )
    return run_id


def test_run_events_cover_full_lifecycle(settings, components):
    store, devin, _ = components
    app, client = _client(settings, components)
    service = app.state.service

    resp = client.post(
        "/simulate/issue", json={"number": 1, "title": "t", "body": "b"}
    )
    assert resp.json() == {"result": "queued"}

    run(service.dispatch_tick())
    run(service.session_poll_tick())  # observes running/working
    run(service.session_poll_tick())  # observes running/finished, finalizes

    run_id = store.by_state("pr_open")[0]["id"]
    events = store.events_for(run_id)
    names = [e["event"] for e in events]

    assert names[0] == "queued"
    assert events[0]["detail"] == "source=simulate"
    assert names[1] == "session_created"
    assert "devin-sim-" in events[1]["detail"]
    assert "https://app.devin.ai/sessions/" in events[1]["detail"]

    # One event per distinct (status, status_detail) observation.
    assert [e["detail"] for e in events if e["event"] == "status_change"] == [
        "status=running detail=working",
        "status=running detail=finished",
    ]

    # Lifecycle ordering: queued -> session_created -> status_change -> finalized.
    assert names.index("queued") < names.index("session_created")
    assert names.index("session_created") < names.index("status_change")
    assert names.index("status_change") < names.index("finalized")
    assert events[names.index("finalized")]["detail"] == (
        "outcome=fixed state=pr_open"
    )

    # GitHub write-backs are recorded on both sides of finalize.
    writes = [e["detail"] for e in events if e["event"] == "github_write"]
    assert len(writes) == 6
    assert all(d.endswith(": skipped (simulate)") for d in writes)
    assert "add_label devin-running" in writes[0]
    assert any(d.startswith("comment session started") for d in writes)
    assert any(d.startswith("comment result") for d in writes)
    last_write = max(i for i, n in enumerate(names) if n == "github_write")
    assert names.index("finalized") < last_write
    assert names[-1] == "github_write"


def test_run_detail_and_events_endpoints(settings, components):
    store, devin, _ = components
    app, client = _client(settings, components)
    service = app.state.service

    client.post("/simulate/issue", json={"number": 2, "title": "t", "body": "b"})
    run(service.dispatch_tick())
    run(service.session_poll_tick())
    run(service.session_poll_tick())

    run_id = store.by_state("pr_open")[0]["id"]
    resp = client.get(f"/api/v1/runs/{run_id}")
    assert resp.status_code == 200
    assert resp.json()["run"]["id"] == run_id
    assert resp.json()["run"]["state"] == "pr_open"
    assert resp.json()["run"]["is_simulated"] == 1

    resp = client.get(f"/api/v1/runs/{run_id}/events")
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert events[0]["event"] == "queued"
    assert {e["event"] for e in events} >= {
        "queued",
        "session_created",
        "status_change",
        "github_write",
        "finalized",
    }

    assert client.get("/api/v1/runs/9999").status_code == 404
    assert client.get("/api/v1/runs/9999/events").status_code == 404


def test_queued_event_records_webhook_source(settings, components):
    store, devin, _ = components
    _, client = _client(settings, components)

    body = json.dumps(
        {
            "action": "labeled",
            "label": {"name": "devin-remediate"},
            "issue": {
                "number": 42,
                "title": "Fix the thing",
                "html_url": "https://github.com/elen-dil/superset-ali/issues/42",
                "state": "open",
                "body": "please fix",
            },
        }
    ).encode()
    resp = client.post(
        "/webhooks/github",
        content=body,
        headers={"x-github-event": "issues", "x-hub-signature-256": _sign(body)},
    )
    assert resp.json() == {"result": "queued"}

    run_id = store.all()[0]["id"]
    first = store.events_for(run_id)[0]
    assert first["event"] == "queued"
    assert first["detail"] == "source=webhook"


def test_metrics_hand_computed(settings, components):
    store, devin, _ = components
    settings.acu_price_usd = 1.5

    # finished_at - started_at -> latency; created_at - finished_at -> label
    # to result. Cap is settings.max_acu_per_session = 10.
    _insert_run(store, 1, "merged", created=0, started=10, finished=70,
                acus=4, pr_url="https://x/pull/1")
    _insert_run(store, 2, "pr_open", created=0, started=30, finished=60,
                acus=10, pr_url="https://x/pull/2")  # at cap
    _insert_run(store, 3, "needs_human", created=0, started=20, finished=50,
                acus=3)
    _insert_run(store, 4, "failed", created=0, started=40, finished=80, acus=2)
    _insert_run(store, 5, "queued", created=0)

    m = compute_metrics(store.all(), settings)
    assert set(m) == {
        "total_runs",
        "by_state",
        "simulate",
        "repo",
        "max_acu_per_session",
        "acu_price_usd",
        "funnel",
        "issues_completed",
        "pr_rate",
        "merge_rate",
        "resolution_rate",
        "median_latency_minutes",
        "median_label_to_result_minutes",
        "median_label_to_pr_minutes",
        "total_acus",
        "acus_per_run",
        "acus_per_pr",
        "acus_per_merged_fix",
        "runs_at_cap",
    }
    assert m["total_runs"] == 5
    assert m["by_state"] == {
        "merged": 1,
        "pr_open": 1,
        "needs_human": 1,
        "failed": 1,
        "queued": 1,
    }
    assert m["simulate"] is True
    assert m["repo"] == settings.github_repo
    assert m["max_acu_per_session"] == 10
    assert m["acu_price_usd"] == 1.5
    assert m["funnel"] == {
        "labeled": 5,
        "sessions_started": 4,
        "prs_opened": 2,
        "merged": 1,
    }
    assert m["issues_completed"] == 4
    assert m["pr_rate"] == 0.5  # 2 PRs / 4 finished
    assert m["merge_rate"] == 0.5  # 1 merged / 2 PRs
    assert m["resolution_rate"] == 0.25  # 1 merged / 4 finished
    # latencies 60, 30, 30, 40 -> median of (30, 40)
    assert m["median_latency_minutes"] == 35.0
    # label->result 70, 60, 50, 80 -> median of (60, 70)
    assert m["median_label_to_result_minutes"] == 65.0
    # label->pr 70, 60 -> 65
    assert m["median_label_to_pr_minutes"] == 65.0
    assert m["total_acus"] == 19
    assert m["acus_per_run"] == 19 / 4
    assert m["acus_per_pr"] == 19 / 2
    assert m["acus_per_merged_fix"] == 19
    assert m["runs_at_cap"] == 1


def test_funnel_counts_distinct_issues(settings, components):
    """Re-remediated issues count once per funnel stage, keeping it monotonic."""
    store, _, _ = components
    _insert_run(store, 7, "needs_human", created=0, started=1, finished=2)
    _insert_run(store, 7, "merged", created=3, started=4, finished=5,
                pr_url="https://x/pull/7")
    _insert_run(store, 8, "queued", created=0)

    m = compute_metrics(store.all(), settings)
    assert m["total_runs"] == 3
    assert m["funnel"] == {
        "labeled": 2,
        "sessions_started": 1,
        "prs_opened": 1,
        "merged": 1,
    }


def test_metrics_endpoint_and_empty_guards(settings, components):
    _, client = _client(settings, components)
    m = client.get("/api/v1/metrics").json()
    assert m["total_runs"] == 0
    assert m["pr_rate"] == 0.0
    assert m["resolution_rate"] == 0.0
    assert m["median_latency_minutes"] is None
    assert m["median_label_to_result_minutes"] is None
    assert m["median_label_to_pr_minutes"] is None
    assert m["acus_per_run"] is None
    assert m["acus_per_pr"] is None
    assert m["acus_per_merged_fix"] is None
    assert m["runs_at_cap"] == 0
    assert m["funnel"] == {
        "labeled": 0,
        "sessions_started": 0,
        "prs_opened": 0,
        "merged": 0,
    }


def test_simulated_runs_filtered_when_not_simulating(settings, components):
    store, devin, github = components
    real = dataclasses.replace(
        settings,
        simulate=False,
        devin_api_key="k",
        devin_org_id="o",
        github_token="t",
    )
    app = create_app(real, store=store, devin=devin, github=github, run_loops=False)
    client = TestClient(app)

    sim_id = store.enqueue(1, "t", "https://x/1", "b", is_simulated=True)
    real_id = store.enqueue(2, "t", "https://x/2", "b", is_simulated=False)

    runs = client.get("/api/v1/runs").json()["runs"]
    assert [r["id"] for r in runs] == [real_id]
    assert runs[0]["is_simulated"] == 0

    m = client.get("/api/v1/metrics").json()
    assert m["total_runs"] == 1
    assert m["simulate"] is False

    # The detail endpoint still serves any run, simulated or not.
    assert client.get(f"/api/v1/runs/{sim_id}").status_code == 200
    assert client.get(f"/api/v1/runs/{sim_id}").json()["run"]["is_simulated"] == 1


def test_simulated_runs_included_when_simulating(settings, components):
    store, devin, _ = components
    _, client = _client(settings, components)
    store.enqueue(1, "t", "https://x/1", "b", is_simulated=True)
    store.enqueue(2, "t", "https://x/2", "b", is_simulated=False)
    runs = client.get("/api/v1/runs").json()["runs"]
    assert len(runs) == 2


# The runs schema as it existed before the is_simulated column was added.
_PRE_MIGRATION_RUNS_SCHEMA = """
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_number INTEGER NOT NULL,
    title TEXT NOT NULL,
    issue_url TEXT NOT NULL,
    issue_body TEXT,
    state TEXT NOT NULL DEFAULT 'queued',
    session_id TEXT,
    session_url TEXT,
    pr_url TEXT,
    outcome TEXT,
    summary TEXT,
    tests_run INTEGER,
    tests_passed INTEGER,
    acus REAL,
    output_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    merged_at TEXT
);
"""


def test_is_simulated_backfill_on_pre_migration_db(tmp_path):
    """Opening a pre-migration DB adds is_simulated and backfills rows whose
    session_id came from SimulatedDevinClient (devin-sim-*)."""
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute(_PRE_MIGRATION_RUNS_SCHEMA)
    conn.execute(
        "INSERT INTO runs (issue_number, title, issue_url, state, session_id,"
        " created_at) VALUES (1, 't', 'https://x/1', 'failed', 'devin-sim-abc',"
        " '2024-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO runs (issue_number, title, issue_url, state, session_id,"
        " created_at) VALUES (2, 't', 'https://x/2', 'failed', 'devin-real-xyz',"
        " '2024-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    store = RunStore(path)
    rows = {r["issue_number"]: r for r in store.all()}
    assert rows[1]["is_simulated"] == 1
    assert rows[2]["is_simulated"] == 0

    # Re-opening is idempotent: the column now exists, so no migration runs.
    assert RunStore(path).get(1)["is_simulated"] == 1
    assert RunStore(path).get(2)["is_simulated"] == 0
