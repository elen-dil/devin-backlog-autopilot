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

    async def list_issue_comments(self, issue_number: int):
        resp = await self._http.get(
            f"/repos/{self._repo}/issues/{issue_number}/comments",
            params={"per_page": 100},
        )
        resp.raise_for_status()
        return resp.json()

    async def list_issue_events(self, issue_number: int):
        resp = await self._http.get(
            f"/repos/{self._repo}/issues/{issue_number}/events",
            params={"per_page": 100},
        )
        resp.raise_for_status()
        return resp.json()

    async def latest_pr_comment(self, pr_url: str):
        """Body of the most recent comment on a PR, or None. PRs share the
        issue comments endpoint; review comments on diffs are not fetched."""
        parts = pr_url.rstrip("/").split("/")
        owner, repo, number = parts[-4], parts[-3], parts[-1]
        resp = await self._http.get(
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            params={"per_page": 100},
        )
        if resp.status_code != 200:
            return None
        comments = resp.json()
        if not comments:
            return None
        return comments[-1].get("body")

    async def pr_status(self, pr_url: str) -> str:
        # pr_url looks like https://github.com/{owner}/{repo}/pull/{n}
        parts = pr_url.rstrip("/").split("/")
        owner, repo, number = parts[-4], parts[-3], parts[-1]
        resp = await self._http.get(f"/repos/{owner}/{repo}/pulls/{number}")
        if resp.status_code != 200:
            # A failed fetch means "keep waiting", same as the old False:
            # transient API errors retry on the next tick.
            return "open"
        pr = resp.json()
        if pr.get("merged_at"):
            return "merged"
        return "closed" if pr.get("state") == "closed" else "open"


class NullGitHubClient:
    """SIMULATE mode: performs no GitHub traffic, records calls for tests."""

    def __init__(self):
        self.events = []
        # pr_url -> "open" | "merged" | "closed"; tests set entries to
        # steer merge_check_tick.
        self.pr_statuses = {}
        # issue_number -> list of comment dicts ({body, user.login,
        # author_association, created_at}); tests set entries to steer
        # approval resolution.
        self.issue_comments = {}
        # issue_number -> list of issue-event dicts; "labeled" events carry
        # label.name and actor.login.
        self.issue_events = {}
        # pr_url -> list of comment dicts ({body}); the last one's body is
        # stored as rejection_reason.
        self.pr_comments = {}

    async def list_labeled_issues(self, label: str):
        return []

    async def add_label(self, issue_number: int, label: str) -> None:
        self.events.append(("add_label", issue_number, label))

    async def remove_label(self, issue_number: int, label: str) -> None:
        self.events.append(("remove_label", issue_number, label))

    async def comment(self, issue_number: int, body: str) -> None:
        self.events.append(("comment", issue_number, body))

    async def list_issue_comments(self, issue_number: int):
        return list(self.issue_comments.get(issue_number, []))

    async def list_issue_events(self, issue_number: int):
        return list(self.issue_events.get(issue_number, []))

    async def latest_pr_comment(self, pr_url: str):
        comments = self.pr_comments.get(pr_url, [])
        return comments[-1].get("body") if comments else None

    async def pr_status(self, pr_url: str) -> str:
        self.events.append(("pr_check", pr_url))
        return self.pr_statuses.get(pr_url, "open")
