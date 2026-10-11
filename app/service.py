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

# author_association values on issue comments that imply write access.
REVIEWER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}

# Issue comments the service posts itself. They go out under the
# maintainer's token, so they carry a write-access author_association and
# the approval flow must never adopt one as the reviewer decision. Both
# the comment writers and the _resolve_approval filter reference these.
_COMMENT_SESSION_STARTED = "Devin session started:"
_COMMENT_RESULT = "Autopilot finished:"
_COMMENT_START_FAILED = "Autopilot could not start"
_COMMENT_NEEDS_DECISION = "Autopilot: "
SERVICE_COMMENT_PREFIXES = (
    _COMMENT_SESSION_STARTED,
    _COMMENT_RESULT,
    _COMMENT_START_FAILED,
    _COMMENT_NEEDS_DECISION,
)

# PR review states that count as human feedback. APPROVED is tracked
# separately: it emits pr_approved, not review_feedback.
FEEDBACK_REVIEW_STATES = {"CHANGES_REQUESTED", "COMMENTED"}


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


def _parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_bot(user) -> bool:
    user = user or {}
    return user.get("type") == "Bot" or (user.get("login") or "").endswith(
        "[bot]"
    )


def _is_devin_bot(user) -> bool:
    return _is_bot(user) and "devin" in (user.get("login") or "").lower()


def _trusted_actor(item) -> bool:
    """Write-association human actor — the same bar approval decisions
    already use on issue comments."""
    return (
        item.get("author_association") in REVIEWER_ASSOCIATIONS
        and not _is_bot(item.get("user"))
    )


def _has_aside(body) -> bool:
    """Comments tagged "(aside)" are conversational, not actionable
    feedback."""
    return "(aside)" in (body or "").lower()


def _is_service_comment(body) -> bool:
    """Comments the service posts itself can never be a reviewer's
    approval decision."""
    return (body or "").startswith(SERVICE_COMMENT_PREFIXES)


