"""Autopilot dashboard. Reads only from the FastAPI endpoints, never the DB."""

import html
import json
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import altair as alt
import httpx
import pandas as pd
import streamlit as st

API = os.environ.get("FASTAPI_BASE_URL", "http://localhost:8000")

# Business timezone for all rendered timestamps. DISPLAY_TZ comes from the
# environment (compose sets env_file); invalid values fall back to UTC.
DISPLAY_TZ_NAME = os.environ.get("DISPLAY_TZ", "America/New_York")
try:
    DISPLAY_TZ = ZoneInfo(DISPLAY_TZ_NAME)
    DISPLAY_TZ_ERROR = None
except Exception:
    DISPLAY_TZ = timezone.utc
    DISPLAY_TZ_ERROR = DISPLAY_TZ_NAME
    DISPLAY_TZ_NAME = "UTC"

TS_FORMAT = "%b %d %H:%M:%S %Z"

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
    "failed": "Failed",
}

TERMINAL_STATES = {"merged", "needs_human", "failed"}

ACU_HELP = (
    "ACU = Agent Compute Unit: Devin's unit of compute billing. "
    "Each session is capped at MAX_ACU_PER_SESSION."
)

# Dataframe row height is ~35px; header counts as one row.
TABLE_ROW_HEIGHT = 35
TABLE_MAX_ROWS = 8

# Auto-refresh cadence for the dashboard fragment. An invalid or
# non-positive value falls back to 10s rather than crashing the page.
try:
    REFRESH_SECONDS = float(os.environ.get("DASHBOARD_REFRESH_SECONDS", "10"))
    if REFRESH_SECONDS <= 0:
        raise ValueError
except ValueError:
    REFRESH_SECONDS = 10.0


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


def fmt_ts(value):
    """Render a timestamp in DISPLAY_TZ, e.g. 'Oct 08 14:32:05 EDT'."""
    ts = parse_ts(value)
    if ts is None:
        return "—"
    return ts.astimezone(DISPLAY_TZ).strftime(TS_FORMAT)


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


def friendly_github_write(detail):
    """Translate 'github_write' detail '<op>: <result>' -> (text, is_error)."""
    op, sep, result = detail.partition(":")
    if not sep:
        return f"GitHub write ({detail or 'no detail'})", False
    op, result = op.strip(), result.strip()

    if op.startswith("add_label"):
        label = op[len("add_label"):].strip()
        base = f"Added label {label}" if label else "Added label"
    elif op.startswith("remove_label"):
        label = op[len("remove_label"):].strip()
        if label == "devin-remediate":
            base = "Removed trigger label"
        elif label:
            base = f"Removed label {label}"
        else:
            base = "Removed label"
    elif op.startswith("comment"):
        context = op[len("comment"):].strip()
        base = {
            "session started": "Posted session link comment",
            "result": "Posted result comment",
            "startup failure": "Posted failure comment",
        }.get(context, "Posted comment")
    else:
        base = f"GitHub write: {op}"

    if not result or result == "ok":
        return base, False
    if result == "skipped (simulate)":
        return f"{base} (skipped in simulate mode)", False
    if result.startswith("failed"):
        return f"{base} — failed", True
    return f"{base} ({result})", False


