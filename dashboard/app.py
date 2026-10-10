"""Autopilot dashboard. Reads only from the FastAPI endpoints, never the DB."""

import html
import json
import math
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
DATE_FORMAT = "%b %d"

# Raw enum -> plain-language label. Raw state/outcome names are never shown.
STATE_LABELS = {
    "queued": "Queued",
    "running": "Running",
    "pr_open": "Awaiting merge",
    "merged": "Merged",
    "rejected": "PR rejected",
    "needs_human": "Needs human",
    "failed": "Failed",
}
STATE_ORDER = list(STATE_LABELS)
OUTCOME_LABELS = {
    "fixed": "Fix proposed",
    "rejected": "PR rejected",
    "needs_human": "Needs human",
    "not_reproducible": "Not reproducible",
    "failed": "Failed",
}

TERMINAL_STATES = {"merged", "rejected", "needs_human", "failed"}

# ---------------------------------------------------------------------------
# Design system: exactly two font sizes and one accent, shared by the CSS
# block, the Altair theme, and .streamlit/config.toml. SMALL covers all
# text; LARGE is reserved for titles and KPI values.
# ---------------------------------------------------------------------------
FS_SMALL = 13  # px
FS_LARGE = 20  # px

# One accent for primary marks, one muted tone for secondary or flagged
# items (e.g. runs that hit the per-session ACU cap). Both read on light
# and dark backgrounds; keep them identical across modes.
ACCENT = "#4C78A8"
MUTED = "#9AA5B1"

CHART_HEIGHT = 220
CHART_PADDING = {"left": 4, "top": 4, "right": 4, "bottom": 4}


@alt.theme.register("dashboard", enable=True)
def _dashboard_theme() -> alt.theme.ThemeConfig:
    """One Vega-Lite config shared by every chart.

    Font sizes are pinned to the two-size type system; colors stay on the
    Streamlit chart theme so axis text and gridlines adapt to light/dark
    while marks keep one accent. Titles render in the DOM via
    section_title(), but config.title is set anyway so a stray
    .properties(title=...) can't introduce a third size.
    """
    return {
        "config": {
            "padding": CHART_PADDING,
            "view": {"stroke": None},
            "axis": {
                "labelFontSize": FS_SMALL,
                "titleFontSize": FS_SMALL,
                "labelFontWeight": 400,
                "titleFontWeight": 400,
                "grid": False,
            },
            # Vega's default label baseline leaves the first band label a
            # few px off its bar; "middle" centers every label exactly.
            "axisY": {"labelBaseline": "middle"},
            "legend": {
                "labelFontSize": FS_SMALL,
                "titleFontSize": FS_SMALL,
            },
            "title": {"fontSize": FS_LARGE, "fontWeight": 600},
            "mark": {"color": ACCENT},
        }
    }


ACU_HELP = (
    "ACU = Agent Compute Unit: Devin's unit of compute billing. Per-session "
    "usage isn't always reported, so session time (start to finish) is the "
    "reliable cost proxy; ACUs are shown where Devin reports them."
)

# Dataframe row height is ~35px; header counts as one row.
TABLE_ROW_HEIGHT = 35
TABLE_MAX_ROWS = 8
# Column widths for the runs table (Issue, Status, Approved, Date,
# Attempts, Time, ACUs, Session, PR, Details). The hover-tooltip script
# in render_overview() needs the same geometry, so both read these
# values.
TABLE_COL_WIDTHS = [260, 100, 70, 60, 65, 65, 60, 70, 50, 70]

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


def fmt_date(value):
    """Render just the date portion in DISPLAY_TZ, e.g. 'Oct 08'."""
    ts = parse_ts(value)
    if ts is None:
        return "—"
    return ts.astimezone(DISPLAY_TZ).strftime(DATE_FORMAT)


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


def acu_display(run) -> str:
    """ACU cell text: 'pending' once a run is finished but usage hasn't been
    reported, '—' while still running with no reading yet."""
    acus = run.get("acus")
    if run.get("finished_at") and not acus:
        return "pending"
    return "—" if acus is None else f"{acus:.1f}"


def session_minutes(run):
    """Minutes between started_at and finished_at, or None for in-flight."""
    start, end = parse_ts(run.get("started_at")), parse_ts(run.get("finished_at"))
    if not start or not end:
        return None
    return (end - start).total_seconds() / 60


