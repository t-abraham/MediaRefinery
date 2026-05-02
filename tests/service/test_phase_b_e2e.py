"""Phase B PR 5 — exit-criterion e2e tests.

Two-user attack probe: Alice and Bob each log in via the proxied
Immich, configure categories, store an API key, trigger a scan, list
their runs and audit, and undo. Bob attempts to read or mutate
Alice's data via every documented endpoint and must get 401/403/404.
"""

from __future__ import annotations

import json
import time

import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from mediarefinery.service.app import API_V1_PREFIX, create_app  # noqa: E402
from mediarefinery.service.config import ServiceConfig  # noqa: E402
from mediarefinery.service.security import CSRF_COOKIE_NAME  # noqa: E402

ALICE_PW = "alice-pw-not-real"
BOB_PW = "bob-pw-not-real"
ALICE_TOKEN = "alice-immich-token-AAAA"
BOB_TOKEN = "bob-immich-token-BBBB"
SHARED_API_KEY = "fake-immich-api-key-SHHH"


def _login_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if body.get("email") == "alice@x.invalid" and body.get("password") == ALICE_PW:
        return httpx.Response(
            201,
            json={
                "accessToken": ALICE_TOKEN,
                "userId": "user-alice",
                "userEmail": "alice@x.invalid",
                "name": "Alice",
                "isAdmin": False,
                "profileImagePath": "",
                "shouldChangePassword": False,
                "isOnboarded": True,
            },
        )
    if body.get("email") == "bob@x.invalid" and body.get("password") == BOB_PW:
        return httpx.Response(
            201,
            json={
                "accessToken": BOB_TOKEN,
                "userId": "user-bob",
                "userEmail": "bob@x.invalid",
                "name": "Bob",
                "isAdmin": False,
                "profileImagePath": "",
                "shouldChangePassword": False,
                "isOnboarded": True,
            },
        )
    return httpx.Response(401, json={"error": "Unauthorized"})


def _immich_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/auth/login":
        return _login_handler(request)
    if request.url.path == "/api/auth/logout":
        return httpx.Response(200)
    if request.url.path == "/api/users/me":
        return httpx.Response(200, json={"id": "ok"})
    return httpx.Response(404)


@pytest.fixture
def app(tmp_path, monkeypatch):
    cfg = ServiceConfig(
        immich_base_url="http://immich.invalid",
        base_url="http://localhost:8080",
        data_dir=tmp_path,
        trusted_proxies=(),
        session_ttl_seconds=3600,
        revalidate_interval_seconds=10_000_000,
        login_rate_per_min=100,
        cookie_secure=False,
    )
    original = httpx.Client

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_immich_handler)
        return original(*args, **kwargs)

    monkeypatch.setattr("mediarefinery.service.app.httpx.Client", patched)
    return create_app(config=cfg)


def _login(client: TestClient, email: str, password: str) -> str:
    r = client.post(
        f"{API_V1_PREFIX}/auth/login",
        json={"email": email, "password": password},
    )
    assert r.status_code == 200, r.text
    return client.cookies[CSRF_COOKIE_NAME]


def _wait_for_scan(client: TestClient, run_id: int, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"{API_V1_PREFIX}/scans/{run_id}").json()
        if body["status"] == "completed":
            return body
        time.sleep(0.05)
    raise AssertionError("scan did not complete in time")


def test_phase_b_exit_criterion_single_user(app):
    with TestClient(app) as client:
        csrf = _login(client, "alice@x.invalid", ALICE_PW)
        h = {"X-CSRF-Token": csrf}

        # Configure categories + policies + api key.
        r = client.put(
            f"{API_V1_PREFIX}/me/categories",
            json={"categories": {"pets": {"enabled": True, "threshold": 0.7}}},
            headers=h,
        )
        assert r.status_code == 200
        assert r.json()["categories"]["pets"]["enabled"] is True

        r = client.put(
            f"{API_V1_PREFIX}/me/policies",
            json={"policies": {"pets": {"image": {"on_match": "tag"}}}},
            headers=h,
        )
        assert r.status_code == 200

        r = client.post(
            f"{API_V1_PREFIX}/me/api-key",
            json={"api_key": SHARED_API_KEY, "label": "primary"},
            headers=h,
        )
        assert r.status_code == 201

        # Trigger scan, wait for completion, see results.
        r = client.post(f"{API_V1_PREFIX}/scans", headers=h)
        assert r.status_code == 202
        run_id = r.json()["run_id"]

        body = _wait_for_scan(client, run_id)
        assert body["status"] == "completed"
        assert len(body["actions"]) == 2
        assert all(action["success"] is True for action in body["actions"])

        # Undo it.
        r = client.post(f"{API_V1_PREFIX}/scans/{run_id}/undo", headers=h)
        assert r.status_code == 200
        assert r.json()["reverted"] == 2

        # Audit reflects the journey.
        audit = client.get(f"{API_V1_PREFIX}/audit").json()["entries"]
        actions = {entry["action"] for entry in audit}
        assert {"login", "categories.update", "policies.update",
                "api_key.store", "scan.start", "scan.finish",
                "scan.undo"} <= actions

        # API key never echoes back in plaintext.
        listed = client.get(f"{API_V1_PREFIX}/me/api-key").json()["api_keys"]
        assert listed and SHARED_API_KEY not in json.dumps(listed)


