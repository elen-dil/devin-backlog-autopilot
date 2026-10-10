import asyncio
import json

from fastapi.testclient import TestClient

from app.main import create_app
from app.service import AutopilotService, compute_metrics

DECISION = "Proceed with the upgrade; the 30-day provenance rule is waived."


def run(coro):
    return asyncio.run(coro)


def _drive(service, issue_number, polls=3, approved=False):
    run(
        service.enqueue_issue(
            issue_number,
            "t",
            f"https://x/{issue_number}",
            "body",
            source="test",
            approved=approved,
        )
    )
    run(service.dispatch_tick())
    for _ in range(polls):
        run(service.session_poll_tick())


def _write_comment(body=DECISION, created_at="2999-01-01T00:00:00Z"):
    return {
        "body": body,
        "user": {"login": "reviewer1"},
        "author_association": "MEMBER",
        "created_at": created_at,
    }


def _seed_needs_human(service, devin, issue_number):
    """Run the issue once so it finalizes as needs_human with blockers."""
    devin.plan_outcome(issue_number, "needs_human")
    _drive(service, issue_number)
    previous = service._store.latest_finished_for_issue(issue_number)
    assert previous is not None
    assert previous["state"] == "needs_human"
    return previous


def test_approval_with_comment_proceeds(settings, components):
    store, devin, github = components
    service = AutopilotService(settings, store, devin, github)
    _seed_needs_human(service, devin, 1)

    github.issue_comments[1] = [_write_comment()]
    github.issue_events[1] = [
        {
            "event": "labeled",
            "label": {"name": "devin-approved"},
            "actor": {"login": "alice"},
        }
    ]
    _drive(service, 1, polls=1, approved=True)

    run_row = store.by_state("running")[0]
    assert run_row["approved"] == 1
    prompt = devin.prompts[-1]
    assert "waived per this" in prompt
    assert DECISION in prompt
    # The previous run's escalation detail travels into the prompt.
    assert "[SIMULATED] requires a human decision" in prompt

    events = [
        e for e in store.events_for(run_row["id"]) if e["event"] == "approval_applied"
    ]
    assert len(events) == 1
    detail = json.loads(events[0]["detail"])
    assert detail["approver"] == "alice"
    assert detail["decision"] == DECISION


def test_approval_label_removed_on_finalize(settings, components):
    store, devin, github = components
    devin.plan_outcome(1, "needs_human")
    service = AutopilotService(settings, store, devin, github)
    _seed_needs_human(service, devin, 1)

    github.issue_comments[1] = [_write_comment()]
    _drive(service, 1, approved=True)

    approved_run = next(r for r in store.all() if r["approved"])
    assert approved_run["state"] == "needs_human"
    assert ("remove_label", 1, "devin-approved") in github.events


def test_approval_without_comment_is_blocked(settings, components):
    store, devin, github = components
    service = AutopilotService(settings, store, devin, github)

    run(service.enqueue_issue(2, "t", "https://x/2", "b", "test", approved=True))
    run(service.dispatch_tick())

    run_row = store.get(1)
    assert run_row["state"] == "failed"
    assert "devin-approved" in (run_row["error"] or "")
    assert devin.prompts == []
    # The run must not hold the labels, or the poller would re-trigger it.
    assert ("remove_label", 2, "devin-remediate") in github.events
    assert ("remove_label", 2, "devin-approved") in github.events
    ask = next(
        body
        for kind, _, body in github.events
        if kind == "comment" and "reviewer" in body.lower()
    )
    assert "decision" in ask.lower()


def test_non_write_comment_does_not_count(settings, components):
    store, devin, github = components
    service = AutopilotService(settings, store, devin, github)
    github.issue_comments[3] = [
        {
            "body": "looks fine to me",
            "user": {"login": "random-user"},
            "author_association": "NONE",
            "created_at": "2999-01-01T00:00:00Z",
        }
    ]

    run(service.enqueue_issue(3, "t", "https://x/3", "b", "test", approved=True))
    run(service.dispatch_tick())

    assert store.get(1)["state"] == "failed"
    assert devin.prompts == []


def test_stale_comment_does_not_count(settings, components):
    """A reviewer comment predating the last finished run is not a
    decision for this approval."""
    store, devin, github = components
    service = AutopilotService(settings, store, devin, github)
    _seed_needs_human(service, devin, 4)

    github.issue_comments[4] = [
        _write_comment(created_at="2000-01-01T00:00:00Z")
    ]
    run(service.enqueue_issue(4, "t", "https://x/4", "b", "test", approved=True))
    run(service.dispatch_tick())

    assert store.by_state("failed")[0]["issue_number"] == 4
    # Only the seed run produced a session; the approved retry never started.
    assert len(devin.prompts) == 1


def test_rejection_reason_stored_at_merge_check(settings, components):
    store, devin, github = components
    service = AutopilotService(settings, store, devin, github)
    _drive(service, 5)
    run_row = store.by_state("pr_open")[0]

    github.pr_statuses[run_row["pr_url"]] = "closed"
    github.pr_comments[run_row["pr_url"]] = [
        {"body": "wrong approach, closing"}
    ]
    run(service.merge_check_tick())

    run_row = store.get(run_row["id"])
    assert run_row["state"] == "rejected"
    assert run_row["rejection_reason"] == "wrong approach, closing"


def test_issues_endpoint_aggregates_attempts(settings, components):
    store, devin, github = components
    service = AutopilotService(settings, store, devin, github)
    devin.plan_outcome(6, "needs_human")
    _drive(service, 6)
    github.issue_comments[6] = [_write_comment()]
    _drive(service, 6, polls=1, approved=True)

    app = create_app(
        settings, store=store, devin=devin, github=github, run_loops=False
    )
    client = TestClient(app)
    issues = client.get("/api/v1/issues").json()["issues"]

    issue = next(i for i in issues if i["issue_number"] == 6)
    assert issue["attempts"] == 2
    assert issue["latest_state"] == "running"
    assert len(issue["runs"]) == 2
    assert issue["runs"][1]["approved"] == 1
    assert any(
        e["event"] == "approval_applied" for e in issue["runs"][1]["events"]
    )


def test_human_intervention_rate(settings):
    def _run(state, **over):
        base = {
            "state": state,
            "issue_number": 1,
            "session_id": None,
            "pr_url": None,
            "acus": None,
            "created_at": None,
            "started_at": None,
            "finished_at": None,
        }
        base.update(over)
        return base

    runs = [
        _run("merged", finished_at="x", pr_url="p", session_id="s"),
        _run("needs_human", finished_at="x", session_id="s"),
        _run("rejected", finished_at="x", pr_url="p", session_id="s"),
        _run("running", started_at="x"),
    ]
    metrics = compute_metrics(runs, settings)
    # 2 of 3 finished runs needed a human (needs_human + rejected).
    assert metrics["human_intervention_rate"] == 2 / 3


def test_issues_endpoint_via_client(settings, components):
    store, devin, github = components
    app = create_app(
        settings, store=store, devin=devin, github=github, run_loops=False
    )
    client = TestClient(app)
    assert client.get("/api/v1/issues").json() == {"issues": []}