def integer_axis(max_value, max_ticks=6, grid=False):
    """Count axis with whole-number ticks. Vega auto-ticks can land on
    fractions (e.g. 0.5) that format="d" rounds into duplicate labels, and
    tickMinStep doesn't reliably prevent that, so pin the values."""
    top = max(1, math.ceil(max_value))
    step = max(1, math.ceil(top / max_ticks))
    return alt.Axis(
        format="d", values=list(range(0, top + step, step)), grid=grid
    )


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
        elif label == "devin-approved":
            base = "Removed approval label"
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
            "approval needed": "Posted approval-needed comment",
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

    if name == "approval_applied":
        try:
            info = json.loads(detail or "{}")
        except ValueError:
            info = {}
        text = f"Approved by {info.get('approver') or 'unknown'}"
        if info.get("decision"):
            text = f"{text} — {info['decision']}"
        return text, False

    if name == "acus_updated":
        return f"ACU usage updated: {detail or '?'}", False

    if name in ("session_terminated", "session_ended"):
        return "Devin session ended", False

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
            ("Sessions", funnel.get("sessions", 0)),
            ("PRs", funnel.get("prs", 0)),
            ("Merged (PRs)", funnel.get("merged", 0)),
            ("Rejected (PRs)", funnel.get("rejected", 0)),
        ]
    # Older API shape: derive from the runs list.
    return [
        ("Sessions", sum(1 for r in runs if r.get("session_id"))),
        ("PRs", sum(1 for r in runs if r.get("pr_url"))),
        ("Merged (PRs)", sum(1 for r in runs if r.get("state") == "merged")),
        ("Rejected (PRs)", sum(1 for r in runs if r.get("state") == "rejected")),
    ]


def is_finished(run):
    return bool(run.get("finished_at")) or run.get("state") in TERMINAL_STATES


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

# Feather "help-circle" glyph, used for every help affordance. The box is
# sized to the SMALL text so the icon sits centered with its label.
HELP_ICON = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="12" cy="12" r="10"/>'
    '<path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>'
    '<line x1="12" y1="17" x2="12.01" y2="17"/></svg>'
)


def section_title(text):
    """LARGE heading, used for every section and chart title."""
    st.markdown(
        f'<div class="sec-title">{html.escape(text)}</div>',
        unsafe_allow_html=True,
    )


def group_label(text):
    """SMALL weight-600 label above a KPI group (Status, Cost, ...)."""
    st.markdown(
        f'<div class="kpi-group">{html.escape(text)}</div>',
        unsafe_allow_html=True,
    )


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
        f'<span class="kpi-help" data-tip="{tip}">{HELP_ICON}</span></div>'
        f'<div class="kpi-value">{html.escape(value)}</div>'
        "</div>"
    )


def render_kpis(metrics, runs):
    """One always-visible row of grouped KPI tiles."""
    counts = by_state_counts(metrics, runs)
    # key= lands as a .st-key-kpi_grid class; the CSS block wraps this row
    # cleanly (4-across -> 2x2 -> stacked) as the viewport narrows.
    with st.container(key="kpi_grid"):
        status_col, eff_col, thr_col, cost_col = st.columns([4, 4, 3, 4])

        with status_col:
            group_label("Status")
            st.markdown(
                _kpi_tile(
                    "Active",
                    f"{counts['running']} running · {counts['queued']} queued",
                    "Runs with a live Devin session, plus labeled issues "
                    "waiting for a free session slot "
                    "(MAX_CONCURRENT_SESSIONS).",
                ),
                unsafe_allow_html=True,
            )

        with eff_col:
            group_label("Effectiveness")
            res_col, merge_col, human_col = st.columns(3)
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
            human_col.markdown(
                _kpi_tile(
                    "Human interventions",
                    fmt_pct(metrics.get("human_intervention_rate")),
                    "Share of finished runs that ended waiting on a human "
                    "decision or with a rejected PR. Formula: (needs human "
                    "+ rejected runs) ÷ finished runs.",
                ),
                unsafe_allow_html=True,
            )

        with thr_col:
            group_label("Throughput")
            st.markdown(
                _kpi_tile(
                    "Median time to PR",
                    fmt_minutes(metrics.get("median_label_to_pr_minutes")),
                    "Median minutes from an issue being labeled to its PR "
                    "opening. Formula: median(PR opened time − labeled "
                    "time).",
                ),
                unsafe_allow_html=True,
            )

        with cost_col:
            group_label("Cost")
            total_col, per_fix_col = st.columns(2)
            total_col.markdown(
                _kpi_tile(
                    "Session minutes",
                    fmt_minutes(metrics.get("total_session_minutes")),
                    f"Total Devin session time across finished runs. {ACU_HELP}",
                ),
                unsafe_allow_html=True,
            )
            per_fix_col.markdown(
                _kpi_tile(
                    "Minutes per merged fix",
                    fmt_minutes(metrics.get("session_minutes_per_merged_fix")),
                    "Total session minutes ÷ merged fixes. Includes time "
                    f"spent on runs that did not merge. {ACU_HELP}",
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
                axis=integer_axis(df["Count"].max()),
            ),
            y=alt.Y(
                "Stage:N",
                sort=alt.EncodingSortField(field="order", order="ascending"),
                title=None,
            ),
            tooltip=["Stage", "Count"],
        )
        .properties(height=CHART_HEIGHT)
    )
    st.altair_chart(chart, width="stretch")


