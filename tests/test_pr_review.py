"""Human review feedback on a run's PR is tracked alongside the merge
poll: trusted comments and reviews record events, Devin's replies close
rounds, and the run carries a rolled-up review_status."""

import asyncio
import json

from app.service import AutopilotService, compute_metrics

PR_URL = "https://github.com/elen-dil/superset-ali/pull/9001"

T1 = "2024-01-01T01:00:00Z"
T2 = "2024-01-01T02:00:00Z"
T3 = "2024-01-01T03:00:00Z"
T4 = "2024-01-01T04:00:00Z"


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


def _user(login, bot=False):
    return {"login": login, "type": "Bot" if bot else "User"}


def _review(rid, state="CHANGES_REQUESTED", ts=T1, body="please fix",
            login="alice", assoc="MEMBER"):
    return {
        "id": rid,
        "state": state,
        "user": _user(login),
        "author_association": assoc,
        "submitted_at": ts,
        "body": body,
        "html_url": f"{PR_URL}#pullrequestreview-{rid}",
    }


def _comment(cid, ts=T1, body="please fix", login="alice", assoc="MEMBER",
             bot=False, review_id=None):
    comment = {
        "id": cid,
        "user": _user(login, bot=bot),
        "author_association": assoc,
        "created_at": ts,
        "body": body,
        "html_url": f"{PR_URL}#issuecomment-{cid}",
    }
    if review_id is not None:
        comment["pull_request_review_id"] = review_id
    return comment


def _devin_reply(cid, ts):
    return _comment(
        cid, ts, body="Done.", login="devin-ai-integration[bot]",
        assoc="NONE", bot=True,
    )


def _commit(sha, ts):
    return {"sha": sha, "commit": {"committer": {"date": ts}}}


def _events(store, run_id, name):
    return [e for e in store.events_for(run_id) if e["event"] == name]


def test_trusted_issue_comment_records_feedback(settings, components):
    store, devin, github = components
    github.pr_comments[PR_URL] = [_comment(1)]
    service = AutopilotService(settings, store, devin, github)
    run_id = _pr_open_run(store)

    run(service.merge_check_tick())

    run_row = store.get(run_id)
    assert run_row["state"] == "pr_open"
    assert run_row["review_status"] == "changes_requested"
    events = _events(store, run_id, "review_feedback")
    assert len(events) == 1
    detail = json.loads(events[0]["detail"])
    assert detail["id"] == "comment:1"
    assert detail["author"] == "alice"
    assert detail["excerpt"] == "please fix"


def test_review_states_and_filters(settings, components):
    """CHANGES_REQUESTED/COMMENTED reviews and orphaned inline comments
    count; aside-tagged, bot, and non-write authors do not. Inline
    comments belonging to a fetched review fold into it."""
    store, devin, github = components
    github.pr_reviews[PR_URL] = [
        _review(10, state="COMMENTED"),
        _review(11, body="(aside) lgtm"),          # aside -> skipped
        _review(12, assoc="NONE"),                 # untrusted -> skipped
        _review(13, login="dependabot[bot]"),      # bot login -> skipped
    ]
    github.pr_review_comments[PR_URL] = [
        _comment(20, review_id=10),                # folds into review 10
        _comment(21, review_id=999),               # orphaned -> feedback
    ]
    github.pr_comments[PR_URL] = [
        _comment(30, body="(Aside) ignore this"),
        _comment(31, login="some-action[bot]", bot=True),
        _comment(32, assoc="CONTRIBUTOR"),
    ]
    service = AutopilotService(settings, store, devin, github)
    run_id = _pr_open_run(store)

    run(service.merge_check_tick())

    events = _events(store, run_id, "review_feedback")
    ids = sorted(json.loads(e["detail"])["id"] for e in events)
    assert ids == ["rcomment:21", "review:10"]
    assert store.get(run_id)["review_status"] == "changes_requested"


def test_devin_reply_addresses_feedback_without_commit(settings, components):
    store, devin, github = components
    github.pr_comments[PR_URL] = [
        _comment(1, ts=T1),
        _devin_reply(2, T2),
    ]
    service = AutopilotService(settings, store, devin, github)
    run_id = _pr_open_run(store)

    run(service.merge_check_tick())

    run_row = store.get(run_id)
    assert run_row["review_status"] == "addressed"
    assert run_row["review_rounds"] == 1
    assert run_row["review_commits"] == 0
    addressed = _events(store, run_id, "feedback_addressed")
    assert len(addressed) == 1
    detail = json.loads(addressed[0]["detail"])
    assert detail["id"] == "comment:1"
    assert detail["minutes"] == 60.0
    assert detail["commits"] == []


def test_commit_inside_window_counts(settings, components):
    store, devin, github = components
    github.pr_comments[PR_URL] = [
        _comment(1, ts=T1),
        _devin_reply(2, T3),
    ]
    github.pr_commits[PR_URL] = [
        _commit("in-window", T2),
        _commit("too-early", "2023-12-31T23:00:00Z"),
        _commit("too-late", T4),
    ]
    service = AutopilotService(settings, store, devin, github)
    run_id = _pr_open_run(store)

    run(service.merge_check_tick())

    run_row = store.get(run_id)
    assert run_row["review_commits"] == 1
    detail = json.loads(
        _events(store, run_id, "feedback_addressed")[0]["detail"]
    )
    assert detail["commits"] == ["in-window"]


