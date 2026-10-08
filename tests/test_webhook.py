import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app.main import create_app

SECRET = "test-secret"


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _labeled_payload(number=42, label="devin-remediate"):
    return {
        "action": "labeled",
        "label": {"name": label},
        "issue": {
            "number": number,
            "title": "Fix the thing",
            "html_url": f"https://github.com/elen-dil/superset-ali/issues/{number}",
            "state": "open",
            "body": "please fix",
        },
    }


def _client(settings, components):
    store, devin, github = components
    app = create_app(settings, store=store, devin=devin, github=github, run_loops=False)
    return TestClient(app)


def test_ping_responds_pong(settings, components):
    client = _client(settings, components)
    body = b"{}"
    resp = client.post(
        "/webhooks/github",
        content=body,
        headers={"x-github-event": "ping", "x-hub-signature-256": _sign(body)},
    )
    assert resp.status_code == 200
    assert resp.json() == {"result": "pong"}


def test_bad_signature_rejected(settings, components):
    client = _client(settings, components)
    resp = client.post(
        "/webhooks/github",
        content=b"{}",
        headers={"x-github-event": "ping", "x-hub-signature-256": "sha256=deadbeef"},
    )
    assert resp.status_code == 401


def test_missing_signature_rejected(settings, components):
    client = _client(settings, components)
    resp = client.post(
        "/webhooks/github", content=b"{}", headers={"x-github-event": "ping"}
    )
    assert resp.status_code == 401


def test_labeled_trigger_enqueues(settings, components):
    client = _client(settings, components)
    body = json.dumps(_labeled_payload()).encode()
    resp = client.post(
        "/webhooks/github",
        content=body,
        headers={"x-github-event": "issues", "x-hub-signature-256": _sign(body)},
    )
    assert resp.status_code == 200
    assert resp.json() == {"result": "queued"}


def test_labeled_other_label_ignored(settings, components):
    client = _client(settings, components)
    body = json.dumps(_labeled_payload(label="bug")).encode()
    resp = client.post(
        "/webhooks/github",
        content=body,
        headers={"x-github-event": "issues", "x-hub-signature-256": _sign(body)},
    )
    assert resp.json() == {"result": "ignored"}


def test_same_issue_twice_is_idempotent(settings, components):
    client = _client(settings, components)
    body = json.dumps(_labeled_payload()).encode()
    headers = {"x-github-event": "issues", "x-hub-signature-256": _sign(body)}
    assert client.post("/webhooks/github", content=body, headers=headers).json() == {
        "result": "queued"
    }
    assert client.post("/webhooks/github", content=body, headers=headers).json() == {
        "result": "already_active"
    }