def test_phase_b_exit_criterion_multi_tenant_isolation(app):
    """Cross-tenant attack probe: Bob cannot read or mutate Alice's data.

    Uses one TestClient (one lifespan, one shared backing store) and
    swaps cookie jars between users — exactly matches the production
    attack model where two browsers hit the same MR backend.
    """

    with TestClient(app) as client:
        alice_csrf = _login(client, "alice@x.invalid", ALICE_PW)
        a_h = {"X-CSRF-Token": alice_csrf}

        client.put(
            f"{API_V1_PREFIX}/me/categories",
            json={"categories": {"alice-secret": {"enabled": True}}},
            headers=a_h,
        )
        client.post(
            f"{API_V1_PREFIX}/me/api-key",
            json={"api_key": "alice-only-key", "label": "alice"},
            headers=a_h,
        )
        run_id_alice = client.post(
            f"{API_V1_PREFIX}/scans", headers=a_h
        ).json()["run_id"]
        _wait_for_scan(client, run_id_alice)

        # Snapshot Alice's cookie jar, then become Bob.
        alice_cookies = dict(client.cookies)
        client.cookies.clear()
        bob_csrf = _login(client, "bob@x.invalid", BOB_PW)
        b_h = {"X-CSRF-Token": bob_csrf}

        bob_cats = client.get(f"{API_V1_PREFIX}/me/categories").json()
        assert bob_cats["categories"] == {}
        assert client.get(f"{API_V1_PREFIX}/me/api-key").json()["api_keys"] == []
        assert client.get(f"{API_V1_PREFIX}/scans").json()["scans"] == []

        b_audit = client.get(f"{API_V1_PREFIX}/audit").json()["entries"]
        # Bob's audit only contains his own login.
        assert {entry["action"] for entry in b_audit} <= {"login"}

        r = client.get(f"{API_V1_PREFIX}/scans/{run_id_alice}")
        assert r.status_code == 404
        assert "alice" not in r.text.lower()

        r = client.post(
            f"{API_V1_PREFIX}/scans/{run_id_alice}/undo", headers=b_h
        )
        assert r.status_code == 404

        # Restore Alice's cookies and verify her data survived.
        client.cookies.clear()
        for name, value in alice_cookies.items():
            client.cookies.set(name, value)
        a_categories = client.get(f"{API_V1_PREFIX}/me/categories").json()
        assert "alice-secret" in a_categories["categories"]


def test_concurrency_cap_returns_409(app, monkeypatch):
    # Make the runner block until released so the second submit hits
    # the concurrency cap.
    import threading

    started = threading.Event()
    proceed = threading.Event()

    def slow_runner(store, user_id, run_id):
        started.set()
        proceed.wait(timeout=3.0)
        from mediarefinery.service.scheduler import synthetic_runner

        synthetic_runner(store, user_id, run_id)

    from mediarefinery.service import scheduler as sched

    original_submit = sched.submit_scan

    def submit_with_slow(*args, **kwargs):
        kwargs.setdefault("runner", slow_runner)
        return original_submit(*args, **kwargs)

    monkeypatch.setattr(sched, "submit_scan", submit_with_slow)

    with TestClient(app) as client:
        csrf = _login(client, "alice@x.invalid", ALICE_PW)
        h = {"X-CSRF-Token": csrf}
        r1 = client.post(f"{API_V1_PREFIX}/scans", headers=h)
        assert r1.status_code == 202
        started.wait(timeout=2.0)
        r2 = client.post(f"{API_V1_PREFIX}/scans", headers=h)
        assert r2.status_code == 409
        proceed.set()
        # Drain the slow runner so the worker thread exits before the
        # lifespan closes the SQLite connection. Skipping this triggers
        # a use-after-close access violation on Windows.
        _wait_for_scan(client, r1.json()["run_id"])


def test_csrf_required_for_mutations(app):
    with TestClient(app) as client:
        _login(client, "alice@x.invalid", ALICE_PW)
        # No X-CSRF-Token header.
        r = client.put(
            f"{API_V1_PREFIX}/me/categories",
            json={"categories": {}},
        )
        assert r.status_code == 403
        r = client.post(f"{API_V1_PREFIX}/scans")
        assert r.status_code == 403


def test_unauthenticated_endpoints_return_401(app):
    with TestClient(app) as client:
        for path in ("/me", "/me/categories", "/scans", "/audit"):
            r = client.get(f"{API_V1_PREFIX}{path}")
            assert r.status_code == 401, path


def test_openapi_lists_v1_endpoints(app):
    with TestClient(app) as client:
        r = client.get(f"{API_V1_PREFIX}/openapi.json")
        assert r.status_code == 200
        paths = r.json()["paths"]
        for required in (
            "/api/v1/auth/login",
            "/api/v1/auth/logout",
            "/api/v1/me",
            "/api/v1/me/categories",
            "/api/v1/me/policies",
            "/api/v1/me/api-key",
            "/api/v1/scans",
            "/api/v1/scans/{run_id}",
            "/api/v1/scans/{run_id}/undo",
            "/api/v1/audit",
            "/api/v1/health",
            "/api/v1/health/ready",
        ):
            assert required in paths, f"missing {required}"


def test_privacy_no_secrets_after_full_session(app):
    with TestClient(app) as client:
        csrf = _login(client, "alice@x.invalid", ALICE_PW)
        h = {"X-CSRF-Token": csrf}
        client.post(
            f"{API_V1_PREFIX}/me/api-key",
            json={"api_key": SHARED_API_KEY, "label": "k"},
            headers=h,
        )
        run_id = client.post(f"{API_V1_PREFIX}/scans", headers=h).json()["run_id"]
        _wait_for_scan(client, run_id)
        for path in (
            "/me", "/me/categories", "/me/policies", "/me/api-key",
            "/scans", f"/scans/{run_id}", "/audit",
        ):
            text = client.get(f"{API_V1_PREFIX}{path}").text
            for forbidden in (ALICE_PW, ALICE_TOKEN, SHARED_API_KEY):
                assert forbidden not in text, f"{forbidden!r} leaked in {path}"
