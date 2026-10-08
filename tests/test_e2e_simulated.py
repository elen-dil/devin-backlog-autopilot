import asyncio

from app.service import AutopilotService


def run(coro):
    return asyncio.run(coro)


def _drive(service, issue_number, polls=3):
    run(service.enqueue_issue(issue_number, "t", f"https://x/{issue_number}", "body"))
    run(service.dispatch_tick())
    for _ in range(polls):
        run(service.session_poll_tick())


def test_simulated_run_reaches_pr_open(settings, components):
    store, devin, github = components
    service = AutopilotService(settings, store, devin, github)

    _drive(service, 1)

    run_row = store.by_state("pr_open")[0]
    assert run_row["issue_number"] == 1
    assert run_row["session_url"]
    assert run_row["pr_url"].endswith("/pull/9001")
    assert run_row["outcome"] == "fixed"
    assert run_row["acus"] == 2.5

    events = github.events
    assert ("add_label", 1, "devin-running") in events
    assert ("remove_label", 1, "devin-remediate") in events
    assert ("add_label", 1, "devin-pr-open") in events
    assert any(e[0] == "comment" for e in events)


def test_simulated_run_reaches_needs_human(settings, components):
    store, devin, github = components
    devin.plan_outcome(2, "needs_human")
    service = AutopilotService(settings, store, devin, github)

    _drive(service, 2)

    run_row = store.by_state("needs_human")[0]
    assert run_row["issue_number"] == 2
    assert run_row["outcome"] == "needs_human"
    assert ("add_label", 2, "devin-needs-human") in github.events


def test_suspended_at_acu_cap_becomes_needs_human(settings, components):
    store, devin, github = components
    service = AutopilotService(settings, store, devin, github)

    run(service.enqueue_issue(3, "t", "https://x/3", "body"))
    run(service.dispatch_tick())
    run_row = store.by_state("running")[0]

    suspended = {
        "status": "suspended",
        "status_detail": "usage_limit_exceeded",
        "acus_consumed": 10.0,
        "pull_requests": [],
        "structured_output": None,
    }
    run(service._finalize(run_row, suspended))

    run_row = store.get(run_row["id"])
    assert run_row["state"] == "needs_human"
    finish_comment = next(
        e[2] for e in github.events if e[0] == "comment" and "Autopilot finished" in e[2]
    )
    assert "usage_limit_exceeded" in finish_comment
    assert ("add_label", 3, "devin-needs-human") in github.events


def test_dispatch_respects_concurrency_cap(settings, components):
    store, devin, github = components
    service = AutopilotService(settings, store, devin, github)
    settings.max_concurrent_sessions = 1

    run(service.enqueue_issue(10, "t", "https://x/10", "b"))
    run(service.enqueue_issue(11, "t", "https://x/11", "b"))
    run(service.dispatch_tick())

    assert store.count_state("running") == 1
    assert len(store.by_state("queued")) == 1
