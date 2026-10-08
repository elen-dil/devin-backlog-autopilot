"""GitHub API client plus a no-op stand-in for SIMULATE mode."""

import httpx

API_BASE = "https://api.github.com"


class GitHubClient:
    def __init__(self, repo: str, token: str, http: httpx.AsyncClient | None = None):
        self._repo = repo
        self._http = http or httpx.AsyncClient(
            base_url=API_BASE,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )

    async def list_labeled_issues(self, label: str):
        resp = await self._http.get(
            f"/repos/{self._repo}/issues",
            params={"state": "open", "labels": label, "per_page": 100},
        )
        resp.raise_for_status()
        # The issues endpoint also returns PRs; skip them.
        return [i for i in resp.json() if "pull_request" not in i]

    async def add_label(self, issue_number: int, label: str) -> None:
        resp = await self._http.post(
            f"/repos/{self._repo}/issues/{issue_number}/labels",
            json={"labels": [label]},
        )
        resp.raise_for_status()

    async def remove_label(self, issue_number: int, label: str) -> None:
        resp = await self._http.delete(
            f"/repos/{self._repo}/issues/{issue_number}/labels/{label}"
        )
        if resp.status_code not in (200, 404):
            resp.raise_for_status()

    async def comment(self, issue_number: int, body: str) -> None:
        resp = await self._http.post(
            f"/repos/{self._repo}/issues/{issue_number}/comments",
            json={"body": body},
        )
        resp.raise_for_status()

    async def pr_is_merged(self, pr_url: str) -> bool:
        # pr_url looks like https://github.com/{owner}/{repo}/pull/{n}
        parts = pr_url.rstrip("/").split("/")
        owner, repo, number = parts[-4], parts[-3], parts[-1]
        resp = await self._http.get(f"/repos/{owner}/{repo}/pulls/{number}")
        if resp.status_code != 200:
            return False
        return bool(resp.json().get("merged_at"))


class NullGitHubClient:
    """SIMULATE mode: performs no GitHub traffic, records calls for tests."""

    def __init__(self):
        self.events = []

    async def list_labeled_issues(self, label: str):
        return []

    async def add_label(self, issue_number: int, label: str) -> None:
        self.events.append(("add_label", issue_number, label))

    async def remove_label(self, issue_number: int, label: str) -> None:
        self.events.append(("remove_label", issue_number, label))

    async def comment(self, issue_number: int, body: str) -> None:
        self.events.append(("comment", issue_number, body))

    async def pr_is_merged(self, pr_url: str) -> bool:
        self.events.append(("pr_check", pr_url))
        return False
