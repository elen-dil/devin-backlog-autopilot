"""Core orchestration: enqueue -> dispatch -> poll -> finalize -> merge check.

Each `*_tick` method is one pass of a background loop, so tests can drive the
pipeline deterministically without sleeping.
"""

import json
import logging
import statistics
from datetime import datetime, timezone

from .prompt import STRUCTURED_OUTPUT_SCHEMA, build_prompt

logger = logging.getLogger("autopilot")

TERMINAL_STATUSES = {"exit", "error", "suspended"}
FINISHED_DETAILS = {"finished", "waiting_for_user"}

# status_detail values that mean the session was stopped by a spending limit
# rather than by finishing or erroring.
QUOTA_DETAILS = {
    "usage_limit_exceeded",
    "user_usage_limit_exceeded",
    "org_usage_limit_exceeded",
    "out_of_credits",
    "out_of_quota",
    "no_quota_allocation",
    "total_session_limit_exceeded",
}

# Devin posts ACU usage to the session object asynchronously, so the reading
# at termination is often stale (frequently still 0). Keep re-polling
# finalized runs until the session exits or this window elapses.
ACU_SETTLE_SECONDS = 30 * 60

# GitHub PR status -> (run state, label setting, run event) for runs
# leaving pr_open. "open" has no entry: the run keeps waiting.
_PR_RESOLUTIONS = {
    "merged": ("merged", "merged_label", "pr_merged"),
    "closed": ("rejected", "pr_rejected_label", "pr_rejected"),
}


def session_is_terminal(status: str, status_detail) -> bool:
    """Terminal = exit/error/suspended, or running with a finished/waiting detail."""
    if status in TERMINAL_STATUSES:
        return True
    return status == "running" and status_detail in FINISHED_DETAILS


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _older_than(iso_ts: str, seconds: float) -> bool:
    if not iso_ts:
        return False
    age = datetime.now(timezone.utc) - datetime.fromisoformat(iso_ts)
    return age.total_seconds() > seconds


