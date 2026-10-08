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

## API

- `POST /webhooks/github` - GitHub `issues` events (`labeled` -> enqueue), `ping`, HMAC-SHA256 verified
- `GET /api/v1/runs` - all runs with session/PR links, outcomes, ACUs
- `GET /api/v1/metrics` - counts by state, PR rate, merge rate (merged / PRs opened), resolution rate (merged / finished runs), median start-to-finish latency, total ACUs, ACUs per merged fix
- `POST /simulate/issue` - inject an issue (SIMULATE only)
- `GET /healthz`

## Tests

```bash
pytest tests/
```
