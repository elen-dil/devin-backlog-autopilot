# devin-backlog-autopilot

Backlog issues (dependency CVEs, scanner findings, small bugs) are real work that
almost never gets prioritized. This service closes that gap: label a GitHub issue
`devin-remediate` and it spins up a cloud Devin session that fixes the issue and
opens a PR, while a small dashboard tracks throughput, success rates, and cost.

## Architecture

```
                 label: devin-remediate
                          │
        poll every 60s    ▼
        open issues with  enqueue (idempotent:
        trigger label     one active run per issue)
                          │
                          ▼
                    SQLite `runs`  (the queue;
                    queued | running | pr_open |
                    merged | needs_human | failed)
                                              │
                        dispatcher (MAX_CONCURRENT_SESSIONS=3)
                                              │
                                              ▼
                                    Devin session (1 per issue,
                                    max_acu_limit=10, structured
                                    output required)
                                              │
                    poll every 30s ◀──────────┘
                    (terminal: exit/error/suspended,
                     or running + finished/waiting_for_user)
                                              │
              ┌───────────────┬───────────────┴──────────────┐
              ▼               ▼                              ▼
          fixed + PR      needs_human /               failed to start
              │           not_reproducible /           or errored
              │           ACU cap hit                       │
              ▼               ▼                              ▼
        label:           label:                        state: failed,
        devin-pr-open    devin-needs-human             trigger label removed
              │
              ▼
     merge check every 5 min ──▶ state: merged

Dashboard (Streamlit :8501) ──reads──▶ GET /api/v1/runs, GET /api/v1/metrics
```

## Design decisions

- **One session per issue.** No central orchestrator decides what Devin does;
  each run is a self-contained session with a standardized prompt and a
  required structured-output report.
- **The database is the queue.** Issue title/body/url are persisted at enqueue
  time, so a restart picks up queued, running, and pr_open runs with no
  recovery path.
- **Idempotency at the DB level.** A partial unique index
  (`issue_number WHERE state IN ('queued','running')`) makes every enqueue
  path (poller, simulate endpoint) race-safe. Re-labeling after a run closes
  starts a fresh run.
- **Session creation dedups on tags.** The sessions API has no idempotency
  keys, so when `POST /sessions` fails ambiguously (5xx or transport error)
  the client first looks for a session carrying the run's `issue-<n>` tag and
  adopts it, instead of risking a duplicate session.
- **Structured output, not log scraping.** Sessions must report
  `outcome`/`summary`/`tests_passed` via Devin's `structured_output_schema`
  before finishing, so state transitions are driven by data, not parsing.
- **Humans merge, always.** Devin opens PRs but never merges. `needs_human`
  (including ACU-cap suspensions and not-reproducible findings) is a first-class
  outcome that leaves a comment explaining why.
- **Guardrails.** `MAX_ACU_PER_SESSION` caps spend per issue,
  `MAX_CONCURRENT_SESSIONS` caps parallelism, and a session that fails to start
  gets its trigger label removed so the poller can't loop on it.
- **Polling, not webhooks.** A 60s poll of open issues carrying the trigger
  label is the only ingress: it works with no public endpoint, no tunnel, and
  no inbound attack surface — the right trade for a locally-hosted service.
  A webhook fast path was removed as unused infrastructure (see git history).

## Setup

```bash
cp .env.example .env        # fill in keys (or keep SIMULATE=true)
docker compose up --build   # api on :8000, dashboard on :8501
```

Or locally:

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000            # API
FASTAPI_BASE_URL=http://localhost:8000 \
  streamlit run dashboard/app.py            # dashboard
```

## Simulate mode

`SIMULATE=true` swaps in a fake Devin client (sessions finish after a few
polls, results are prefixed `[SIMULATED]`) and disables all GitHub traffic, so
the repo can be exercised with no credentials:

```bash
curl -X POST localhost:8000/simulate/issue \
  -H 'content-type: application/json' \
  -d '{"number": 1, "title": "demo issue", "body": "fix me"}'

# force a needs_human outcome:
curl -X POST localhost:8000/simulate/issue \
  -H 'content-type: application/json' \
  -d '{"number": 2, "title": "ambiguous", "outcome": "needs_human"}'
```

## Dashboard

Streamlit app on :8501, reads only from the FastAPI API (never SQLite directly),
auto-refreshes every `DASHBOARD_REFRESH_SECONDS` (default 10s). A compact banner shows LIVE vs SIMULATE MODE plus the
repo and last-refresh time. Everything is designed to fit one laptop screen.

An always-visible KPI row groups the headline numbers:

- **Status** — running and queued counts in one tile.
- **Effectiveness** — *resolution rate* (merged / finished runs) and *merge
  rate* (merged / PRs opened).
- **Throughput** — *median time to PR* (labeled to PR opened).
- **Cost** — total ACUs and *ACUs per merged fix* (total ACUs / merged fixes,
  including compute spent on runs that did not merge). All spend is tracked in
  ACUs; there is no dollar pricing.

Below the KPIs, three tabs:

- **Overview** — a compact runs table (issue, status, time to result, ACUs,
  session/PR links) plus a pipeline funnel (labeled -> sessions started -> PRs
  opened -> merged) and the list of blockers from needs_human runs.
- **Trends** — cumulative completed-over-time chart and outcome breakdown.
- **Cost** — a per-run ACU bar chart with the `MAX_ACU_PER_SESSION` cap as a
  reference line and at-cap runs highlighted.

Selecting a row in the runs table opens a detail dialog: issue/session/PR
links, the session's full structured report, and an event timeline
(`run_events`) covering queue source, session creation, status changes, every
GitHub write-back, and finalization.

In non-SIMULATE mode, simulated runs are excluded from metrics and tables
(`is_simulated` flag, set at enqueue time).

## API

- `GET /api/v1/runs` - all runs with session/PR links, outcomes, ACUs
- `GET /api/v1/runs/{id}` - one run; `GET /api/v1/runs/{id}/events` - its event timeline
- `GET /api/v1/metrics` - counts by state, funnel, PR/merge/resolution rates, latency medians, ACU totals and per-unit metrics, runs at cap
- `POST /simulate/issue` - inject an issue (SIMULATE only)
- `GET /healthz`

## Tests

```bash
pytest tests/
```