class AutopilotService:
    def __init__(self, settings, store, devin, github):
        self._s = settings
        self._store = store
        self._devin = devin
        self._github = github

    async def enqueue_issue(
        self, number: int, title: str, url: str, body: str, source: str
    ) -> str:
        run_id = self._store.enqueue(
            number, title, url, body or "", is_simulated=self._s.simulate
        )
        if run_id is None:
            logger.info("issue #%s already has an active run; skipping", number)
            return "already_active"
        self._store.add_event(run_id, "queued", f"source={source}")
        logger.info("queued issue #%s (run %s) via %s", number, run_id, source)
        return "queued"

    async def _gh_write(self, run_id: int, op: str, call, *args) -> None:
        """GitHub write-back with a `github_write` event; failures re-raise.

        Callers keep their existing error handling (e.g. a failed label
        removal aborts finalize so the run stays active for a retry).
        """
        try:
            await call(*args)
        except Exception as exc:
            self._store.add_event(run_id, "github_write", f"{op}: failed ({exc})")
            raise
        result = "skipped (simulate)" if self._s.simulate else "ok"
        self._store.add_event(run_id, "github_write", f"{op}: {result}")

    def _record_status_change(self, run_id: int, status, status_detail) -> None:
        """Record a status_change event when the observed (status, detail)
        pair differs from the last one recorded; the first observation counts."""
        observed = f"status={status} detail={status_detail}"
        last = next(
            (
                e["detail"]
                for e in reversed(self._store.events_for(run_id))
                if e["event"] == "status_change"
            ),
            None,
        )
        if observed != last:
            self._store.add_event(run_id, "status_change", observed)

    def _record_acus(self, run: dict, acus, *, event: bool = False) -> None:
        """Persist the latest ACU reading when the API reports a new one.
        Live polls stay silent (the value ticks up every pass); only
        post-finalize corrections get an acus_updated event."""
        if acus is None or acus == run["acus"]:
            return
        self._store.update(run["id"], acus=acus)
        if event:
            self._store.add_event(
                run["id"], "acus_updated", f"{run['acus']} -> {acus}"
            )

    async def issue_poll_tick(self) -> None:
        """Adopt open issues carrying the trigger label."""
        issues = await self._github.list_labeled_issues(self._s.trigger_label)
        for issue in issues:
            await self.enqueue_issue(
                issue["number"],
                issue["title"],
                issue["html_url"],
                issue.get("body") or "",
                source="poller",
            )

    async def dispatch_tick(self) -> None:
        """Promote queued runs into Devin sessions while slots are free."""
        slots = self._s.max_concurrent_sessions - self._store.count_state("running")
        if slots <= 0:
            return
        for run in self._store.oldest_queued(slots):
            await self._start_run(run)

    async def _start_run(self, run: dict) -> None:
        issue_number = run["issue_number"]
        # Claim the run before the async call: if the process dies between
        # create_session and writing the session_id, the stale-claim sweep in
        # session_poll_tick fails the run instead of re-dispatching it and
        # spawning a second (orphaned) session for the same issue.
        self._store.update(run["id"], state="running", started_at=_now())
        try:
            session = await self._devin.create_session(
                prompt=build_prompt(
                    issue_number=issue_number,
                    title=run["title"],
                    url=run["issue_url"],
                    body=run["issue_body"],
                    repo=self._s.github_repo,
                    needs_human_label=self._s.needs_human_label,
                ),
                title=f"autopilot: issue #{issue_number}",
                repos=[self._s.github_repo],
                tags=["autopilot", f"issue-{issue_number}"],
                max_acu_limit=self._s.max_acu_per_session,
                structured_output_schema=STRUCTURED_OUTPUT_SCHEMA,
            )
        except Exception as exc:
            # Never leave the trigger label on a run we failed to start, or the
            # issue poller would retry it forever.
            logger.exception("failed to create session for issue #%s", issue_number)
            self._store.update(
                run["id"],
                state="failed",
                outcome="failed",
                error=str(exc),
                finished_at=_now(),
            )
            self._store.add_event(run["id"], "error", str(exc))
            await self._gh_write(
                run["id"],
                f"remove_label {self._s.trigger_label}",
                self._github.remove_label,
                issue_number,
                self._s.trigger_label,
            )
            await self._gh_write(
                run["id"],
                "comment startup failure",
                self._github.comment,
                issue_number,
                f"Autopilot could not start a Devin session: `{exc}`",
            )
            return

        self._store.update(
            run["id"],
            session_id=session["session_id"],
            session_url=session.get("url"),
        )
        self._store.add_event(
            run["id"],
            "session_created",
            f"session_id={session['session_id']} url={session.get('url')}",
        )
        await self._gh_write(
            run["id"],
            f"add_label {self._s.running_label}",
            self._github.add_label,
            issue_number,
            self._s.running_label,
        )
        await self._gh_write(
            run["id"],
            "comment session started",
            self._github.comment,
            issue_number,
            f"Devin session started: {session.get('url')}\n"
            f"ACU cap: {self._s.max_acu_per_session}",
        )

    async def session_poll_tick(self) -> None:
        """Poll in-flight sessions and finalize the ones that reached a terminal state."""
        for run in self._store.by_state("running"):
            if not run["session_id"]:
                # Claimed but session create never landed (e.g. process killed
                # mid-dispatch). Fail stale claims so they can't wedge a slot.
                if _older_than(run["started_at"], seconds=300):
                    error = "session creation interrupted"
                    self._store.update(
                        run["id"],
                        state="failed",
                        outcome="failed",
                        error=error,
                        finished_at=_now(),
                    )
                    self._store.add_event(run["id"], "error", error)
                    await self._gh_write(
                        run["id"],
                        f"remove_label {self._s.trigger_label}",
                        self._github.remove_label,
                        run["issue_number"],
                        self._s.trigger_label,
                    )
                continue
            try:
                session = await self._devin.get_session(run["session_id"])
            except Exception:
                # A failed poll is transient; the next tick retries.
                logger.exception("poll failed for session %s", run["session_id"])
                continue
            self._record_status_change(
                run["id"], session.get("status"), session.get("status_detail")
            )
            self._record_acus(run, session.get("acus_consumed"))
            if session_is_terminal(session.get("status"), session.get("status_detail")):
                try:
                    await self._finalize(run, session)
                except Exception:
                    # One poisoned run must not starve the others; the tick
                    # retries it next pass.
                    logger.exception("finalize failed for run %s", run["id"])

    async def _finalize(self, run: dict, session: dict) -> None:
        # Only the session poll loop calls this, and its ticks run serially,
        # so this re-check cannot race within a process. It guards against a
        # run having been finalized between the poll listing and this call.
        if self._store.get(run["id"])["state"] != "running":
            return
        issue_number = run["issue_number"]
        output = session.get("structured_output") or {}
        outcome = output.get("outcome")
        status, detail = session.get("status"), session.get("status_detail")
        pr_url = next(
            (p["pr_url"] for p in session.get("pull_requests", []) if p.get("pr_url")),
            None,
        )
        blockers = output.get("blockers") or ""
        acus = session.get("acus_consumed")
        # Consumption only grows and live polls may already have stored a
        # higher value; never let the finalize snapshot move it backwards.
        if run["acus"] is not None and (acus is None or acus < run["acus"]):
            acus = run["acus"]

        if outcome == "fixed" and pr_url:
            state, label = "pr_open", self._s.pr_open_label
        elif outcome == "fixed":
            state, label = "needs_human", self._s.needs_human_label
            blockers = _append(blockers, "reported fixed but no pull request was found")
        elif outcome in ("needs_human", "not_reproducible"):
            state, label = "needs_human", self._s.needs_human_label
        elif status == "error":
            state, label = "failed", None
        else:
            # Suspended/exited/finished without a usable report. If it hit a
            # usage limit, say so plainly; either way a human must look.
            state, label = "needs_human", self._s.needs_human_label
            reason = (
                f"session hit its ACU limit ({detail})"
                if detail in QUOTA_DETAILS
                else f"session ended without structured output (status={status}, detail={detail})"
            )
            blockers = _append(blockers, reason)
            outcome = outcome or "needs_human"

        # Remove the trigger/running labels BEFORE the state update. Once the
        # run leaves an active state the issue poller may adopt it again, so
        # the trigger label must be gone first. Removal is idempotent
        # (404-tolerant), so a retry after a mid-finalize crash is safe.
        await self._gh_write(
            run["id"],
            f"remove_label {self._s.trigger_label}",
            self._github.remove_label,
            issue_number,
            self._s.trigger_label,
        )
        await self._gh_write(
            run["id"],
            f"remove_label {self._s.running_label}",
            self._github.remove_label,
            issue_number,
            self._s.running_label,
        )

        stored_outcome = outcome or ("failed" if state == "failed" else None)
        self._store.update(
            run["id"],
            state=state,
            pr_url=pr_url,
            outcome=stored_outcome,
            summary=output.get("summary"),
            tests_run=output.get("tests_run"),
            tests_passed=output.get("tests_passed"),
            acus=acus,
            output_json=json.dumps(output) if output else None,
            error=None if state != "failed" else f"status={status} detail={detail}",
            finished_at=_now(),
        )
        if state == "failed":
            self._store.add_event(
                run["id"], "error", f"status={status} detail={detail}"
            )
        # Recorded before the remaining write-backs: if one of them fails the
        # run is still finalized, and the event log should say so.
        self._store.add_event(
            run["id"], "finalized", f"outcome={stored_outcome} state={state}"
        )

        if label:
            await self._gh_write(
                run["id"],
                f"add_label {label}",
                self._github.add_label,
                issue_number,
                label,
            )
        await self._gh_write(
            run["id"],
            "comment result",
            self._github.comment,
            issue_number,
            _finish_comment(
                outcome=outcome or state,
                session_url=run["session_url"],
                pr_url=pr_url,
                summary=output.get("summary"),
                tests_run=output.get("tests_run"),
                tests_passed=output.get("tests_passed"),
                risk_notes=output.get("risk_notes"),
                blockers=blockers,
                acus=acus,
                error=f"status={status} detail={detail}" if state == "failed" else None,
            ),
        )

        # Sessions that end on a finished/waiting_for_user detail are still
        # "running" and would linger suspended until inactivity timeout.
        # Terminate explicitly so they close out promptly.
        if status == "running" and run["session_id"]:
            try:
                await self._devin.terminate_session(run["session_id"])
                self._store.add_event(run["id"], "session_terminated")
            except Exception:
                logger.exception(
                    "terminate failed for session %s", run["session_id"]
                )

    async def merge_check_tick(self) -> None:
        """Resolve pr_open runs whose PR reached a terminal state on GitHub:
        merged -> state 'merged', closed-unmerged -> state 'rejected'. Also
        runs once at startup as a backfill for PRs that transitioned while
        the service was down."""
        for run in self._store.by_state("pr_open"):
            if not run["pr_url"]:
                continue
            try:
                resolution = _PR_RESOLUTIONS.get(
                    await self._github.pr_status(run["pr_url"])
                )
                if resolution is None:
                    continue
                state, label_attr, event = resolution
                new_label = getattr(self._s, label_attr)
                # Swap labels BEFORE the state update: a GitHub failure
                # leaves the run in pr_open so the next tick retries.
                await self._gh_write(
                    run["id"],
                    f"remove_label {self._s.pr_open_label}",
                    self._github.remove_label,
                    run["issue_number"],
                    self._s.pr_open_label,
                )
                await self._gh_write(
                    run["id"],
                    f"add_label {new_label}",
                    self._github.add_label,
                    run["issue_number"],
                    new_label,
                )
                self._store.update(run["id"], state=state)
                self._store.add_event(run["id"], event, run["pr_url"])
            except Exception:
                # One poisoned run must not starve the others; the tick
                # retries it next pass.
                logger.exception("merge check failed for run %s", run["id"])

    async def acu_backfill_tick(self) -> None:
        """Re-poll recently-finalized sessions for ACU usage. Usage posts to
        the session object asynchronously, so the finalize-time snapshot is
        often stale; keep refreshing until the session reports 'exit' (a
        session_ended marker stops further polls) or the settle window ends."""
        finalized = self._store.by_state(
            "pr_open", "merged", "rejected", "needs_human", "failed"
        )
        for run in finalized:
            if not run["session_id"] or not run["finished_at"]:
                continue
            if _older_than(run["finished_at"], ACU_SETTLE_SECONDS):
                continue
            if any(
                e["event"] == "session_ended"
                for e in self._store.events_for(run["id"])
            ):
                continue
            try:
                session = await self._devin.get_session(run["session_id"])
            except Exception:
                logger.exception("ACU backfill failed for run %s", run["id"])
                continue
            self._record_acus(run, session.get("acus_consumed"), event=True)
            if session.get("status") == "exit":
                self._store.add_event(run["id"], "session_ended")


