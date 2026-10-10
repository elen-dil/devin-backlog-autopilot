"""Standardized prompt and structured-output schema for remediation sessions."""

STRUCTURED_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {
            "type": "string",
            "enum": ["fixed", "needs_human", "not_reproducible"],
        },
        "summary": {"type": "string"},
        "root_cause": {"type": "string"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "tests_run": {"type": "integer"},
        "tests_passed": {"type": "integer"},
        "risk_notes": {"type": "string"},
        "blockers": {"type": "string"},
    },
    "required": ["outcome", "summary", "tests_passed"],
}


def build_prompt(
    *,
    issue_number: int,
    title: str,
    url: str,
    body: str,
    repo: str,
    needs_human_label: str,
    approval: dict | None = None,
) -> str:
    return f"""You are remediating a GitHub issue in the repository {repo}.

Issue #{issue_number}: {title}
URL: {url}

{body or "(no issue body)"}
{_approval_block(approval)}
Rules:
- Work only in the {repo} fork. Keep the change minimal and scoped to this issue.
- First reproduce or confirm the finding against master. If you cannot confirm
  it still exists, report outcome "not_reproducible" and stop.
- Use test-driven development where practical: write a failing test that
  demonstrates the issue, then implement the fix. Do not open a pull
  request until the relevant tests pass.
- Open the PR against the master branch and include "Fixes #{issue_number}"
  in the PR description.
- Never merge the PR.
- Mark outcome "needs_human" and do not open a PR if the fix requires any
  of the following:
  - a major-version upgrade of a dependency, or removal of supported
    behavior/protocols (a breaking change),
  - a dependency version published less than 30 days ago, or one without
    verifiable provenance,
  - changes to auth, crypto, or network-security defaults beyond the
    issue's stated scope.
  In that case add the label "{needs_human_label}" to the issue, explain
  which rule applied in "blockers", and stop.
- Before finishing, report your result via the structured output tool.
"""


def _approval_block(approval: dict | None) -> str:
    """Waiver section for runs dispatched under the approval label. A
    reviewer comment waives the escalation rules the previous run cited;
    everything else in Rules still applies."""
    if not approval:
        return ""
    previous = approval.get("previous_output") or {}
    previous_text = ""
    if previous:
        previous_text = (
            "\nPrevious run report:\n"
            f"- Summary: {previous.get('summary') or 'n/a'}\n"
            f"- Escalation rules cited (blockers): "
            f"{previous.get('blockers') or 'n/a'}\n"
            f"- Risk notes: {previous.get('risk_notes') or 'n/a'}\n"
        )
    return f"""
Human approval override:
A reviewer approved this run after a previous escalation.{previous_text}
Reviewer decision (verbatim):
{approval.get('decision') or 'n/a'}

The escalation rule(s) cited in the previous run are waived per this
decision. Implement within its constraints. All other rules still apply.
If you find a new risk not covered by the decision, escalate again.

"""