def friendly_event(event):
    """Translate a raw run event into (plain-English text, is_error)."""
    name = event.get("event") or ""
    detail = (event.get("detail") or "").strip()

    if name == "queued":
        source = re.search(r"source=(\S+)", detail)
        if source:
            label = {
                "webhook": "webhook",
                "poller": "poller",
                "simulate": "simulate injection",
            }.get(source.group(1), source.group(1))
            return f"Queued (via {label})", False
        return "Queued", False

    if name == "session_created":
        return "Devin session started", False

    if name == "status_change":
        status_m = re.search(r"status=(\S+)", detail)
        detail_m = re.search(r"detail=(.*)$", detail)
        status = status_m.group(1) if status_m else None
        sub = detail_m.group(1).strip() if detail_m else ""
        if status == "running" and sub == "working":
            return "Devin is working", False
        if status == "running" and sub == "finished":
            return "Devin finished", False
        if status == "running" and sub == "waiting_for_user":
            return "Devin is waiting for user input", False
        if status == "suspended":
            return f"Session suspended ({sub or 'no detail'})", False
        if status == "exit":
            return "Session exited", False
        if status == "error":
            return "Session errored", False
        if status:
            return f"Session status: {status} ({sub or '—'})", False
        return f"Status changed ({detail or 'no detail'})", False

    if name == "github_write":
        return friendly_github_write(detail)

    if name == "finalized":
        outcome_m = re.search(r"outcome=(\S+)", detail)
        state_m = re.search(r"state=(\S+)", detail)
        if outcome_m:
            outcome = outcome_m.group(1)
            label = OUTCOME_LABELS.get(outcome)
            if label is None and outcome == "failed":
                label = "Failed"
            if label is None and state_m:
                label = state_label(state_m.group(1))
            if label is None:
                label = outcome.replace("_", " ").title()
            return f"Finished: {label}", False
        if state_m:
            return f"Finished: {state_label(state_m.group(1))}", False
        return f"Finished ({detail or 'no detail'})", False

    if name == "error":
        return f"Error: {detail or 'unknown'}", True

    text = pretty_event(name)
    if detail:
        text = f"{text} — {detail}"
    return text, False


def short_title(run, limit=48):
    title = (run.get("title") or "").strip()
    if limit is None or len(title) <= limit:
        return title
    # Cut at a word boundary so labels don't end mid-word; a hard cut is
    # the fallback when no space fits inside the limit.
    cut = title[: limit - 1].rsplit(" ", 1)[0] or title[: limit - 1]
    return cut + "…"


def issue_ref(run, limit=48):
    number = run.get("issue_number")
    prefix = f"#{number}" if number is not None else f"run {run.get('id')}"
    return f"{prefix} {short_title(run, limit)}".strip()


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
    if DISPLAY_TZ_ERROR is not None and not st.session_state.get("_tz_warned"):
        st.session_state["_tz_warned"] = True
        st.warning(
            f"Invalid DISPLAY_TZ '{DISPLAY_TZ_ERROR}' — falling back to UTC."
        )
    repo = metrics.get("repo") or "unknown repo"
    refreshed = datetime.now(DISPLAY_TZ).strftime(TS_FORMAT)
    if health.get("simulate"):
        st.caption(
            f":orange[**SIMULATE MODE** (fake data)] · {repo} · "
            f"refreshed {refreshed}"
        )
    else:
        st.caption(f":green[**LIVE**] · {repo} · refreshed {refreshed}")


def _kpi_tile(label, value, help_text):
    """Render one KPI tile as HTML with a CSS ::after tooltip.

    Streamlit's `help=` tooltips live in a portal that can outlive its anchor
    when the auto-refresh fragment recreates the metric DOM, leaving orphan
    tooltip boxes that overlap newer ones. A native `title` tooltip stays
    bound to its element but is painted by the browser chrome: it carries a
    hover delay, can be canceled when the fragment refresh recreates the DOM
    mid-hover, and does not render at all in some embedded views. A CSS
    ::after tooltip renders inside the page, so it has none of those issues
    while remaining bound to the element.
    """
    tip = html.escape(help_text, quote=True)
    return (
        '<div class="kpi">'
        f'<div class="kpi-label">{html.escape(label)}'
        f'<span class="kpi-help" data-tip="{tip}">?</span></div>'
        f'<div class="kpi-value">{html.escape(value)}</div>'
        "</div>"
    )