class AutopilotService:
    def __init__(self, settings, store, devin, github):
        self._s = settings
        self._store = store
        self._devin = devin
        self._github = github

    async def enqueue_issue(
        self,
        number: int,
        title: str,
        url: str,
        body: str,
        source: str,
        approved: bool = False,
    ) -> str:
        run_id = self._store.enqueue(
            number,
            title,
            url,
            body or "",
            is_simulated=self._s.simulate,
            approved=approved,
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
            labels = {
                (l.get("name") if isinstance(l, dict) else l)
                for l in issue.get("labels", [])
            }
            await self.enqueue_issue(
                issue["number"],
                issue["title"],
                issue["html_url"],
                issue.get("body") or "",
                source="poller",
                approved=self._s.approval_label in labels,
            )

    async def dispatch_tick(self) -> None:
        """Promote queued runs into Devin sessions while slots are free."""
        slots = self._s.max_concurrent_sessions - self._store.count_state("running")
        if slots <= 0:
            return
        for run in self._store.oldest_queued(slots):
            await self._start_run(run)

    async def _resolve_approval(self, run: dict):
        """Reviewer decision for an approved run, or None. The decision is
        the latest comment by a write-access author posted after the issue's
        most recent finished run."""
        issue_number = run["issue_number"]
        previous = self._store.latest_finished_for_issue(issue_number)
        since = _parse_ts(previous["finished_at"]) if previous else None
        comments = await self._github.list_issue_comments(issue_number)
        epoch = datetime.min.replace(tzinfo=timezone.utc)
        decisions = [
            c
            for c in comments
            if c.get("author_association") in REVIEWER_ASSOCIATIONS
            and not _is_service_comment(c.get("body"))
            and (
                since is None
                or (_parse_ts(c.get("created_at")) or epoch) > since
            )
        ]
        if not decisions:
            return None
        previous_output = None
        if previous and previous.get("output_json"):
            try:
                previous_output = json.loads(previous["output_json"])
            except ValueError:
                previous_output = None
        return {
            "decision": decisions[-1].get("body") or "",
            "approver": await self._approval_actor(issue_number, decisions[-1]),
            "previous_output": previous_output,
        }

    async def _approval_actor(self, issue_number: int, decision_comment):
        """Login of whoever applied the approval label; the decision
        comment's author is the fallback when no label event is found."""
        try:
            events = await self._github.list_issue_events(issue_number)
        except Exception:
            events = []
        for e in reversed(events):
            if (
                e.get("event") == "labeled"
                and (e.get("label") or {}).get("name") == self._s.approval_label
            ):
                return (e.get("actor") or {}).get("login")
        return (decision_comment.get("user") or {}).get("login")

    async def _fail_without_decision(self, run: dict) -> None:
        """devin-approved with no reviewer comment: fail the run, ask for
        the decision, and strip both labels so the poller can't loop."""
        issue_number = run["issue_number"]
        error = f"{self._s.approval_label} set but no reviewer decision found"
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
            issue_number,
            self._s.trigger_label,
        )
        await self._gh_write(
            run["id"],
            f"remove_label {self._s.approval_label}",
            self._github.remove_label,
            issue_number,
            self._s.approval_label,
        )
        await self._gh_write(
            run["id"],
            "comment approval needed",
            self._github.comment,
            issue_number,
            f"{_COMMENT_NEEDS_DECISION}`{self._s.approval_label}` is set, "
            "but no reviewer decision was found. A maintainer with write "
            "access must post a "
            "comment stating the decision, then re-apply "
            f"`{self._s.trigger_label}` and `{self._s.approval_label}`.",
        )

    async def _start_run(self, run: dict) -> None:
        issue_number = run["issue_number"]
        approval = None
        if run.get("approved"):
            try:
                approval = await self._resolve_approval(run)
            except Exception:
                # Transient GitHub failure: keep the run queued so the
                # next dispatch tick retries rather than failing spuriously.
                logger.exception(
                    "approval check failed for run %s; retrying next tick",
                    run["id"],
                )
                return
            if approval is None:
                await self._fail_without_decision(run)
                return
        # A retry can land while the previous run's outcome labels are
        # still on the issue; strip them so the board reflects the new
        # attempt, not the old result. Removal is 404-tolerant and goes
        # through _gh_write like every label mutation.
        await self._gh_write(
            run["id"],
            f"remove_label {self._s.needs_human_label}",
            self._github.remove_label,
            issue_number,
            self._s.needs_human_label,
        )
        await self._gh_write(
            run["id"],
            f"remove_label {self._s.pr_rejected_label}",
            self._github.remove_label,
            issue_number,
            self._s.pr_rejected_label,
        )
        # Claim the run before the async call: if the process dies between
        # create_session and writing the session_id, the stale-claim sweep in
        # session_poll_tick fails the run instead of re-dispatching it and
        # spawning a second (orphaned) session for the same issue.
        self._store.update(run["id"], state="running", started_at=_now())
        if approval is not None:
            self._store.add_event(
                run["id"],
                "approval_applied",
                json.dumps(
                    {
                        "approver": approval["approver"],
                        "decision": approval["decision"],
                    }
                ),
            )
        try:
            session = await self._devin.create_session(
                prompt=build_prompt(
                    issue_number=issue_number,
                    title=run["title"],
                    url=run["issue_url"],
                    body=run["issue_body"],
                    repo=self._s.github_repo,
                    approval=approval,
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
            if run.get("approved"):
                await self._gh_write(
                    run["id"],
                    f"remove_label {self._s.approval_label}",
                    self._github.remove_label,
                    issue_number,
                    self._s.approval_label,
                )
            await self._gh_write(
                run["id"],
                "comment startup failure",
                self._github.comment,
                issue_number,
                f"{_COMMENT_START_FAILED} a Devin session: `{exc}`",
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
            f"{_COMMENT_SESSION_STARTED} {session.get('url')}\n"
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
            # Time-to-PR anchors on the PR's creation, not session finish:
            # as soon as a session reports a PR, persist it and stamp a
            # pr_opened event (once per run).
            pr_url = next(
                (
                    p["pr_url"]
                    for p in session.get("pull_requests", [])
                    if p.get("pr_url")
                ),
                None,
            )
            if pr_url and not any(
                e["event"] == "pr_opened"
                for e in self._store.events_for(run["id"])
            ):
                self._store.update(run["id"], pr_url=pr_url)
                self._store.add_event(run["id"], "pr_opened", pr_url)
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
        ) or run["pr_url"]  # an earlier poll may already have recorded it
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
        if run.get("approved"):
            # devin-approved is single-use: consumed by this run.
            await self._gh_write(
                run["id"],
                f"remove_label {self._s.approval_label}",
                self._github.remove_label,
                issue_number,
                self._s.approval_label,
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
            tests_failed=output.get("tests_failed"),
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
                tests_failed=output.get("tests_failed"),
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

    async def _review_scan(self, run: dict) -> None:
        """One pass over a run's PR: record human review feedback and
        approvals as events, match Devin's replies to each feedback item,
        and roll the outcome up onto the run. Safe to re-run — every event
        dedupes on the GitHub object's external_id."""
        pr_url = run["pr_url"]
        if not pr_url:
            return
        reviews = await self._github.list_pr_reviews(pr_url)
        review_comments = await self._github.list_pr_review_comments(pr_url)
        issue_comments = await self._github.list_pr_issue_comments(pr_url)
        commits = await self._github.list_pr_commits(pr_url)

        def _item(gh_id, obj, ts_key):
            return {
                "gh_id": gh_id,
                "author": (obj.get("user") or {}).get("login"),
                "url": obj.get("html_url"),
                "excerpt": (obj.get("body") or "")[:200],
                "ts": _parse_ts(obj.get(ts_key)),
            }

        review_ids = {r.get("id") for r in reviews if r.get("id") is not None}
        feedback = []
        approvals = []
        for r in reviews:
            if not _trusted_actor(r) or _has_aside(r.get("body")):
                continue
            if r.get("state") in FEEDBACK_REVIEW_STATES:
                item = _item(f"review:{r.get('id')}", r, "submitted_at")
                if item["ts"] is not None:
                    feedback.append(item)
            elif r.get("state") == "APPROVED":
                item = _item(f"approval:{r.get('id')}", r, "submitted_at")
                if item["ts"] is not None:
                    approvals.append(item)
        # Inline review comments that belong to a fetched review fold into
        # that review; only orphaned ones count as feedback on their own.
        for c in review_comments:
            if c.get("pull_request_review_id") in review_ids:
                continue
            if not _trusted_actor(c) or _has_aside(c.get("body")):
                continue
            item = _item(f"rcomment:{c.get('id')}", c, "created_at")
            if item["ts"] is not None:
                feedback.append(item)
        for c in issue_comments:
            if not _trusted_actor(c) or _has_aside(c.get("body")):
                continue
            item = _item(f"comment:{c.get('id')}", c, "created_at")
            if item["ts"] is not None:
                feedback.append(item)

        # A feedback item is addressed once Devin replies after it; no
        # commit is required (the reply itself is the round boundary).
        devin_replies = []
        for kind, c in (
            [("issue", c) for c in issue_comments]
            + [("review", c) for c in review_comments]
        ):
            if not _is_devin_bot(c.get("user")):
                continue
            ts = _parse_ts(c.get("created_at"))
            if ts is None:
                continue
            devin_replies.append(
                {
                    "key": f"{kind}:{c.get('id')}",
                    "ts": ts,
                    "url": c.get("html_url"),
                }
            )
        devin_replies.sort(key=lambda d: d["ts"])

        commit_list = []
        for c in commits:
            detail = c.get("commit") or {}
            ts = _parse_ts(
                (detail.get("committer") or {}).get("date")
                or (detail.get("author") or {}).get("date")
            )
            if ts is None or not c.get("sha"):
                continue
            commit_list.append({"sha": c["sha"], "ts": ts})

        feedback.sort(key=lambda f: f["ts"])
        approvals.sort(key=lambda a: a["ts"])
        addressing = {}  # reply key -> reply; one reply can close many items
        round_shas = set()
        unaddressed = []
        for item in feedback:
            self._store.add_event(
                run["id"],
                "review_feedback",
                json.dumps(
                    {
                        "id": item["gh_id"],
                        "author": item["author"],
                        "url": item["url"],
                        "excerpt": item["excerpt"],
                    }
                ),
                external_id=item["gh_id"],
            )
            reply = next(
                (d for d in devin_replies if d["ts"] > item["ts"]), None
            )
            if reply is None:
                unaddressed.append(item)
                continue
            addressing[reply["key"]] = reply
            shas = sorted(
                {
                    c["sha"]
                    for c in commit_list
                    if item["ts"] < c["ts"] <= reply["ts"]
                }
            )
            round_shas.update(shas)
            minutes = round(
                (reply["ts"] - item["ts"]).total_seconds() / 60, 1
            )
            self._store.add_event(
                run["id"],
                "feedback_addressed",
                json.dumps(
                    {
                        "id": item["gh_id"],
                        "reply_url": reply["url"],
                        "minutes": minutes,
                        "commits": shas,
                    }
                ),
                external_id=f"addr:{item['gh_id']}",
            )
        for item in approvals:
            self._store.add_event(
                run["id"],
                "pr_approved",
                json.dumps(
                    {
                        "id": item["gh_id"],
                        "author": item["author"],
                        "url": item["url"],
                    }
                ),
                external_id=item["gh_id"],
            )

        latest_approval = max((a["ts"] for a in approvals), default=None)
        if run["state"] == "rejected":
            review_status = "rejected"
        elif unaddressed:
            review_status = "changes_requested"
        elif latest_approval is not None and all(
            f["ts"] <= latest_approval for f in feedback
        ):
            review_status = "approved"
        elif addressing:
            review_status = "addressed"
        else:
            review_status = "none"
        self._store.update(
            run["id"],
            review_rounds=len(addressing),
            review_commits=len(round_shas),
            review_status=review_status,
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
                # Scan before resolving so a PR that merged or closed since
                # the last tick still gets its final review state captured.
                await self._review_scan(run)
            except Exception:
                # Review tracking is best-effort; it must never block a
                # merge/reject resolution.
                logger.exception("review scan failed for run %s", run["id"])
            try:
                resolution = _PR_RESOLUTIONS.get(
                    await self._github.pr_status(run["pr_url"])
                )
                if resolution is None:
                    continue
                state, label_attr, event = resolution
                new_label = getattr(self._s, label_attr)
                # Capture the reason before mutating anything: a fetch
                # failure leaves the run untouched for the next tick.
                rejection_reason = None
                if state == "rejected":
                    rejection_reason = await self._github.latest_pr_comment(
                        run["pr_url"]
                    )
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
                update = {"state": state, "rejection_reason": rejection_reason}
                if state == "rejected":
                    update["review_status"] = "rejected"
                self._store.update(run["id"], **update)
                self._store.add_event(run["id"], event, run["pr_url"])
            except Exception:
                # One poisoned run must not starve the others; the tick
                # retries it next pass.
                logger.exception("merge check failed for run %s", run["id"])

    async def review_backfill_tick(self) -> None:
        """Backfill PR-derived data for every run that has a PR, whatever
        its state. Runs once at startup: it stamps pr_opened events for
        runs that predate them (the PR's GitHub created_at is the real
        open time), stores the closing comment on rejected runs that
        predate the rejection_reason column, picks up feedback posted
        while the service was down, and computes review_rounds for
        already-merged runs, which the periodic merge_check_tick never
        rescans."""
        for run in self._store.all():
            if not run["pr_url"]:
                continue
            if not any(
                e["event"] == "pr_opened"
                for e in self._store.events_for(run["id"])
            ):
                try:
                    created_at = await self._github.pr_created_at(
                        run["pr_url"]
                    )
                    if created_at:
                        self._store.add_event(
                            run["id"],
                            "pr_opened",
                            run["pr_url"],
                            timestamp=created_at,
                        )
                except Exception:
                    # A failed fetch just retries on the next startup; the
                    # metrics fallback to finished_at covers the gap.
                    logger.exception(
                        "pr_opened backfill failed for run %s", run["id"]
                    )
            if run["state"] == "rejected" and run["rejection_reason"] is None:
                try:
                    reason = await self._github.latest_pr_comment(
                        run["pr_url"]
                    )
                    if reason:
                        self._store.update(
                            run["id"], rejection_reason=reason
                        )
                except Exception:
                    logger.exception(
                        "rejection reason backfill failed for run %s",
                        run["id"],
                    )
            try:
                await self._review_scan(run)
            except Exception:
                # One poisoned run must not starve the others.
                logger.exception("review backfill failed for run %s", run["id"])

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
        f"{_COMMENT_RESULT} {verdict}",
        f"Session: {f['session_url']}",
        f"PR: {f['pr_url'] or 'none'}",
        f"Summary: {f['summary'] or 'n/a'}",
        f"Tests run: {f['tests_run']}, tests passed: {f['tests_passed']}, "
        f"tests failed: {f['tests_failed']}",
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


def compute_metrics(runs, settings, events=None) -> dict:
    by_state = {}
    for run in runs:
        by_state[run["state"]] = by_state.get(run["state"], 0) + 1

    total = len(runs)
    merged = by_state.get("merged", 0)
    rejected = by_state.get("rejected", 0)
    failed = by_state.get("failed", 0)
    finished = (
        merged
        + by_state.get("pr_open", 0)
        + rejected
        + by_state.get("needs_human", 0)
        + failed
    )

    # Issue-level metrics key on each issue's latest run — max id, the
    # ordering issues_index uses (a missing id sorts as 0) — so a
    # retried issue counts once by where it ended, not once per attempt.
    by_issue = {}
    for run in runs:
        by_issue.setdefault(run["issue_number"], []).append(run)
    latest = {
        n: sorted(rs, key=lambda r: r.get("id") or 0)[-1]
        for n, rs in by_issue.items()
    }
    issues_attempted = len(by_issue)
    issues_merged = sum(1 for r in latest.values() if r["state"] == "merged")
    policy_stops_awaiting = sum(
        1 for r in latest.values() if r["state"] == "needs_human"
    )
    # A stop is waived when an approved retry ran later on the same
    # issue; an approved run with no earlier needs_human run on its
    # issue is not a waiver.
    policy_stops_waived = sum(
        1
        for rs in by_issue.values()
        for r in rs
        if r.get("approved")
        and any(
            o["state"] == "needs_human"
            and (o.get("id") or 0) < (r.get("id") or 0)
            for o in rs
        )
    )

    # started_at -> finished_at: active session time.
    latencies = _minutes_between(runs, "started_at", "finished_at")
    median_latency = statistics.median(latencies) if latencies else None
    # created_at -> finished_at: label applied to a result, over finished runs.
    label_to_result = _minutes_between(runs, "created_at", "finished_at")
    median_label_to_result = (
        statistics.median(label_to_result) if label_to_result else None
    )
    # created_at -> PR creation over runs that produced a PR. The pr_opened
    # event carries the real open time; runs that predate it (or whose
    # backfill fetch failed) fall back to finished_at.
    pr_opened_at = {}
    for e in events or []:
        if e.get("event") == "pr_opened":
            pr_opened_at.setdefault(
                e.get("run_id"), _parse_ts(e.get("timestamp"))
            )
    label_to_pr = []
    for r in runs:
        if not r.get("pr_url"):
            continue
        start = _parse_ts(r.get("created_at"))
        end = pr_opened_at.get(r.get("id")) or _parse_ts(r.get("finished_at"))
        if start and end:
            label_to_pr.append((end - start).total_seconds() / 60)
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

    # Median minutes from feedback posted to Devin's addressing reply,
    # parsed out of the feedback_addressed event details.
    address_minutes = []
    for e in events or []:
        if e.get("event") != "feedback_addressed":
            continue
        try:
            detail = json.loads(e.get("detail") or "")
        except ValueError:
            continue
        minutes = detail.get("minutes")
        if isinstance(minutes, (int, float)):
            address_minutes.append(minutes)

    return {
        "total_runs": total,
        "by_state": by_state,
        "simulate": settings.simulate,
        "repo": settings.github_repo,
        "max_acu_per_session": settings.max_acu_per_session,
        # ACU usage settles for this long after finalize; the dashboard
        # shows "not reported" once the window closes.
        "acu_settle_seconds": ACU_SETTLE_SECONDS,
        # The funnel counts runs (not distinct issues) at every stage, so
        # the terminal stages partition the PR count: merged + awaiting
        # merge + rejected == prs. awaiting_merge is keyed on pr_url and
        # covers runs still in "running" (a session that already reported
        # its PR) as well as pr_open. "sessions" keys on session_id — a
        # Devin session really existed — not started_at, which is stamped
        # on dispatch claim even when create_session fails.
        "funnel": {
            "sessions": sum(1 for r in runs if r["session_id"]),
            "prs": sum(1 for r in runs if r["pr_url"]),
            "merged": merged,
            "awaiting_merge": sum(
                1
                for r in runs
                if r["pr_url"] and r["state"] in ("running", "pr_open")
            ),
            "rejected": rejected,
        },
        "issues_completed": finished,
        # fix_rate: of the issues attempted, the fraction whose latest
        # run merged. Retries re-attempt the same issue without
        # diluting the denominator.
        "issues_attempted": issues_attempted,
        "issues_merged": issues_merged,
        "fix_rate": (
            issues_merged / issues_attempted if issues_attempted else 0.0
        ),
        # pr_acceptance_rate: of the PRs a human ruled on, how many
        # merged. pr_open runs are undecided and stay out of the
        # denominator.
        "prs_decided": merged + rejected,
        "pr_acceptance_rate": (
            merged / (merged + rejected) if merged + rejected else 0.0
        ),
        # Policy stops count runs: every needs_human is a stop. Waived
        # stops were picked back up by an approved retry; awaiting
        # stops are issues still parked on a needs_human latest run.
        "policy_stops": by_state.get("needs_human", 0),
        "policy_stops_waived": policy_stops_waived,
        "policy_stops_awaiting": policy_stops_awaiting,
        # failed_rate is over all runs: a crashed session is a cost
        # whether or not it ever reached a decision.
        "failed_runs": failed,
        "failed_rate": failed / total if total else 0.0,
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
        "median_feedback_address_minutes": (
            statistics.median(address_minutes) if address_minutes else None
        ),
        "feedback_address_count": len(address_minutes),
    }


def issues_index(runs, events_for) -> list:
    """Per-issue aggregates for the dashboard: latest state, attempt
    count, and each run's parsed output plus event log, oldest first.
    Ordered by most recent activity (latest run id) descending."""
    by_issue = {}
    for run in runs:
        by_issue.setdefault(run["issue_number"], []).append(run)
    issues = []
    for number, issue_runs in by_issue.items():
        ordered = sorted(issue_runs, key=lambda r: r["id"])
        latest = ordered[-1]
        entries = []
        for r in ordered:
            output = None
            if r.get("output_json"):
                try:
                    output = json.loads(r["output_json"])
                except ValueError:
                    output = None
            entries.append({**r, "output": output, "events": events_for(r["id"])})
        issues.append(
            {
                "issue_number": number,
                "title": latest["title"],
                "issue_url": latest["issue_url"],
                "latest_state": latest["state"],
                "attempts": len(ordered),
                "runs": entries,
            }
        )
    issues.sort(key=lambda i: i["runs"][-1]["id"], reverse=True)
    return issues
