"""Autopilot dashboard. Reads only from the FastAPI endpoints, never the DB."""

import json
import os
from datetime import datetime, timezone

import altair as alt
import httpx
import pandas as pd
import streamlit as st

API = os.environ.get("FASTAPI_BASE_URL", "http://localhost:8000")

# Raw enum -> plain-language label. Raw state/outcome names are never shown.
STATE_LABELS = {
    "queued": "Queued",
    "running": "Running",
    "pr_open": "PR awaiting review",
    "merged": "Merged",
    "needs_human": "Needs human",
    "failed": "Failed",
}
STATE_ORDER = list(STATE_LABELS)
OUTCOME_LABELS = {
    "fixed": "Fixed",
    "needs_human": "Needs human",
    "not_reproducible": "Not reproducible",
}

ACTIVE_STATES = {"queued", "running"}
TERMINAL_STATES = {"merged", "needs_human", "failed"}

ACU_HELP = (
    "ACU = Agent Compute Unit: Devin's unit of compute billing. "
    "Each session is capped at MAX_ACU_PER_SESSION."
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def api_get(path: str):
    resp = httpx.get(f"{API}{path}", timeout=10)
    resp.raise_for_status()
    return resp.json()


def parse_ts(value):
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def fmt_delta(start, end):
    """Human-readable '4m' / '1h 5m' / '2d 3h' between two ISO timestamps."""
    start_ts, end_ts = parse_ts(start), parse_ts(end)
    if start_ts is None or end_ts is None:
        return "—"
    secs = max(0, int((end_ts - start_ts).total_seconds()))
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    if hours < 48:
        return f"{hours}h {mins % 60}m"
    return f"{hours // 24}d {hours % 24}h"


def fmt_minutes(value):
    return "n/a" if value is None else f"{value:.1f} min"


def fmt_number(value):
    return "n/a" if value is None else f"{value:.1f}"


def fmt_pct(value):
    return "n/a" if value is None else f"{value:.0%}"


def state_label(state):
    if not state:
        return "Unknown"
    return STATE_LABELS.get(state, state.replace("_", " ").title())


def outcome_label(outcome):
    if not outcome:
        return "—"
    return OUTCOME_LABELS.get(outcome, outcome.replace("_", " ").title())


def pretty_event(name):
    return str(name or "unknown").replace("_", " ").title()


def short_title(run, limit=48):
    title = (run.get("title") or "").strip()
    return title if len(title) <= limit else title[: limit - 1] + "…"


def issue_ref(run):
    number = run.get("issue_number")
    prefix = f"#{number}" if number is not None else f"run {run.get('id')}"
    return f"{prefix} {short_title(run)}".strip()


def parse_output(run):
    """Return the parsed output_json dict, or None."""
    raw = run.get("output_json")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def tests_text(run):
    total = run.get("tests_run")
    if total is None:
        return "—"
    return f"{run.get('tests_passed') or 0}/{total}"


def by_state_counts(metrics, runs):
    counts = {state: 0 for state in STATE_ORDER}
    raw = metrics.get("by_state") or {}
    if raw:
        for state, count in raw.items():
            if state in counts:
                counts[state] = count
    else:
        # Older API shape: derive from the runs list.
        for run in runs:
            if run.get("state") in counts:
                counts[run["state"]] += 1
    return counts


def funnel_counts(metrics, runs):
    funnel = metrics.get("funnel") or {}
    if funnel:
        return [
            ("Labeled", funnel.get("labeled", 0)),
            ("Sessions started", funnel.get("sessions_started", 0)),
            ("PRs opened", funnel.get("prs_opened", 0)),
            ("Merged", funnel.get("merged", 0)),
        ]
    # Older API shape: derive from the runs list.
    return [
        ("Labeled", metrics.get("total_runs", len(runs))),
        ("Sessions started", sum(1 for r in runs if r.get("session_id"))),
        ("PRs opened", sum(1 for r in runs if r.get("pr_url"))),
        ("Merged", sum(1 for r in runs if r.get("state") == "merged")),
    ]


def is_finished(run):
    return bool(run.get("finished_at")) or run.get("state") in TERMINAL_STATES


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def render_banner(health, metrics):
    repo = metrics.get("repo") or "unknown repo"
    refreshed = datetime.now().strftime("%H:%M:%S")
    if health.get("simulate"):
        st.warning(
            f"**SIMULATE MODE (fake data)** — {repo} — last refreshed {refreshed}"
        )
    else:
        st.success(f"**LIVE** — {repo} — last refreshed {refreshed}")


def render_status(metrics, runs):
    st.header("Status")
    st.caption("What is happening right now?")

    counts = by_state_counts(metrics, runs)
    cols = st.columns(len(STATE_ORDER))
    for col, state in zip(cols, STATE_ORDER):
        col.metric(
            STATE_LABELS[state],
            counts[state],
            help=f"Number of runs currently in the '{STATE_LABELS[state]}' state.",
        )

    st.subheader("Active now")
    active = [r for r in runs if r.get("state") in ACTIVE_STATES]
    if not active:
        st.caption("Nothing queued or running right now.")
        return
    now = datetime.now(timezone.utc).isoformat()
    df = pd.DataFrame(
        [
            {
                "Issue": issue_ref(r),
                "Status": state_label(r.get("state")),
                "Elapsed": fmt_delta(r.get("created_at"), now),
                "Session": r.get("session_url"),
            }
            for r in active
        ]
    )
    st.dataframe(
        df,
        column_config={"Session": st.column_config.LinkColumn("Session")},
        hide_index=True,
        width="stretch",
    )


def render_effectiveness(metrics, runs):
    st.header("Effectiveness")
    st.caption("Is Devin's work good?")

    cols = st.columns(3)
    cols[0].metric(
        "Resolution rate",
        fmt_pct(metrics.get("resolution_rate")),
        help="Share of finished runs that ended in a merged fix. "
        "Formula: merged runs ÷ finished runs.",
    )
    cols[1].metric(
        "Merge rate",
        fmt_pct(metrics.get("merge_rate")),
        help="Share of opened PRs that were merged. "
        "Formula: merged ÷ PRs opened.",
    )
    cols[2].metric(
        "PR rate",
        fmt_pct(metrics.get("pr_rate")),
        help="Share of finished runs that produced a pull request. "
        "Formula: PRs opened ÷ finished runs.",
    )

    left, right = st.columns(2)

    with left:
        st.subheader("Pipeline funnel")
        funnel = funnel_counts(metrics, runs)
        if all(count == 0 for _, count in funnel):
            st.caption("No issues have entered the pipeline yet.")
        else:
            df = pd.DataFrame(
                {
                    "Stage": [s for s, _ in funnel],
                    "Count": [c for _, c in funnel],
                    "order": range(len(funnel)),
                }
            )
            chart = (
                alt.Chart(df)
                .mark_bar()
                .encode(
                    x=alt.X("Count:Q", title="Issues"),
                    y=alt.Y(
                        "Stage:N",
                        sort=alt.EncodingSortField(field="order", order="ascending"),
                        title=None,
                    ),
                    tooltip=["Stage", "Count"],
                )
            )
            st.altair_chart(chart, width="stretch")

    with right:
        st.subheader("Outcome breakdown")
        finished = [r for r in runs if is_finished(r)]
        if not finished:
            st.caption("No finished runs yet.")
        else:
            outcome_counts = {}
            for r in finished:
                label = outcome_label(r.get("outcome"))
                outcome_counts[label] = outcome_counts.get(label, 0) + 1
            df = pd.DataFrame(
                [
                    {"Outcome": k, "Runs": v}
                    for k, v in sorted(
                        outcome_counts.items(), key=lambda kv: kv[1], reverse=True
                    )
                ]
            )
            chart = (
                alt.Chart(df)
                .mark_bar()
                .encode(
                    x=alt.X("Runs:Q", title="Runs"),
                    y=alt.Y("Outcome:N", sort="-x", title=None),
                    tooltip=["Outcome", "Runs"],
                )
            )
            st.altair_chart(chart, width="stretch")

    st.subheader("Blocked — needs a human")
    blocked = [r for r in runs if r.get("state") == "needs_human"]
    if not blocked:
        st.caption("No runs are waiting on a human.")
    else:
        for r in blocked:
            report = parse_output(r) or {}
            blocker = report.get("blockers") or r.get("error") or "No blocker detail recorded."
            url = r.get("issue_url")
            ref = f"[{issue_ref(r)}]({url})" if url else issue_ref(r)
            st.markdown(f"- {ref} — {blocker}")


def render_throughput(metrics, runs):
    st.header("Throughput")
    st.caption("How fast is the backlog moving?")

    issues_completed = metrics.get("issues_completed")
    if issues_completed is None:
        issues_completed = sum(1 for r in runs if is_finished(r))

    cols = st.columns(3)
    cols[0].metric(
        "Issues completed",
        issues_completed,
        help="Issues whose run reached a final result (merged, needs human, or failed).",
    )
    cols[1].metric(
        "Median time to result",
        fmt_minutes(metrics.get("median_label_to_result_minutes")),
        help="Median minutes from an issue being labeled to its run finishing. "
        "Formula: median(finish time − labeled time).",
    )
    cols[2].metric(
        "Median time to PR",
        fmt_minutes(metrics.get("median_label_to_pr_minutes")),
        help="Median minutes from an issue being labeled to its PR opening. "
        "Formula: median(PR opened time − labeled time).",
    )

    st.subheader("Completed over time")
    finished = sorted(
        (r for r in runs if parse_ts(r.get("finished_at"))),
        key=lambda r: parse_ts(r["finished_at"]),
    )
    if not finished:
        st.caption("No runs have finished yet.")
        return
    df = pd.DataFrame(
        {
            "Finished": [parse_ts(r["finished_at"]) for r in finished],
            "Completed": range(1, len(finished) + 1),
        }
    )
    chart = (
        alt.Chart(df)
        .mark_line(point=True)
        .encode(
            x=alt.X("Finished:T", title="Finished at"),
            y=alt.Y("Completed:Q", title="Runs completed"),
            tooltip=[
                alt.Tooltip("Finished:T", title="Finished at"),
                alt.Tooltip("Completed:Q", title="Runs completed"),
            ],
        )
    )
    st.altair_chart(chart, width="stretch")


def render_cost(metrics, runs):
    st.header("Cost")
    st.caption("What is this costing?")

    total_acus = metrics.get("total_acus") or 0
    price = metrics.get("acu_price_usd")
    cap = metrics.get("max_acu_per_session")
    runs_at_cap = metrics.get("runs_at_cap")
    if runs_at_cap is None and cap is not None:
        runs_at_cap = sum(
            1 for r in runs if r.get("acus") is not None and r["acus"] >= cap
        )

    cols = st.columns(5)
    cols[0].metric(
        "Total ACUs",
        f"{total_acus:.1f}",
        help=f"Sum of ACUs consumed across all runs. {ACU_HELP}",
    )
    cols[1].metric(
        "Estimated cost",
        f"${total_acus * price:,.2f}" if price is not None else "set ACU_PRICE_USD",
        help=f"Total ACUs × ACU_PRICE_USD. {ACU_HELP}",
    )
    cols[2].metric(
        "ACUs per run",
        fmt_number(metrics.get("acus_per_run")),
        help=f"Total ACUs ÷ finished runs. {ACU_HELP}",
    )
    cols[3].metric(
        "ACUs per PR opened",
        fmt_number(metrics.get("acus_per_pr")),
        help=f"Total ACUs ÷ PRs opened. {ACU_HELP}",
    )
    cols[4].metric(
        "ACUs per merged fix",
        fmt_number(metrics.get("acus_per_merged_fix")),
        help=f"Total ACUs ÷ merged fixes. {ACU_HELP}",
    )

    st.subheader("ACUs per run")
    billable = [r for r in runs if r.get("acus") is not None]
    if not billable:
        st.caption("No ACU usage recorded yet.")
        return
    df = pd.DataFrame(
        {
            "Run": [issue_ref(r) for r in billable],
            "ACUs": [r["acus"] for r in billable],
            "order": range(len(billable)),
            "at_cap": [
                bool(cap is not None and r["acus"] >= cap) for r in billable
            ],
        }
    )
    x = alt.X(
        "Run:N",
        sort=alt.EncodingSortField(field="order", order="ascending"),
        title="Run",
        axis=alt.Axis(labelAngle=-45),
    )
    base = alt.Chart(df).mark_bar().encode(
        x=x, y=alt.Y("ACUs:Q"), tooltip=["Run", "ACUs"]
    )
    layers = [base]
    if cap is not None:
        layers.append(
            alt.Chart(df)
            .mark_bar(color="orange")
            .transform_filter(alt.datum.at_cap == True)  # noqa: E712
            .encode(x=x, y=alt.Y("ACUs:Q"), tooltip=["Run", "ACUs"])
        )
        layers.append(
            alt.Chart(pd.DataFrame({"cap": [cap]}))
            .mark_rule(strokeDash=[6, 4])
            .encode(y="cap:Q")
        )
    st.altair_chart(alt.layer(*layers), width="stretch")
    if cap is not None:
        if runs_at_cap:
            st.caption(
                f"{runs_at_cap} run(s) hit the MAX_ACU_PER_SESSION cap "
                f"({cap}) — highlighted in orange."
            )
        else:
            st.caption(f"No runs have hit the MAX_ACU_PER_SESSION cap ({cap}).")


def render_runs(runs):
    st.header("Runs")
    if not runs:
        st.caption("No runs yet — waiting for issues labeled `devin-remediate`.")
        return

    df = pd.DataFrame(
        [
            {
                "Issue": issue_ref(r),
                "Status": state_label(r.get("state")),
                "Outcome": outcome_label(r.get("outcome")),
                "Time to result": fmt_delta(r.get("created_at"), r.get("finished_at")),
                "ACUs used": "—" if r.get("acus") is None else f"{r['acus']:.1f}",
                "Tests": tests_text(r),
                "Session": r.get("session_url"),
                "PR": r.get("pr_url"),
            }
            for r in runs
        ]
    )
    event = st.dataframe(
        df,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "Session": st.column_config.LinkColumn("Session"),
            "PR": st.column_config.LinkColumn("PR"),
        },
        hide_index=True,
        width="stretch",
    )

    selected = event.selection.rows
    if not selected:
        st.caption("Select a row to inspect a run.")
        return
    render_run_detail(runs[selected[0]])