def render_kpis(metrics, runs):
    """One always-visible row of grouped KPI tiles."""
    counts = by_state_counts(metrics, runs)
    status_col, eff_col, thr_col, cost_col = st.columns([4, 4, 3, 4])

    with status_col:
        st.caption("**Status**")
        st.markdown(
            _kpi_tile(
                "Active",
                f"{counts['running']} running · {counts['queued']} queued",
                "Runs with a live Devin session, plus labeled issues waiting "
                "for a free session slot (MAX_CONCURRENT_SESSIONS).",
            ),
            unsafe_allow_html=True,
        )

    with eff_col:
        st.caption("**Effectiveness**")
        res_col, merge_col = st.columns(2)
        res_col.markdown(
            _kpi_tile(
                "Resolution rate",
                fmt_pct(metrics.get("resolution_rate")),
                "Share of finished runs that ended in a merged fix. "
                "Formula: merged runs ÷ finished runs.",
            ),
            unsafe_allow_html=True,
        )
        merge_col.markdown(
            _kpi_tile(
                "Merge rate",
                fmt_pct(metrics.get("merge_rate")),
                "Share of opened PRs that were merged. "
                "Formula: merged ÷ PRs opened.",
            ),
            unsafe_allow_html=True,
        )

    with thr_col:
        st.caption("**Throughput**")
        st.markdown(
            _kpi_tile(
                "Median time to PR",
                fmt_minutes(metrics.get("median_label_to_pr_minutes")),
                "Median minutes from an issue being labeled to its PR "
                "opening. Formula: median(PR opened time − labeled time).",
            ),
            unsafe_allow_html=True,
        )

    with cost_col:
        st.caption("**Cost**")
        total_col, per_fix_col = st.columns(2)
        total_col.markdown(
            _kpi_tile(
                "Total ACUs",
                f"{(metrics.get('total_acus') or 0):.1f}",
                f"Sum of ACUs consumed across all runs. {ACU_HELP}",
            ),
            unsafe_allow_html=True,
        )
        per_fix_col.markdown(
            _kpi_tile(
                "ACUs / merged fix",
                fmt_number(metrics.get("acus_per_merged_fix")),
                "Total ACUs ÷ merged fixes. Includes compute spent on runs "
                f"that did not merge. {ACU_HELP}",
            ),
            unsafe_allow_html=True,
        )


def render_funnel(metrics, runs):
    funnel = funnel_counts(metrics, runs)
    if all(count == 0 for _, count in funnel):
        st.caption("No issues have entered the pipeline yet.")
        return
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
            x=alt.X(
                "Count:Q",
                title="Issues",
                axis=alt.Axis(format="d", tickMinStep=1),
            ),
            y=alt.Y(
                "Stage:N",
                sort=alt.EncodingSortField(field="order", order="ascending"),
                title=None,
            ),
            tooltip=["Stage", "Count"],
        )
        .properties(height=180)
    )
    st.altair_chart(chart, width="stretch")


def render_needs_human(runs):
    blocked = [r for r in runs if r.get("state") == "needs_human"]
    if not blocked:
        st.caption("No runs are waiting on a human.")
        return
    for r in blocked:
        report = parse_output(r) or {}
        blocker = (
            report.get("blockers")
            or r.get("error")
            or "No blocker detail recorded."
        )
        url = r.get("issue_url")
        ref = f"[{issue_ref(r)}]({url})" if url else issue_ref(r)
        st.markdown(f"- {ref} — {blocker}")


