import os
import tempfile

# Must be set before app.main is imported (module-level app = create_app()).
os.environ.setdefault("SIMULATE", "true")
os.environ.setdefault(
    "DATABASE_PATH", os.path.join(tempfile.mkdtemp(), "autopilot.db")
)

import pytest

from app.config import Settings
from app.devin import SimulatedDevinClient
from app.github import NullGitHubClient
from app.store import RunStore


@pytest.fixture
def settings(tmp_path):
    return Settings(
        simulate=True,
        devin_api_key="",
        devin_org_id="",
        github_token="",
        github_repo="elen-dil/superset-ali",
        trigger_label="devin-remediate",
        running_label="devin-running",
        pr_open_label="devin-pr-open",
        needs_human_label="devin-needs-human",
        max_acu_per_session=10,
        max_concurrent_sessions=3,
        session_poll_seconds=30,
        issue_poll_seconds=60,
        merge_check_seconds=300,
        dispatch_seconds=5,
        database_path=str(tmp_path / "autopilot.db"),
        acu_backfill_seconds=60,
    )


@pytest.fixture
def components(settings):
    store = RunStore(settings.database_path)
    devin = SimulatedDevinClient()
    github = NullGitHubClient()
    return store, devin, github