def render_awaiting_decision(issues):
    waiting = [i for i in issues if i.get("latest_state") == "needs_human"]
    if not waiting:
        st.caption("No issues are waiting on a decision.")
        return
    # Longest-waiting first: sort by when each run finalized, oldest up top.
    waiting.sort(key=lambda i: i["runs"][-1].get("finished_at") or "")
    now = datetime.now(DISPLAY_TZ).isoformat()
    for issue in waiting:
        run = issue["runs"][-1]
        report = parse_output(run) or {}
        rule = (
            report.get("blockers")
            or run.get("error")
            or "No blocker detail recorded."
        )
        url = issue.get("issue_url")
        ref = f"[{issue_ref(run)}]({url})" if url else issue_ref(run)
        waited = fmt_delta(run.get("finished_at"), now)
        st.markdown(f"- {ref} — {rule} · waiting {waited}")


def render_runs_table(issues):
    if not issues:
        st.caption("No runs yet — waiting for issues labeled `devin-remediate`.")
        return

    # Attention order: in-flight work first, then blockers, then finished
    # runs, each group most recent first. Rows are issues; each row shows
    # the issue's latest run.
    state_rank = {
        "running": 0,
        "queued": 1,
        "needs_human": 2,
        "pr_open": 3,
        "failed": 4,
        "rejected": 5,
        "merged": 6,
    }

    def sort_key(issue):
        run = issue["runs"][-1]
        ts = (
            parse_ts(run.get("finished_at"))
            or parse_ts(run.get("started_at"))
            or parse_ts(run.get("created_at"))
            or datetime.min.replace(tzinfo=timezone.utc)
        )
        return (state_rank.get(run.get("state"), 9), -ts.timestamp())

    ordered = sorted(issues, key=sort_key)
    now = datetime.now(DISPLAY_TZ).isoformat()
    df = pd.DataFrame(
        [
            {
                # Full title in the cell; the grid ellipsizes overflow, so
                # truncating here too would lose the tail twice. Hovering a
                # truncated cell shows the whole title in the grid tooltip.
                "Issue": issue_ref(latest, limit=None),
                "Status": state_label(latest.get("state")),
                "Approved": "Yes" if latest.get("approved") else "",
                "Date": fmt_date(latest.get("created_at")),
                "Attempts": issue["attempts"],
                # For unfinished runs this column shows elapsed time instead.
                "Time": fmt_delta(
                    latest.get("created_at"), latest.get("finished_at") or now
                ),
                "ACUs": acu_display(latest),
                "Session": latest.get("session_url"),
                "PR": latest.get("pr_url"),
                "Details": "View",
            }
            for issue in ordered
            for latest in [issue["runs"][-1]]
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
            "Issue": st.column_config.TextColumn(width=TABLE_COL_WIDTHS[0]),
            "Status": st.column_config.TextColumn(width=TABLE_COL_WIDTHS[1]),
            "Approved": st.column_config.TextColumn(width=TABLE_COL_WIDTHS[2]),
            "Date": st.column_config.TextColumn(width=TABLE_COL_WIDTHS[3]),
            "Attempts": st.column_config.NumberColumn(
                width=TABLE_COL_WIDTHS[4]
            ),
            "Time": st.column_config.TextColumn(width=TABLE_COL_WIDTHS[5]),
            "ACUs": st.column_config.TextColumn(width=TABLE_COL_WIDTHS[6]),
            "Session": st.column_config.LinkColumn(
                "Session", display_text="Open", width=TABLE_COL_WIDTHS[7]
            ),
            "PR": st.column_config.LinkColumn(
                "PR", display_text="Open", width=TABLE_COL_WIDTHS[8]
            ),
            "Details": st.column_config.ButtonColumn(
                "Details", key="run_detail_click", width=TABLE_COL_WIDTHS[9]
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
        issue_detail_dialog(ordered[click["row"]])
        return

    # Row selection also opens the dialog, but only when the selection
    # changes; a persisted selection would otherwise reopen it every tick.
    selected = event.selection.rows
    if selected == st.session_state.get("_selected_row"):
        return
    st.session_state["_selected_row"] = selected
    if selected:
        issue_detail_dialog(ordered[selected[0]])


def approval_detail(event):
    """Parse the JSON detail of an approval_applied event."""
    try:
        return json.loads(event.get("detail") or "{}")
    except ValueError:
        return {}


def run_reason(run):
    """Why this attempt ended as it did: rejection comment, escalation
    rule, or error, whichever applies."""
    report = parse_output(run) or {}
    return (
        run.get("rejection_reason")
        or report.get("blockers")
        or run.get("error")
        or "—"
    )


@st.dialog("Issue details", width="large")
def issue_detail_dialog(issue):
    title = issue.get("title") or issue["runs"][-1].get("title") or ""
    st.markdown(f"##### #{issue['issue_number']} {title}")
    if issue.get("issue_url"):
        st.markdown(f"[Issue]({issue['issue_url']})")

    for i, run in enumerate(issue["runs"], 1):
        section_title(f"Attempt {i} · {state_label(run.get('state'))}")
        st.caption(
            f"Outcome: {outcome_label(run.get('outcome'))} · "
            f"Started: {fmt_ts(run.get('started_at'))} · "
            f"Finished: {fmt_ts(run.get('finished_at'))} · "
            f"ACUs: {acu_display(run)} · "
            f"Tests: {tests_text(run)}"
        )
        links = []
        if run.get("session_url"):
            links.append(f"[Devin session]({run['session_url']})")
        if run.get("pr_url"):
            links.append(f"[Pull request]({run['pr_url']})")
        if links:
            st.markdown(" · ".join(links))
        st.markdown(f"**Reason:** {run_reason(run)}")
        if run.get("error"):
            st.error(f"Error: {run['error']}")
        for e in run.get("events", []):
            if e.get("event") != "approval_applied":
                continue
            info = approval_detail(e)
            st.markdown(
                f"**Approved by {info.get('approver') or 'unknown'}** "
                f"({fmt_ts(e.get('timestamp'))}): "
                f"{info.get('decision') or '—'}"
            )
        with st.expander("Report & events"):
            report = parse_output(run)
            if report is None:
                st.info(
                    "Devin has not posted a structured report for this run yet."
                )
            else:
                st.markdown(f"**Summary:** {report.get('summary') or '—'}")
                st.markdown(
                    f"**Root cause:** {report.get('root_cause') or '—'}"
                )
                files = report.get("files_changed") or []
                if files:
                    st.markdown("**Files changed:**")
                    for path in files:
                        st.markdown(f"- `{path}`")
                else:
                    st.markdown("**Files changed:** —")
                st.markdown(
                    f"**Tests:** {report.get('tests_passed') or 0}"
                    f"/{report.get('tests_run') or 0} passed"
                )
                st.markdown(
                    f"**Risk notes:** {report.get('risk_notes') or '—'}"
                )
                st.markdown(f"**Blockers:** {report.get('blockers') or '—'}")
            events = run.get("events", [])
            if not events:
                st.caption("No events recorded for this run.")
            else:
                for e in events:
                    text, is_error = friendly_event(e)
                    marker = "⚠️ " if is_error else ""
                    st.markdown(
                        f"- {fmt_ts(e.get('timestamp'))} — {marker}{text}"
                    )


def render_overview(metrics, runs, issues):
    # Grid cells are canvas, not DOM, so the browser can't tooltip
    # truncated titles. Pointer position maps to (column, row) via the
    # grid's real scroll offsets; the full title comes from the grid's
    # own accessibility mirror cell. Guarded to bind once.
    st.html(
        f"""<script>
(() => {{
  if (window.__issueTipBound) return;
  window.__issueTipBound = true;
  const LEFT = 36, RIGHT = {36 + TABLE_COL_WIDTHS[0]};
  const HEADER = 36, ROW = {TABLE_ROW_HEIGHT};
  const tip = document.createElement('div');
  tip.className = 'gdg-tip';
  tip.style.display = 'none';
  document.body.appendChild(tip);
  const hide = () => {{ tip.style.display = 'none'; }};
  document.addEventListener('mousemove', (e) => {{
    const t = e.target instanceof Element ? e.target : null;
    const df = t && t.closest('[data-testid="stDataFrame"]');
    const scroller = df && df.querySelector('.dvn-scroller');
    if (!scroller) {{ hide(); return; }}
    const r = scroller.getBoundingClientRect();
    const x = e.clientX - r.left + scroller.scrollLeft;
    const y = e.clientY - r.top + scroller.scrollTop - HEADER;
    if (x < LEFT || x >= RIGHT || y < 0) {{ hide(); return; }}
    const row = Math.floor(y / ROW);
    const cell = df.querySelector('[id="glide-cell-1-' + row + '"]');
    const text = cell && cell.textContent ? cell.textContent.trim() : '';
    if (!text) {{ hide(); return; }}
    tip.textContent = text;
    tip.style.display = 'block';
    const left = Math.min(
      e.clientX + 12, window.innerWidth - tip.offsetWidth - 8
    );
    tip.style.left = left + 'px';
    tip.style.top = (e.clientY + 14) + 'px';
  }}, true);
}})();
</script>""",
        unsafe_allow_javascript=True,
    )
    # Below ~1240px the columns stack (CSS media query on the keyed
    # container) so the runs table keeps every column on screen.
    with st.container(key="overview_split"):
        table_col, side_col = st.columns([7, 4])
        with table_col:
            render_runs_table(issues)
        with side_col:
            section_title("Pipeline")
            render_funnel(metrics, runs)
            section_title("Awaiting decision")
            render_awaiting_decision(issues)


def render_trends(metrics, runs):
    # Keyed container so the two charts stack via CSS instead of squeezing
    # or overflowing at narrow widths.
    with st.container(key="trends_split"):
        left, right = st.columns(2)

    with left:
        section_title("Completed over time")
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
                        axis=integer_axis(
                            counts["Completed"].max(), grid=True
                        ),
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
                .properties(height=CHART_HEIGHT)
            )
            st.altair_chart(chart, width="stretch")

    with right:
        section_title("Outcome breakdown")
        finished = [r for r in runs if is_finished(r)]
        if not finished:
            st.caption("No finished runs yet.")
        else:
            outcome_counts = {}
            outcome_flagged = {}
            for r in finished:
                # Terminal PR states render as themselves: merged means the
                # fix was applied, and rejected runs keep outcome="fixed" in
                # the DB. Other finished runs fall back to their reported
                # outcome, or to state when they failed before producing a
                # report.
                raw = (
                    r.get("state")
                    if r.get("state") in ("merged", "rejected")
                    else (r.get("outcome") or r.get("state"))
                )
                label = outcome_label(raw)
                outcome_counts[label] = outcome_counts.get(label, 0) + 1
                outcome_flagged[label] = raw not in ("fixed", "merged")
            df = pd.DataFrame(
                [
                    {
                        "Outcome": k,
                        "Runs": v,
                        "Flagged": outcome_flagged[k],
                    }
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
                        axis=integer_axis(df["Runs"].max()),
                    ),
                    y=alt.Y("Outcome:N", sort="-x", title=None),
                    # Non-fix outcomes are the flagged/secondary set and
                    # use the muted color.
                    color=alt.Color(
                        "Flagged:N",
                        scale=alt.Scale(
                            domain=[False, True], range=[ACCENT, MUTED]
                        ),
                        legend=None,
                    ),
                    tooltip=["Outcome", "Runs"],
                )
                .properties(height=CHART_HEIGHT)
            )
            st.altair_chart(chart, width="stretch")


def render_cost(metrics, runs):
    section_title("Session time per run")
    # Devin doesn't always meter per-session ACUs, so wall-clock session
    # time is the cost proxy; reported ACUs ride along as a secondary field.
    timed = [r for r in runs if session_minutes(r) is not None]
    if not timed:
        st.caption("No finished runs yet.")
        return
    acu_cap = metrics.get("max_acu_per_session")
    at_cap = [
        bool(r.get("finished_at"))
        and acu_cap is not None
        and (r.get("acus") or 0) >= acu_cap
        for r in timed
    ]
    df = pd.DataFrame(
        {
            "Run": [issue_ref(r) for r in timed],
            # Untruncated name for the hover tooltip; the y-axis label uses
            # the truncated Run value.
            "Full": [issue_ref(r, limit=None) for r in timed],
            "Minutes": [session_minutes(r) for r in timed],
            "ACUs": [acu_display(r) for r in timed],
            "At cap": at_cap,
        }
    )
    # Horizontal bars: run names read left-to-right on the y axis, so they
    # stay legible instead of truncating like rotated x-axis labels. Vega-Lite
    # axis labels can't carry tooltips; hovering a bar shows the full name.
    # sort="-x" puts the longest sessions at the top.
    chart = (
        alt.Chart(df)
        .mark_bar()
        .encode(
            x=alt.X(
                "Minutes:Q",
                title="Session minutes",
                axis=alt.Axis(format=".0f", tickCount=10),
            ),
            y=alt.Y(
                "Run:N",
                sort="-x",
                title=None,
                axis=alt.Axis(labelLimit=340),
            ),
            # Sessions that hit the per-session ACU cap use the muted color.
            color=alt.Color(
                "At cap:N",
                scale=alt.Scale(
                    domain=[False, True], range=[ACCENT, MUTED]
                ),
                legend=None,
            ),
            tooltip=[
                alt.Tooltip("Full:N", title="Run"),
                alt.Tooltip("Minutes:Q", format=".1f"),
                "ACUs",
            ],
        )
        # One row per run needs per-row spacing or labels collide;
        # CHART_HEIGHT is the floor so it matches the other charts when
        # few runs exist.
        .properties(height=max(CHART_HEIGHT, 26 * len(timed)))
    )
    st.altair_chart(chart, width="stretch")

    reported = sum(1 for r in timed if (r.get("acus") or 0) > 0)
    note = (
        f"ACUs reported for {reported}/{len(timed)} finished runs; "
        "unreported usage shows 'pending'."
    )
    if any(at_cap):
        note += " Muted bars hit the per-session ACU cap."
    st.caption(note)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Backlog Automation Statistics", layout="wide"
)
# Type system + component styles. Font sizes come from --fs-s/--fs-l, which
# mirror FS_SMALL/FS_LARGE above; config.toml's baseFontSize sizes the
# canvas dataframe. The catch-all pins every element to SMALL (including
# portal-rendered dialog/tooltip content and Vega text) so only the two
# declared sizes ever compute.
st.markdown(
    """<style>
:root{--fs-s:13px;--fs-l:20px;}
.block-container{padding-top:1.75rem;padding-bottom:1rem;}

/* toolbarMode=viewer drops Deploy/dev options; this removes the leftover
   hamburger menu too. */
[data-testid="stMainMenu"]{display:none!important;}

/* SMALL is the default for every element on the page. */
body,body *,body *::before,body *::after{font-size:var(--fs-s)!important;}

/* LARGE: headings (page/section/dialog titles) and KPI values. */
body h1,body h2,body h3,body h4,body h5,body h6,
.sec-title,.kpi-value{font-size:var(--fs-l)!important;font-weight:600!important;}
/* Heading text often sits in a nested <p>/<strong>; let it inherit the
   heading size instead of the SMALL catch-all. */
body h1 *,body h2 *,body h3 *,body h4 *,body h5 *,body h6 *,
.sec-title *,.kpi-value *{font-size:inherit!important;font-weight:inherit;}

/* One font family everywhere, including inline code. */
code{font-family:inherit!important;}

.sec-title{line-height:1.3;margin:16px 0 6px;}

/* KPI grid: 4-across -> 2x2 -> stacked as the viewport narrows. Group
   labels carry their own top spacing so they never collide with values
   from a wrapped row above. */
.kpi-group{font-weight:600;margin-top:16px;}

/* Columns wrap when they can't fit, instead of enforcing their default
   min-width and pushing the whole page wider than the viewport. The
   flex-basis (not a media query) decides when wrapping happens. */
.st-key-kpi_grid [data-testid="stHorizontalBlock"],
.st-key-overview_split [data-testid="stHorizontalBlock"],
.st-key-trends_split [data-testid="stHorizontalBlock"]{flex-wrap:wrap;}
.st-key-kpi_grid [data-testid="stHorizontalBlock"]>[data-testid="stColumn"]{flex:1 1 230px!important;min-width:230px!important;}
/* Two-tile inner groups wrap rather than squeeze. */
.st-key-kpi_grid [data-testid="stColumn"] [data-testid="stHorizontalBlock"]>[data-testid="stColumn"]{flex:1 1 110px!important;min-width:110px!important;}
/* Overview: table keeps ~60% while it fits, sidebar wraps below it. */
.st-key-overview_split [data-testid="stHorizontalBlock"]>[data-testid="stColumn"]:first-child{flex:7 1 560px!important;min-width:520px!important;}
.st-key-overview_split [data-testid="stHorizontalBlock"]>[data-testid="stColumn"]:last-child{flex:4 1 300px!important;min-width:260px!important;}
/* Trends: the two charts side by side, stacked when space runs out. */
.st-key-trends_split [data-testid="stHorizontalBlock"]>[data-testid="stColumn"]{flex:1 1 420px!important;min-width:300px!important;}

.kpi-label{color:var(--text-color);color:color-mix(in srgb,var(--text-color) 60%,transparent);display:flex;align-items:center;gap:6px;}
.kpi-help{position:relative;display:inline-flex;align-items:center;justify-content:center;width:14px;height:14px;color:var(--text-color);color:color-mix(in srgb,var(--text-color) 55%,transparent);cursor:help;}
.kpi-help svg{display:block;width:14px;height:14px;}
.kpi-help::after{content:attr(data-tip);position:absolute;top:calc(100% + 7px);left:50%;transform:translateX(-50%);width:max-content;max-width:15rem;white-space:normal;text-align:left;background:#31333F;color:#fff;font-weight:400;line-height:1.45;padding:7px 9px;border-radius:6px;box-shadow:0 4px 12px rgba(0,0,0,.18);opacity:0;visibility:hidden;transition:opacity .12s;pointer-events:none;z-index:100;}
.kpi-help::before{content:"";position:absolute;top:calc(100% + 3px);left:50%;transform:translateX(-50%);border:4px solid transparent;border-bottom-color:#31333F;opacity:0;visibility:hidden;transition:opacity .12s;pointer-events:none;z-index:100;}
.kpi-help:hover::after,.kpi-help:hover::before{opacity:1;visibility:visible;}
.kpi-value{color:var(--text-color);line-height:1.4;}

/* Full-title tooltip for truncated Issue cells (see render_overview). */
.gdg-tip{position:fixed;z-index:1000;background:#31333F;color:#fff;padding:7px 9px;border-radius:6px;box-shadow:0 4px 12px rgba(0,0,0,.25);line-height:1.45;max-width:22rem;pointer-events:none;}
</style>""",
    unsafe_allow_html=True,
)
st.title("Backlog Automation Statistics")


@st.fragment(run_every=REFRESH_SECONDS)
def render():
    try:
        health = api_get("/healthz")
        metrics = api_get("/api/v1/metrics")
        runs = api_get("/api/v1/runs").get("runs", [])
        issues = api_get("/api/v1/issues").get("issues", [])
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
            render_overview(metrics, runs, issues)
    if tab_trends.open:
        with tab_trends:
            render_trends(metrics, runs)
    if tab_cost.open:
        with tab_cost:
            render_cost(metrics, runs)


render()