def render_runs_table(runs):
    if not runs:
        st.caption("No runs yet — waiting for issues labeled `devin-remediate`.")
        return

    # Attention order: in-flight work first, then blockers, then finished
    # runs, each group most recent first.
    state_rank = {
        "running": 0,
        "queued": 1,
        "needs_human": 2,
        "pr_open": 3,
        "failed": 4,
        "merged": 5,
    }

    def sort_key(run):
        ts = (
            parse_ts(run.get("finished_at"))
            or parse_ts(run.get("started_at"))
            or parse_ts(run.get("created_at"))
            or datetime.min.replace(tzinfo=timezone.utc)
        )
        return (state_rank.get(run.get("state"), 9), -ts.timestamp())

    ordered = sorted(runs, key=sort_key)
    now = datetime.now(DISPLAY_TZ).isoformat()
    df = pd.DataFrame(
        [
            {
                # Full title in the cell; the grid ellipsizes overflow, so
                # truncating here too would lose the tail twice.
                "Issue": issue_ref(r, limit=None),
                "Status": state_label(r.get("state")),
                # For unfinished runs this column shows elapsed time instead.
                "Time to result": fmt_delta(
                    r.get("created_at"), r.get("finished_at") or now
                ),
                "ACUs": "—" if r.get("acus") is None else f"{r['acus']:.1f}",
                "Session": r.get("session_url"),
                "PR": r.get("pr_url"),
                "Details": "View",
            }
            for r in ordered
        ]
    )
    # LinkColumn renders NaN/None literally; use empty string for no link.
    for col in ("Session", "PR"):
        df[col] = df[col].where(df[col].notna(), "")
    # Cap the table at ~8 rows so the Overview tab fits one laptop screen.
    height = min(
        TABLE_ROW_HEIGHT * (TABLE_MAX_ROWS + 1),
        TABLE_ROW_HEIGHT * (len(df) + 1) + 4,
    )
    event = st.dataframe(
        df,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "Issue": st.column_config.TextColumn(width=320),
            "Status": st.column_config.TextColumn(width=120),
            "Time to result": st.column_config.TextColumn(width=85),
            "ACUs": st.column_config.TextColumn(width=50),
            "Session": st.column_config.LinkColumn(
                "Session", display_text="Open", width=65
            ),
            "PR": st.column_config.LinkColumn(
                "PR", display_text="Open", width=45
            ),
            "Details": st.column_config.ButtonColumn(
                "Details", key="run_detail_click", width=65
            ),
        },
        height=height,
        hide_index=True,
        width="stretch",
    )

    # ButtonColumn clicks are transient (set only during the click rerun), so
    # the fragment auto-refresh never reopens a dismissed dialog.
    click = st.session_state.get("run_detail_click")
    if click is not None:
        run_detail_dialog(ordered[click["row"]])
        return

    # Row selection also opens the dialog, but only when the selection
    # changes; a persisted selection would otherwise reopen it every tick.
    selected = event.selection.rows
    if selected == st.session_state.get("_selected_row"):
        return
    st.session_state["_selected_row"] = selected
    if selected:
        run_detail_dialog(ordered[selected[0]])


@st.dialog("Run details", width="large")
def run_detail_dialog(run):
    st.markdown(f"##### {issue_ref(run, limit=None)}")

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
    for e in events:
        text, is_error = friendly_event(e)
        marker = "⚠️ " if is_error else ""
        st.markdown(f"- {fmt_ts(e.get('timestamp'))} — {marker}{text}")
    with st.expander("Raw event details"):
        raw = pd.DataFrame(
            {
                "Time": [fmt_ts(e.get("timestamp")) for e in events],
                "Event": [e.get("event") or "—" for e in events],
                "Detail": [e.get("detail") or "—" for e in events],
            }
        )
        st.dataframe(raw, hide_index=True, width="stretch")


def render_overview(metrics, runs):
    table_col, side_col = st.columns([7, 4])
    with table_col:
        render_runs_table(runs)
    with side_col:
        st.markdown("##### Pipeline")
        render_funnel(metrics, runs)
        st.markdown("##### Needs human")
        render_needs_human(runs)