def test_one_reply_closing_two_feedbacks_is_one_round(settings, components):
    store, devin, github = components
    github.pr_comments[PR_URL] = [
        _comment(1, ts=T1),
        _comment(2, ts=T2),
        _devin_reply(3, T3),
    ]
    service = AutopilotService(settings, store, devin, github)
    run_id = _pr_open_run(store)

    run(service.merge_check_tick())

    run_row = store.get(run_id)
    assert run_row["review_rounds"] == 1
    assert run_row["review_status"] == "addressed"
    assert len(_events(store, run_id, "review_feedback")) == 2
    assert len(_events(store, run_id, "feedback_addressed")) == 2


def test_two_feedback_reply_cycles_are_two_rounds(settings, components):
    store, devin, github = components
    github.pr_comments[PR_URL] = [
        _comment(1, ts=T1),
        _devin_reply(2, T2),
        _comment(3, ts=T3),
        _devin_reply(4, T4),
    ]
    service = AutopilotService(settings, store, devin, github)
    run_id = _pr_open_run(store)

    run(service.merge_check_tick())

    assert store.get(run_id)["review_rounds"] == 2


def test_approval_records_event_and_status(settings, components):
    store, devin, github = components
    github.pr_reviews[PR_URL] = [_review(10, state="APPROVED")]
    service = AutopilotService(settings, store, devin, github)
    run_id = _pr_open_run(store)

    run(service.merge_check_tick())

    run_row = store.get(run_id)
    assert run_row["review_status"] == "approved"
    events = _events(store, run_id, "pr_approved")
    assert len(events) == 1
    assert json.loads(events[0]["detail"])["id"] == "approval:10"

    # A later comment the approval predates flips the status back.
    github.pr_comments[PR_URL] = [_comment(50, ts=T2)]
    run(service.merge_check_tick())
    assert store.get(run_id)["review_status"] == "changes_requested"


def test_rescan_dedupes_events(settings, components):
    store, devin, github = components
    github.pr_reviews[PR_URL] = [_review(10, state="APPROVED")]
    github.pr_comments[PR_URL] = [
        _comment(1, ts=T1),
        _devin_reply(2, T2),
    ]
    service = AutopilotService(settings, store, devin, github)
    run_id = _pr_open_run(store)

    run(service.merge_check_tick())
    first = len(store.events_for(run_id))
    run(service.merge_check_tick())

    assert len(store.events_for(run_id)) == first


def test_merge_keeps_approval_status(settings, components):
    """state and review_status are independent: merging does not erase
    an approved review."""
    store, devin, github = components
    github.pr_statuses[PR_URL] = "merged"
    github.pr_reviews[PR_URL] = [_review(10, state="APPROVED")]
    service = AutopilotService(settings, store, devin, github)
    run_id = _pr_open_run(store)

    run(service.merge_check_tick())

    run_row = store.get(run_id)
    assert run_row["state"] == "merged"
    assert run_row["review_status"] == "approved"


def test_merge_without_approval_keeps_computed_status(settings, components):
    store, devin, github = components
    github.pr_statuses[PR_URL] = "merged"
    service = AutopilotService(settings, store, devin, github)
    run_id = _pr_open_run(store)

    run(service.merge_check_tick())

    run_row = store.get(run_id)
    assert run_row["state"] == "merged"
    assert run_row["review_status"] == "none"


def test_closed_pr_sets_review_status_rejected(settings, components):
    store, devin, github = components
    github.pr_statuses[PR_URL] = "closed"
    github.pr_comments[PR_URL] = [
        _comment(1, ts=T1),
        _devin_reply(2, T2),
    ]
    service = AutopilotService(settings, store, devin, github)
    run_id = _pr_open_run(store)

    run(service.merge_check_tick())

    run_row = store.get(run_id)
    assert run_row["state"] == "rejected"
    assert run_row["review_status"] == "rejected"


def test_review_backfill_scans_merged_runs(settings, components):
    """The periodic tick never revisits merged runs; the backfill pass
    computes their review state from the PR."""
    store, devin, github = components
    run_id = _pr_open_run(store)
    store.update(run_id, state="merged")
    github.pr_comments[PR_URL] = [
        _comment(1, ts=T1),
        _devin_reply(2, T2),
    ]
    service = AutopilotService(settings, store, devin, github)

    run(service.review_backfill_tick())

    run_row = store.get(run_id)
    assert run_row["review_rounds"] == 1
    assert run_row["review_status"] == "addressed"


def test_review_backfill_marks_rejected_runs(settings, components):
    store, devin, github = components
    run_id = _pr_open_run(store)
    store.update(run_id, state="rejected")
    service = AutopilotService(settings, store, devin, github)

    run(service.review_backfill_tick())

    assert store.get(run_id)["review_status"] == "rejected"


def test_feedback_address_minutes(settings):
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
        {**base, "issue_number": 2, "state": "merged"},
        {**base, "issue_number": 3, "state": "rejected"},
    ]
    events = [
        {"event": "feedback_addressed", "detail": json.dumps({"minutes": 30.0})},
        {"event": "feedback_addressed", "detail": json.dumps({"minutes": 60.0})},
        {"event": "feedback_addressed", "detail": "not json"},
        {"event": "review_feedback", "detail": json.dumps({"minutes": 5})},
    ]

    m = compute_metrics(runs, settings, events=events)
    assert m["median_feedback_address_minutes"] == 45.0

    # Without events there is no median.
    m = compute_metrics(runs, settings)
    assert m["median_feedback_address_minutes"] is None
