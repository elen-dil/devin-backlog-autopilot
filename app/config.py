"""Environment-driven configuration. See .env.example for documentation."""

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _bool(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


@dataclass
class Settings:
    simulate: bool
    devin_api_key: str
    devin_org_id: str
    github_token: str
    github_repo: str
    trigger_label: str
    running_label: str
    pr_open_label: str
    needs_human_label: str
    max_acu_per_session: int
    max_concurrent_sessions: int
    session_poll_seconds: int
    issue_poll_seconds: int
    merge_check_seconds: int
    dispatch_seconds: int
    database_path: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            simulate=_bool("SIMULATE"),
            devin_api_key=os.environ.get("DEVIN_API_KEY", ""),
            devin_org_id=os.environ.get("DEVIN_ORG_ID", ""),
            github_token=os.environ.get("GITHUB_TOKEN", ""),
            github_repo=os.environ.get("GITHUB_REPO", "elen-dil/superset-ali"),
            trigger_label=os.environ.get("TRIGGER_LABEL", "devin-remediate"),
            running_label=os.environ.get("RUNNING_LABEL", "devin-running"),
            pr_open_label=os.environ.get("PR_OPEN_LABEL", "devin-pr-open"),
            needs_human_label=os.environ.get("NEEDS_HUMAN_LABEL", "devin-needs-human"),
            max_acu_per_session=_int("MAX_ACU_PER_SESSION", 10),
            max_concurrent_sessions=_int("MAX_CONCURRENT_SESSIONS", 3),
            session_poll_seconds=_int("SESSION_POLL_SECONDS", 30),
            issue_poll_seconds=_int("ISSUE_POLL_SECONDS", 60),
            merge_check_seconds=_int("MERGE_CHECK_SECONDS", 300),
            dispatch_seconds=_int("DISPATCH_SECONDS", 5),
            database_path=os.environ.get("DATABASE_PATH", "./data/autopilot.db"),
        )

    def validate(self) -> None:
        """Fail fast when running for real without required credentials."""
        if self.simulate:
            return
        missing = [
            name
            for name, value in (
                ("DEVIN_API_KEY", self.devin_api_key),
                ("DEVIN_ORG_ID", self.devin_org_id),
                ("GITHUB_TOKEN", self.github_token),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"Missing required env vars: {', '.join(missing)} "
                "(or set SIMULATE=true to run without credentials)"
            )