def render_trends(metrics, runs):
    left, right = st.columns(2)

    with left:
        st.markdown("##### Completed over time")
        finished = sorted(
            (r for r in runs if parse_ts(r.get("finished_at"))),
            key=lambda r: parse_ts(r["finished_at"]),
        )
        if not finished:
            st.caption("No runs have finished yet.")
        else:
            df = pd.DataFrame(
                {
                    "Finished": pd.to_datetime(
                        [r["finished_at"] for r in finished], utc=True
                    ),
                    "Completed": range(1, len(finished) + 1),
                }
            )
            # Wall-clock times in DISPLAY_TZ; dropping tzinfo keeps the axis
            # labels in the business timezone regardless of the viewer's
            # browser timezone.
            df["Finished"] = (
                df["Finished"].dt.tz_convert(DISPLAY_TZ).dt.tz_localize(None)
            )
            span = df["Finished"].max() - df["Finished"].min()
            # Bucket by hour within a day, by day beyond that. A cumulative
            # line was tried first but renders as a vertical spike whenever
            # several runs share a finish time.
            freq, time_format = (
                ("h", "%H:%M")
                if span < pd.Timedelta(hours=24)
                else ("D", "%b %d")
            )
            df["Bucket"] = df["Finished"].dt.floor(freq)
            # Reindex over the full bucket range so empty periods show as
            # gaps instead of being silently skipped.
            counts = (
                df.groupby("Bucket")
                .size()
                .reindex(
                    pd.date_range(
                        df["Bucket"].min(), df["Bucket"].max(), freq=freq
                    ),
                    fill_value=0,
                )
                .rename("Completed")
                .rename_axis("Bucket")
                .reset_index()
            )
            axis = alt.Axis(format=time_format)
            if freq == "D":
                # Auto ticks land mid-day and format to the same "%b %d"
                # label repeatedly; one tick per day avoids the duplicates.
                axis.tickCount = "day"
            chart = (
                alt.Chart(counts)
                .mark_bar()
                .encode(
                    x=alt.X(
                        "Bucket:T",
                        title=f"Finished at ({DISPLAY_TZ_NAME})",
                        axis=axis,
                    ),
                    y=alt.Y(
                        "Completed:Q",
                        title="Runs completed",
                        axis=alt.Axis(format="d", tickMinStep=1),
                    ),
                    tooltip=[
                        alt.Tooltip(
                            "Bucket:T",
                            title="Finished at",
                            format="%b %d %H:%M",
                        ),
                        alt.Tooltip("Completed:Q", title="Runs completed"),
                    ],
                )
            )
            st.altair_chart(chart, width="stretch")

    with right:
        st.markdown("##### Outcome breakdown")
        finished = [r for r in runs if is_finished(r)]
        if not finished:
            st.caption("No finished runs yet.")
        else:
            outcome_counts = {}
            for r in finished:
                # Runs that failed before producing a report have no
                # outcome; their state is the honest label.
                label = outcome_label(r.get("outcome") or r.get("state"))
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
                    x=alt.X(
                        "Runs:Q",
                        title="Runs",
                        axis=alt.Axis(format="d", tickMinStep=1),
                    ),
                    y=alt.Y("Outcome:N", sort="-x", title=None),
                    tooltip=["Outcome", "Runs"],
                )
            )
            st.altair_chart(chart, width="stretch")


