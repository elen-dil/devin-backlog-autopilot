"""Autopilot dashboard. Reads only from the FastAPI endpoints, never the DB."""

import os

import httpx
import pandas as pd
import streamlit as st

API = os.environ.get("FASTAPI_BASE_URL", "http://localhost:8000")

st.set_page_config(page_title="Devin Backlog Autopilot", layout="wide")
st.title("Devin Backlog Autopilot")


@st.fragment(run_every=10)
def render():
    try:
        metrics = httpx.get(f"{API}/api/v1/metrics", timeout=10).json()
        runs = httpx.get(f"{API}/api/v1/runs", timeout=10).json()["runs"]
    except Exception as exc:
        st.error(f"API unreachable at {API}: {exc}")
        return

    cols = st.columns(6)
    cols[0].metric("Merge rate", f"{metrics['merge_rate']:.0%}")
    cols[1].metric("Resolution rate", f"{metrics['resolution_rate']:.0%}")
    cols[2].metric("PR rate", f"{metrics['pr_rate']:.0%}")
    cols[3].metric(
        "Median latency",
        f"{metrics['median_latency_minutes']:.1f} min"
        if metrics["median_latency_minutes"] is not None
        else "n/a",
    )
    cols[4].metric("Total ACUs", f"{metrics['total_acus']:.1f}")
    cols[5].metric(
        "ACUs per merged fix",
        f"{metrics['acus_per_merged_fix']:.1f}"
        if metrics["acus_per_merged_fix"] is not None
        else "n/a",
    )

    st.subheader("Runs by state")
    if metrics["by_state"]:
        st.bar_chart(pd.Series(metrics["by_state"]))
    else:
        st.caption("No runs yet.")

    st.subheader("Runs")
    if runs:
        df = pd.DataFrame(runs)
        show = [
            c
            for c in (
                "id",
                "issue_number",
                "title",
                "state",
                "outcome",
                "session_url",
                "pr_url",
                "acus",
                "created_at",
            )
            if c in df.columns
        ]
        st.dataframe(
            df[show],
            column_config={
                "session_url": st.column_config.LinkColumn("Session"),
                "pr_url": st.column_config.LinkColumn("PR"),
                "issue_url": st.column_config.LinkColumn("Issue"),
            },
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("Waiting for issues labeled `devin-remediate`.")


render()
