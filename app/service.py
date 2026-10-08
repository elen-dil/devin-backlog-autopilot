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

    async def enqueue_issue(self, number: int, title: str, url: str, body: str) -> str:
        run_id = self._store.enqueue(number, title, url, body or "")
        if run_id is None:
            logger.info("issue #%s already has an active run; skipping", number)
            return "already_active"
        logger.info("queued issue #%s (run %s)", number, run_id)
        return "queued"

    async def issue_poll_tick(self) -> None:
        """Fallback trigger: adopt open issues carrying the trigger label."""
        issues = await self._github.list_labeled_issues(self._s.trigger_label)
        for issue in issues:
            await self.enqueue_issue(
                issue["number"],
                issue["title"],
                issue["html_url"],
                issue.get("body") or "",
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
                run["id"], state="failed", error=str(exc), finished_at=_now()
            )
            await self._github.remove_label(issue_number, self._s.trigger_label)
            await self._github.comment(
                issue_number,
                f"Autopilot could not start a Devin session: `{exc}`",
            )
            return

        self._store.update(
            run["id"],
            session_id=session["session_id"],
            session_url=session.get("url"),
        )
        await self._github.add_label(issue_number, self._s.running_label)
        await self._github.comment(
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
                    self._store.update(
                        run["id"],
                        state="failed",
                        error="session creation interrupted",
                        finished_at=_now(),
                    )
                    await self._github.remove_label(
                        run["issue_number"], self._s.trigger_label
                    )
                continue
            try:
                session = await self._devin.get_session(run["session_id"])
            except Exception:
                # A failed poll is transient; the next tick retries.
                logger.exception("poll failed for session %s", run["session_id"])
                continue
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
        await self._github.remove_label(issue_number, self._s.trigger_label)
        await self._github.remove_label(issue_number, self._s.running_label)

        self._store.update(
            run["id"],
            state=state,
            pr_url=pr_url,
            outcome=outcome or ("failed" if state == "failed" else None),
            summary=output.get("summary"),
            tests_run=output.get("tests_run"),
            tests_passed=output.get("tests_passed"),
            acus=session.get("acus_consumed"),
            output_json=json.dumps(output) if output else None,
            error=None if state != "failed" else f"status={status} detail={detail}",
            finished_at=_now(),
        )

        if label:
            await self._github.add_label(issue_number, label)
        await self._github.comment(
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
                acus=session.get("acus_consumed"),
                error=f"status={status} detail={detail}" if state == "failed" else None,
            ),
        )

    async def merge_check_tick(self) -> None:
        """Mark pr_open runs as merged once GitHub reports the PR merged."""
        for run in self._store.by_state("pr_open"):
            if run["pr_url"] and await self._github.pr_is_merged(run["pr_url"]):
                self._store.update(run["id"], state="merged", merged_at=_now())


def _append(existing: str, note: str) -> str:
    return f"{existing}\n{note}".strip() if existing else note


def _finish_comment(**f) -> str:
    lines = [
        f"Autopilot finished: outcome `{f['outcome']}`",
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


def compute_metrics(runs) -> dict:
    by_state = {}
    for run in runs:
        by_state[run["state"]] = by_state.get(run["state"], 0) + 1

    total = len(runs)
    merged = by_state.get("merged", 0)
    pr_opened = merged + by_state.get("pr_open", 0)

    latencies = [
        (datetime.fromisoformat(r["finished_at"]) - datetime.fromisoformat(r["started_at"]))
        .total_seconds() / 60
        for r in runs
        if r["started_at"] and r["finished_at"]
    ]
    median_latency = statistics.median(latencies) if latencies else None

    total_acus = sum(r["acus"] or 0 for r in runs)

    finished = merged + by_state.get("pr_open", 0) + by_state.get(
        "needs_human", 0
    ) + by_state.get("failed", 0)

    return {
        "total_runs": total,
        "by_state": by_state,
        # pr_rate: fraction of all runs that produced a PR.
        "pr_rate": pr_opened / total if total else 0.0,
        # merge_rate: of the PRs Devin opened, how many were merged.
        "merge_rate": merged / pr_opened if pr_opened else 0.0,
        # resolution_rate: of finished runs, how many shipped a merged fix.
        "resolution_rate": merged / finished if finished else 0.0,
        "median_latency_minutes": median_latency,
        "total_acus": total_acus,
        "acus_per_merged_fix": (total_acus / merged) if merged else None,
    }