def _append(existing: str, note: str) -> str:
    return f"{existing}\n{note}".strip() if existing else note


OUTCOME_VERDICTS = {
    "fixed": "fix proposed",
    "needs_human": "needs human review",
    "not_reproducible": "not reproducible",
    "failed": "failed",
}


def _finish_comment(**f) -> str:
    verdict = OUTCOME_VERDICTS.get(f["outcome"], f["outcome"])
    lines = [
        f"Autopilot finished: {verdict}",
        f"Session: {f['session_url']}",
        f"PR: {f['pr_url'] or 'none'}",
        f"Summary: {f['summary'] or 'n/a'}",
        f"Tests run: {f['tests_run']}, tests passed: {f['tests_passed']}",
        f"Risk notes: {f['risk_notes'] or 'n/a'}",
        f"Blockers: {f['blockers'] or 'none'}",
        f"ACUs used: {f['acus']}",
    ]
    if f["error"]:
        lines.append(f"Error: {f['error']}")
    return "\n".join(lines)


def _minutes_between(runs, start_key: str, end_key: str):
    return [
        (
            datetime.fromisoformat(r[end_key]) - datetime.fromisoformat(r[start_key])
        ).total_seconds()
        / 60
        for r in runs
        if r[start_key] and r[end_key]
    ]


