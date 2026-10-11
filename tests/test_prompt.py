"""The prompt defines test-count semantics and leaves all label writes
to the service."""

from app.prompt import STRUCTURED_OUTPUT_SCHEMA, build_prompt


def _prompt(**kw):
    return build_prompt(
        issue_number=1,
        title="t",
        url="https://x/1",
        body="b",
        repo="elen-dil/superset-ali",
        **kw,
    )


def test_prompt_does_not_delegate_label_writes():
    prompt = _prompt()
    # The service is the only label writer; Devin must not be told to
    # add the needs-human label itself.
    assert 'add the label "devin-needs-human"' not in prompt
    assert 'explain which rule applied in "blockers"' in prompt


def test_prompt_defines_test_count_fields():
    prompt = _prompt()
    assert '"tests_run" means tests executed' in prompt
    assert '"tests_failed" means' in prompt


def test_schema_includes_tests_failed():
    props = STRUCTURED_OUTPUT_SCHEMA["properties"]
    assert props["tests_failed"] == {"type": "integer"}
    assert props["tests_run"] == {"type": "integer"}
    assert props["tests_passed"] == {"type": "integer"}
