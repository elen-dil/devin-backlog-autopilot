# devin-backlog-autopilot

Backlog issues (dependency CVEs, scanner findings, small bugs) are real work that
almost never gets prioritized. This service closes that gap: label a GitHub issue
`devin-remediate` and it spins up a cloud Devin session that fixes the issue and
opens a PR, while a small dashboard tracks throughput, success rates, and cost.

## Architecture

```
                 label: devin-remediate
                          │
GitHub ──webhook──▶ POST /webhooks/github ──┐
 (issues.labeled,        (HMAC-SHA256       │   enqueue (idempotent:
  signature verified)     + ping)           │   one active run per issue)
                          │               ▼
        poll every 60s ─────────────────▶ SQLite `runs`  (the queue;
        open issues with                  queued | running | pr_open |
        trigger label                     merged | needs_human | failed)
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
  (`issue_number WHERE state IN ('queued','running')`) makes the webhook and
  the fallback poller race-safe. Re-labeling after a run closes starts a fresh
  run.
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
- **Webhook + poller.** The webhook is the fast path; a 60s poll of labeled
  open issues catches anything the webhook missed (or a webhook that was never
  configured).

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

## GitHub webhook

Create a webhook on the target repo:

- Payload URL: `https://<your-host>/webhooks/github` (locally, forward with
  [smee.io](https://smee.io): `smee -u https://smee.io/<channel> -t http://localhost:8000/webhooks/github`)
- Secret: value of `GITHUB_WEBHOOK_SECRET`
- Events: **Issues**

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
auto-refreshes every 10s. A banner shows LIVE vs SIMULATE MODE plus the repo and
last-refresh time. Four sections:

- **STATUS** — what is happening now. Count of runs per state, and an "Active
  now" table of queued/running work with live session links.
- **EFFECTIVENESS** — is Devin's work good.
  - *Resolution rate* = merged / finished runs (pr_open + merged + needs_human + failed).
  - *Merge rate* = merged / PRs opened.
  - *PR rate* = PRs opened / finished runs.
  - Pipeline funnel (labeled -> sessions started -> PRs opened -> merged),
    outcome breakdown, and a list of blockers from needs_human runs.
- **THROUGHPUT** — how fast the backlog moves: issues completed, median time
  from label to result, median time from label to PR, and a cumulative
  completed-over-time chart.
- **COST** — total ACUs, estimated cost (`ACU_PRICE_USD` env; unset shows a
  reminder, no price is hardcoded), ACUs per run / per PR / per merged fix, and
  a per-run ACU bar chart with the `MAX_ACU_PER_SESSION` cap as a reference
  line and at-cap runs highlighted.

Selecting a row in the Runs table opens a detail panel: issue/session/PR links,
the session's full structured report, and an event timeline (`run_events`)
covering queue source, session creation, status changes, every GitHub
write-back, and finalization.

In non-SIMULATE mode, simulated runs are excluded from metrics and tables
(`is_simulated` flag, set at enqueue time).

## API

- `POST /webhooks/github` - GitHub `issues` events (`labeled` -> enqueue), `ping`, HMAC-SHA256 verified
- `GET /api/v1/runs` - all runs with session/PR links, outcomes, ACUs
- `GET /api/v1/runs/{id}` - one run; `GET /api/v1/runs/{id}/events` - its event timeline
- `GET /api/v1/metrics` - counts by state, funnel, PR/merge/resolution rates, latency medians, ACU totals and per-unit costs, runs at cap
- `POST /simulate/issue` - inject an issue (SIMULATE only)
- `GET /healthz`

## Tests

```bash
pytest tests/
```