def render_cost(metrics, runs):
    st.markdown("##### ACUs per run")
    cap = metrics.get("max_acu_per_session")
    runs_at_cap = metrics.get("runs_at_cap")
    if runs_at_cap is None and cap is not None:
        runs_at_cap = sum(
            1 for r in runs if r.get("acus") is not None and r["acus"] >= cap
        )

    billable = [r for r in runs if r.get("acus") is not None]
    if not billable:
        st.caption("No ACU usage recorded yet.")
        return
    df = pd.DataFrame(
        {
            "Run": [issue_ref(r) for r in billable],
            # Untruncated name for the hover tooltip; the y-axis label uses
            # the truncated Run value.
            "Full": [issue_ref(r, limit=None) for r in billable],
            "ACUs": [r["acus"] for r in billable],
            "at_cap": [
                bool(cap is not None and r["acus"] >= cap) for r in billable
            ],
        }
    )
    # Horizontal bars: run names read left-to-right on the y axis, so they
    # stay legible instead of truncating like rotated x-axis labels. Vega-Lite
    # axis labels can't carry tooltips; hovering a bar shows the full name.
    # Highest spend at the top.
    by_acus = sorted(billable, key=lambda r: r["acus"], reverse=True)
    y = alt.Y(
        "Run:N",
        sort=[issue_ref(r) for r in by_acus],
        title=None,
        axis=alt.Axis(labelLimit=340, labelOverlap=False),
    )
    base = alt.Chart(df).mark_bar().encode(
        x=alt.X("ACUs:Q", title="ACUs"),
        y=y,
        tooltip=[alt.Tooltip("Full:N", title="Run"), "ACUs"],
    )
    layers = [base]
    chart_height = max(160, 26 * len(billable))
    if cap is not None:
        layers.append(
            alt.Chart(df)
            .mark_bar(color="orange")
            .transform_filter(alt.datum.at_cap == True)  # noqa: E712
            .encode(
                x=alt.X("ACUs:Q"),
                y=y,
                tooltip=[alt.Tooltip("Full:N", title="Run"), "ACUs"],
            )
        )
        # Fixed color so the cap line and its label read on both light and
        # dark Streamlit themes; the theme's default rule color does not.
        # The label sits inside the bottom-left of the line: bars are sorted
        # longest-first, so the bottom row has the most open space next to
        # the cap. The cap layers reuse the "ACUs" title so the shared x
        # axis keeps its name instead of merging to "ACUs, cap".
        cap_df = pd.DataFrame({"cap": [cap]})
        layers.append(
            alt.Chart(cap_df)
            .mark_rule(strokeDash=[6, 4], color="#E45756")
            .encode(x=alt.X("cap:Q", title="ACUs"))
        )
        layers.append(
            alt.Chart(cap_df)
            .mark_text(
                text=f"cap ({cap:g})",
                align="right",
                dx=-6,
                baseline="bottom",
                color="#E45756",
            )
            .encode(
                x=alt.X("cap:Q", title="ACUs"),
                y=alt.value(chart_height - 8),
            )
        )
    st.altair_chart(
        alt.layer(*layers).properties(height=chart_height),
        width="stretch",
    )
    if cap is not None:
        if runs_at_cap:
            st.caption(
                f"{runs_at_cap} run(s) hit the MAX_ACU_PER_SESSION cap "
                f"({cap}) — highlighted in orange."
            )
        else:
            st.caption(f"No runs have hit the MAX_ACU_PER_SESSION cap ({cap}).")


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Devin Backlog Autopilot", layout="wide")
# Tighten Streamlit's default page padding so the KPI row plus the Overview
# tab fit a ~1440x900 laptop screen without scrolling.
st.markdown(
    """<style>
.block-container{padding-top:1.75rem;padding-bottom:1rem;}
.kpi-label{font-size:.875rem;color:rgba(49,51,63,.6);display:flex;align-items:center;gap:.35rem;}
.kpi-help{position:relative;display:inline-flex;align-items:center;justify-content:center;width:.95rem;height:.95rem;border-radius:50%;border:1px solid rgba(49,51,63,.45);font-size:.62rem;color:rgba(49,51,63,.55);cursor:help;}
.kpi-help::after{content:attr(data-tip);position:absolute;top:calc(100% + 7px);left:50%;transform:translateX(-50%);width:max-content;max-width:15rem;white-space:normal;text-align:left;background:#31333F;color:#fff;font-size:.75rem;font-weight:400;line-height:1.45;padding:.45rem .6rem;border-radius:.375rem;box-shadow:0 4px 12px rgba(0,0,0,.18);opacity:0;visibility:hidden;transition:opacity .12s;pointer-events:none;z-index:100;}
.kpi-help::before{content:"";position:absolute;top:calc(100% + 3px);left:50%;transform:translateX(-50%);border:4px solid transparent;border-bottom-color:#31333F;opacity:0;visibility:hidden;transition:opacity .12s;pointer-events:none;z-index:100;}
.kpi-help:hover::after,.kpi-help:hover::before{opacity:1;visibility:visible;}
.kpi-value{font-size:1.55rem;font-weight:400;color:#31333F;line-height:1.4;}
</style>""",
    unsafe_allow_html=True,
)
st.title("Devin Backlog Autopilot")


@st.fragment(run_every=REFRESH_SECONDS)
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
    render_kpis(metrics, runs)
    # on_change="rerun" makes tabs dynamic: only the open tab renders, so
    # charts never mount inside a hidden (zero-width) container and stay
    # correctly sized when switching.
    tab_overview, tab_trends, tab_cost = st.tabs(
        ["Overview", "Trends", "Cost"], on_change="rerun"
    )
    if tab_overview.open:
        with tab_overview:
            render_overview(metrics, runs)
    if tab_trends.open:
        with tab_trends:
            render_trends(metrics, runs)
    if tab_cost.open:
        with tab_cost:
            render_cost(metrics, runs)


render()
