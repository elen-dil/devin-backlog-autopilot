"""pr_open runs resolve to merged or rejected once GitHub reports the PR's
final state; labels swap to match on both transitions."""

import asyncio

from fastapi.testclient import TestClient

from app.github import NullGitHubClient
from app.main import create_app
from app.service import AutopilotService, compute_metrics

PR_URL = "https://github.com/elen-dil/superset-ali/pull/9001"


def run(coro):
    return asyncio.run(coro)


def _pr_open_run(store, issue_number=1, pr_url=PR_URL):
    run_id = store.enqueue(issue_number, "t", f"https://x/{issue_number}", "b")
    store.update(
        run_id,
        state="pr_open",
        pr_url=pr_url,
        started_at="2024-01-01T00:00:00+00:00",
        finished_at="2024-01-01T00:30:00+00:00",
    )
    return run_id


def test_open_pr_keeps_run_waiting(settings, components):
    store, devin, github = components
    service = AutopilotService(settings, store, devin, github)
    run_id = _pr_open_run(store)

    run(service.merge_check_tick())

    assert store.get(run_id)["state"] == "pr_open"
    assert github.events == [("pr_check", PR_URL)]


def test_merged_pr_swaps_label(settings, components):
    store, devin, github = components
    github.pr_statuses[PR_URL] = "merged"
    service = AutopilotService(settings, store, devin, github)
    run_id = _pr_open_run(store)

    run(service.merge_check_tick())

    run_row = store.get(run_id)
    assert run_row["state"] == "merged"
    assert ("remove_label", 1, "devin-pr-open") in github.events
    assert ("add_label", 1, "devin-merged") in github.events
    # The pr_merged event timestamp records when the merge was observed.
    merged_events = [
        e for e in store.events_for(run_id)
        if e["event"] == "pr_merged" and e["detail"] == PR_URL
    ]
    assert len(merged_events) == 1
    assert merged_events[0]["timestamp"]


def test_closed_pr_marks_run_rejected(settings, components):
    store, devin, github = components
    github.pr_statuses[PR_URL] = "closed"
    service = AutopilotService(settings, store, devin, github)
    run_id = _pr_open_run(store)

    run(service.merge_check_tick())

    run_row = store.get(run_id)
    assert run_row["state"] == "rejected"
    assert ("remove_label", 1, "devin-pr-open") in github.events
    assert ("add_label", 1, "devin-pr-rejected") in github.events
    assert any(
        e["event"] == "pr_rejected" and e["detail"] == PR_URL
        for e in store.events_for(run_id)
    )


def test_github_failure_leaves_run_pr_open(settings, components):
    """A failed label swap must leave the run in pr_open so the next tick
    retries, same discipline as finalize's label ordering."""

    class FlakyLabelGitHub(NullGitHubClient):
        async def remove_label(self, issue_number, label):
            raise RuntimeError("github unavailable")

    store, devin, _ = components
    github = FlakyLabelGitHub()
    github.pr_statuses[PR_URL] = "merged"
    service = AutopilotService(settings, store, devin, github)
    run_id = _pr_open_run(store)

    run(service.merge_check_tick())

    assert store.get(run_id)["state"] == "pr_open"
    assert ("add_label", 1, "devin-merged") not in github.events


def test_startup_backfill_resolves_closed_pr(settings, components):
    """A pr_open run whose PR closed while the service was down flips to
    rejected on startup, not after the first scheduled tick."""
    store, devin, github = components
    run_id = _pr_open_run(store)
    github.pr_statuses[PR_URL] = "closed"
    app = create_app(
        settings, store=store, devin=devin, github=github, run_loops=True
    )

    with TestClient(app):
        assert store.get(run_id)["state"] == "rejected"


def test_rejected_counts_in_metrics(settings):
    base = {
        "session_id": "s-1",
        "pr_url": PR_URL,
        "acus": 2.0,
        "created_at": "2024-01-01T00:00:00+00:00",
        "started_at": "2024-01-01T00:10:00+00:00",
        "finished_at": "2024-01-01T00:40:00+00:00",
    }
    runs = [
        {**base, "issue_number": 1, "state": "merged"},
        {**base, "issue_number": 2, "state": "rejected"},
    ]

    m = compute_metrics(runs, settings)
    assert m["issues_completed"] == 2
    assert m["pr_rate"] == 1.0  # both finished runs produced a PR
    assert m["merge_rate"] == 0.5  # 1 merged / 2 PRs
    assert m["resolution_rate"] == 0.5  # 1 merged / 2 finished
    assert m["funnel"]["sessions"] == 2
    assert m["funnel"]["prs"] == 2
    assert m["funnel"]["rejected"] == 1
    assert m["by_state"]["rejected"] == 1