def compute_metrics(runs, settings) -> dict:
    by_state = {}
    for run in runs:
        by_state[run["state"]] = by_state.get(run["state"], 0) + 1

    total = len(runs)
    merged = by_state.get("merged", 0)
    rejected = by_state.get("rejected", 0)
    pr_opened = merged + by_state.get("pr_open", 0) + rejected
    finished = (
        merged
        + by_state.get("pr_open", 0)
        + rejected
        + by_state.get("needs_human", 0)
        + by_state.get("failed", 0)
    )

    # started_at -> finished_at: active session time.
    latencies = _minutes_between(runs, "started_at", "finished_at")
    median_latency = statistics.median(latencies) if latencies else None
    # created_at -> finished_at: label applied to a result, over finished runs.
    label_to_result = _minutes_between(runs, "created_at", "finished_at")
    median_label_to_result = (
        statistics.median(label_to_result) if label_to_result else None
    )
    # created_at -> finished_at over runs that produced a PR.
    label_to_pr = _minutes_between(
        [r for r in runs if r["pr_url"]], "created_at", "finished_at"
    )
    median_label_to_pr = statistics.median(label_to_pr) if label_to_pr else None

    total_acus = sum(r["acus"] or 0 for r in runs)
    runs_at_cap = sum(
        1
        for r in runs
        if r["finished_at"] and (r["acus"] or 0) >= settings.max_acu_per_session
    )
    # A finished run with no reported usage is "pending" — Devin posts ACUs
    # asynchronously (or not at all). Pending runs are excluded from the ACU
    # averages so a lagged 0.0 can't understate them.
    measured = [r for r in runs if (r["acus"] or 0) > 0]
    acus_pending = sum(
        1 for r in runs if r["finished_at"] and not (r["acus"] or 0)
    )
    measured_finished = sum(1 for r in measured if r["finished_at"])
    measured_prs = sum(1 for r in measured if r["pr_url"])
    measured_merged = sum(1 for r in measured if r["state"] == "merged")

    # Devin doesn't always meter per-session usage, so session wall-clock
    # time (started_at -> finished_at) is the reliable cost proxy.
    total_session_minutes = sum(latencies)

    return {
        "total_runs": total,
        "by_state": by_state,
        "simulate": settings.simulate,
        "repo": settings.github_repo,
        "max_acu_per_session": settings.max_acu_per_session,
        # Funnel counts distinct issues at every stage: an issue remediated
        # twice still counts once per stage. Retries still show up in ACU
        # totals. "sessions" keys on session_id — a Devin session really
        # existed — not started_at, which is stamped on dispatch claim
        # even when create_session fails.
        "funnel": {
            "sessions": len(
                {r["issue_number"] for r in runs if r["session_id"]}
            ),
            "prs": len({r["issue_number"] for r in runs if r["pr_url"]}),
            "merged": len(
                {r["issue_number"] for r in runs if r["state"] == "merged"}
            ),
            "rejected": len(
                {r["issue_number"] for r in runs if r["state"] == "rejected"}
            ),
        },
        "issues_completed": finished,
        # pr_rate: fraction of finished runs that produced a PR.
        "pr_rate": pr_opened / finished if finished else 0.0,
        # merge_rate: of the PRs Devin opened, how many were merged.
        "merge_rate": merged / pr_opened if pr_opened else 0.0,
        # resolution_rate: of finished runs, how many shipped a merged fix.
        "resolution_rate": merged / finished if finished else 0.0,
        "median_latency_minutes": median_latency,
        "median_label_to_result_minutes": median_label_to_result,
        "median_label_to_pr_minutes": median_label_to_pr,
        "total_acus": total_acus,
        "acus_pending": acus_pending,
        "acus_per_run": (
            (total_acus / measured_finished) if measured_finished else None
        ),
        "acus_per_pr": (
            (total_acus / measured_prs) if measured_prs else None
        ),
        "acus_per_merged_fix": (
            (total_acus / measured_merged) if measured_merged else None
        ),
        "runs_at_cap": runs_at_cap,
        "total_session_minutes": total_session_minutes,
        "session_minutes_per_run": (
            (total_session_minutes / len(latencies)) if latencies else None
        ),
        "session_minutes_per_merged_fix": (
            (total_session_minutes / merged) if merged else None
        ),
    }