def render_run_detail(run):
    st.divider()
    st.subheader(issue_ref(run))

    links = []
    if run.get("issue_url"):
        links.append(f"[Issue]({run['issue_url']})")
    if run.get("session_url"):
        links.append(f"[Devin session]({run['session_url']})")
    if run.get("pr_url"):
        links.append(f"[Pull request]({run['pr_url']})")
    st.markdown(" · ".join(links) if links else "No links recorded yet.")

    st.caption(
        f"Status: {state_label(run.get('state'))} · "
        f"Outcome: {outcome_label(run.get('outcome'))} · "
        f"ACUs: {run.get('acus') if run.get('acus') is not None else '—'} · "
        f"Tests: {tests_text(run)}"
    )
    if run.get("error"):
        st.error(f"Error: {run['error']}")

    report = parse_output(run)
    st.markdown("##### Devin's report")
    if report is None:
        st.info("Devin has not posted a structured report for this run yet.")
    else:
        st.markdown(f"**Summary:** {report.get('summary') or '—'}")
        st.markdown(f"**Root cause:** {report.get('root_cause') or '—'}")
        files = report.get("files_changed") or []
        if files:
            st.markdown("**Files changed:**")
            for path in files:
                st.markdown(f"- `{path}`")
        else:
            st.markdown("**Files changed:** —")
        st.markdown(
            f"**Tests:** {report.get('tests_passed') or 0}/{report.get('tests_run') or 0} passed"
        )
        st.markdown(f"**Risk notes:** {report.get('risk_notes') or '—'}")
        st.markdown(f"**Blockers:** {report.get('blockers') or '—'}")

    st.markdown("##### Events")
    try:
        events = api_get(f"/api/v1/runs/{run['id']}/events").get("events", [])
    except Exception as exc:
        st.warning(f"Couldn't load events for this run: {exc}")
        return
    if not events:
        st.caption("No events recorded for this run.")
        return
    rows = []
    for e in events:
        ts = parse_ts(e.get("timestamp"))
        rows.append(
            {
                "Time": ts.strftime("%Y-%m-%d %H:%M:%S") if ts else "—",
                "Event": pretty_event(e.get("event")),
                "Detail": e.get("detail") or "—",
            }
        )
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Devin Backlog Autopilot", layout="wide")
st.title("Devin Backlog Autopilot")


@st.fragment(run_every=10)
def render():
    try:
        health = api_get("/healthz")
        metrics = api_get("/api/v1/metrics")
        runs = api_get("/api/v1/runs").get("runs", [])
    except Exception as exc:
        st.error(
            f"Can't reach the API at {API} ({exc}). "
            "The dashboard will keep retrying automatically."
        )
        return

    render_banner(health, metrics)
    render_status(metrics, runs)
    render_effectiveness(metrics, runs)
    render_throughput(metrics, runs)
    render_cost(metrics, runs)
    render_runs(runs)


render()
