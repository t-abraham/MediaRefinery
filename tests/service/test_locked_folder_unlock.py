"""Phase D, PR 4 — PIN-unlock + revert flow privacy tests.

Validates threat-model T09 / T10:

- The PIN flows from request to Immich without being logged, audited,
  or persisted.
- The PIN-unlocked Bearer token is held only for the request handler
  and never reaches state-v2.db.
- Response bodies, audit_log rows, and DB rows do not contain the PIN
  string or the Bearer token string anywhere a future query could
  surface them.
"""

from __future__ import annotations

import json

import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from mediarefinery.service.app import API_V1_PREFIX, create_app  # noqa: E402
from mediarefinery.service.config import ServiceConfig  # noqa: E402
from mediarefinery.service.security import CSRF_COOKIE_NAME  # noqa: E402

ALICE_PW = "alice-pw-not-real"
ALICE_TOKEN = "alice-immich-bearer-XYZ123"
PIN_VALUE = "1379-secret-pin"


def _login_response() -> httpx.Response:
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


def _make_handler(*, unlock_status: int = 200):
    state = {"unlock_calls": 0, "lock_calls": 0, "visibility_writes": []}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/auth/login":
            return _login_response()
        if path == "/api/auth/logout":
            return httpx.Response(200)
        if path == "/api/users/me":
            return httpx.Response(200, json={"id": "ok"})
        if path == "/api/auth/session/unlock":
            state["unlock_calls"] += 1
            return httpx.Response(unlock_status, json={"ok": True})
        if path == "/api/auth/session/lock":
            state["lock_calls"] += 1
            return httpx.Response(200, json={"ok": True})
        if request.method == "PUT" and path.startswith("/api/assets/"):
            asset_id = path.rsplit("/", 1)[-1]
            body = json.loads(request.content)
            state["visibility_writes"].append(
                {"asset_id": asset_id, "visibility": body.get("visibility")}
            )
            return httpx.Response(200)
        return httpx.Response(404)

    return handler, state


@pytest.fixture
def app_state(tmp_path, monkeypatch):
    handler, traffic = _make_handler()
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
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr("mediarefinery.service.app.httpx.Client", patched)
    return create_app(config=cfg), traffic


def _login(client: TestClient) -> str:
    r = client.post(
        f"{API_V1_PREFIX}/auth/login",
        json={"email": "alice@x.invalid", "password": ALICE_PW},
    )
    assert r.status_code == 200
    return client.cookies[CSRF_COOKIE_NAME]


def _seed_locked_run(app, user_id: str = "user-alice") -> int:
    store = app.state.store
    scoped = store.with_user(user_id)
    run_id = scoped.start_run(dry_run=False, command="scan")
    for asset_id in ("asset-1", "asset-2"):
        scoped.upsert_asset(asset_id=asset_id, media_type="image")
        scoped.record_action(
            run_id=run_id,
            asset_id=asset_id,
            action_name="move_to_locked_folder",
            dry_run=False,
            would_apply=True,
            success=True,
        )
    scoped.finish_run(run_id, status="completed")
    return run_id


def test_unlock_revert_happy_path(app_state):
    app, traffic = app_state
    with TestClient(app) as client:
        csrf = _login(client)
        run_id = _seed_locked_run(app)

        r = client.post(
            f"{API_V1_PREFIX}/me/locked-folder/unlock",
            json={"run_id": run_id, "pin": PIN_VALUE},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {"run_id": run_id, "reverted": 2, "failed_asset_ids": []}

        # Verify Immich traffic.
        assert traffic["unlock_calls"] == 1
        assert traffic["lock_calls"] == 1
        assert traffic["visibility_writes"] == [
            {"asset_id": "asset-1", "visibility": "timeline"},
            {"asset_id": "asset-2", "visibility": "timeline"},
        ]

        # Audit reflects per-asset unlocks plus the scan.undo summary.
        audit = client.get(f"{API_V1_PREFIX}/audit").json()["entries"]
        actions = [e["action"] for e in audit]
        assert actions.count("asset.unlocked") == 2
        assert "scan.undo" in actions


def test_unlock_pin_never_appears_in_response_or_audit(app_state):
    app, _ = app_state
    with TestClient(app) as client:
        csrf = _login(client)
        run_id = _seed_locked_run(app)

        r = client.post(
            f"{API_V1_PREFIX}/me/locked-folder/unlock",
            json={"run_id": run_id, "pin": PIN_VALUE},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 200
        assert PIN_VALUE not in r.text
        assert ALICE_TOKEN not in r.text

        audit = client.get(f"{API_V1_PREFIX}/audit").json()
        audit_text = json.dumps(audit)
        assert PIN_VALUE not in audit_text
        assert ALICE_TOKEN not in audit_text


def test_unlock_bearer_never_persists_to_db(app_state):
    """The PIN-unlocked Bearer is held only in-request and is not written
    back to ``sessions`` (the encrypted blob in the row is the original
    pre-unlock token, but the plaintext bytes never appear elsewhere).
    """

    app, _ = app_state
    with TestClient(app) as client:
        csrf = _login(client)
        run_id = _seed_locked_run(app)
        client.post(
            f"{API_V1_PREFIX}/me/locked-folder/unlock",
            json={"run_id": run_id, "pin": PIN_VALUE},
            headers={"X-CSRF-Token": csrf},
        )

        # No other table should contain the bearer or pin in plaintext.
        store = app.state.store
        for table in ("audit_log", "actions", "errors", "user_config"):
            cursor = store._conn.execute(f"SELECT * FROM {table}")
            for row in cursor.fetchall():
                blob = json.dumps({k: row[k] for k in row.keys()}, default=str)
                assert PIN_VALUE not in blob, table
                assert ALICE_TOKEN not in blob, table


def test_unlock_invalid_pin_returns_401(tmp_path, monkeypatch):
    handler, _ = _make_handler(unlock_status=401)
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
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr("mediarefinery.service.app.httpx.Client", patched)
    app = create_app(config=cfg)
    with TestClient(app) as client:
        csrf = _login(client)
        run_id = _seed_locked_run(app)
        r = client.post(
            f"{API_V1_PREFIX}/me/locked-folder/unlock",
            json={"run_id": run_id, "pin": PIN_VALUE},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 401
        assert PIN_VALUE not in r.text


def test_unlock_no_locked_actions_returns_400(app_state):
    app, _ = app_state
    with TestClient(app) as client:
        csrf = _login(client)
        # Seed a run with no locked-folder actions.
        store = app.state.store
        scoped = store.with_user("user-alice")
        run_id = scoped.start_run(dry_run=False, command="scan")
        scoped.finish_run(run_id, status="completed")
        r = client.post(
            f"{API_V1_PREFIX}/me/locked-folder/unlock",
            json={"run_id": run_id, "pin": PIN_VALUE},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 400


def test_unlock_unknown_run_returns_404(app_state):
    app, _ = app_state
    with TestClient(app) as client:
        csrf = _login(client)
        r = client.post(
            f"{API_V1_PREFIX}/me/locked-folder/unlock",
            json={"run_id": 99999, "pin": PIN_VALUE},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 404
