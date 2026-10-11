"""Pure dashboard helpers: label conventions, per-run units, and the
reason fallbacks. Render functions are exercised by hand; these cover
the pieces that decide what text the UI shows."""

import json
from datetime import datetime, timedelta, timezone

import dashboard.app as dash


def _ago(seconds):
    return (
        datetime.now(timezone.utc) - timedelta(seconds=seconds)
    ).isoformat()


def test_acu_display_pending_inside_settle_window():
    run = {"finished_at": _ago(60), "acus": None}
    assert dash.acu_display(run, 1800) == "pending"


def test_acu_display_not_reported_after_settle_window():
    run = {"finished_at": _ago(1800 + 60), "acus": None}
    assert dash.acu_display(run, 1800) == "not reported"


def test_acu_display_running_and_measured():
    assert dash.acu_display({"finished_at": None, "acus": None}, 1800) == "—"
    assert dash.acu_display(
        {"finished_at": _ago(3600), "acus": 2.5}, 1800
    ) == "2.5"


def test_tests_text_shows_failed_count_when_present():
    run = {"tests_run": 22367, "tests_passed": 22320, "tests_failed": 0}
    assert dash.tests_text(run) == "22320 passed · 0 failed · 22367 run"


def test_tests_text_keeps_legacy_ratio():
    assert dash.tests_text({"tests_run": 10, "tests_passed": 8}) == "8/10"
    assert dash.tests_text({}) == "—"


def test_funnel_counts_are_per_run():
    metrics = {
        "funnel": {
            "sessions": 3,
            "prs": 2,
            "merged": 1,
            "awaiting_merge": 1,
            "rejected": 0,
        }
    }
    assert dash.funnel_counts(metrics, []) == [
        ("Sessions", 3),
        ("PRs opened", 2),
        ("Merged", 1),
        ("Awaiting merge", 1),
        ("Rejected", 0),
    ]
    # Older API shape falls back to the runs list, still per run. A
    # running run with a PR counts under Awaiting merge so the terminal
    # stages always partition PRs opened.
    runs = [
        {"id": 1, "issue_number": 7, "session_id": "a", "state": "rejected",
         "pr_url": "p"},
        {"id": 2, "issue_number": 7, "session_id": "b", "state": "merged",
         "pr_url": "p2"},
        {"id": 3, "issue_number": 8, "session_id": "c", "state": "running",
         "pr_url": "p3"},
        {"id": 4, "issue_number": 9, "session_id": "d", "state": "running",
         "pr_url": None},
    ]
    assert dash.funnel_counts({}, runs) == [
        ("Sessions", 4),
        ("PRs opened", 3),
        ("Merged", 1),
        ("Awaiting merge", 1),
        ("Rejected", 1),
    ]


def test_outcome_bucket_maps_pr_open_to_awaiting_merge():
    label, _ = dash.outcome_bucket(
        {"state": "pr_open", "outcome": "fixed", "finished_at": _ago(60)}
    )
    assert label == "Awaiting merge"
    assert dash.outcome_bucket({"state": "merged", "outcome": "fixed"})[0] == "Merged"
    assert dash.outcome_bucket({"state": "rejected", "outcome": "fixed"})[0] == (
        "PR rejected"
    )


def test_cost_run_label_gives_each_attempt_its_own_bar():
    runs = [
        {"id": 1, "issue_number": 7, "title": "bug"},
        {"id": 5, "issue_number": 7, "title": "bug"},
        {"id": 8, "issue_number": 9, "title": "other"},
    ]
    labels = {r["id"]: dash.cost_run_label(r, runs) for r in runs}
    assert labels[1].endswith("attempt 1")
    assert labels[5].endswith("attempt 2")
    assert "attempt" not in labels[8]
    # Labels are unique per run even when issue_refs match.
    assert len({dash.cost_run_label(r, runs) for r in runs}) == 3


def test_run_reason_falls_back_to_summary_then_error():
    run = {"output_json": json.dumps({"blockers": "", "summary": "cannot repro"})}
    assert dash.run_reason(run) == "cannot repro"
    run = {
        "output_json": json.dumps({"blockers": "", "summary": ""}),
        "error": "session died",
    }
    assert dash.run_reason(run) == "session died"


def test_run_reason_uses_latest_review_feedback_when_no_stored_reason():
    run = {
        "state": "rejected",
        "rejection_reason": None,
        "output_json": None,
        "events": [
            {
                "event": "review_feedback",
                "detail": json.dumps({"excerpt": "first comment"}),
            },
            {
                "event": "review_feedback",
                "detail": json.dumps({"excerpt": "latest comment"}),
            },
        ],
    }
    assert dash.run_reason(run) == "latest comment"
