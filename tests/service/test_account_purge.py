"""DELETE /api/v1/me — account purge (threat-model T20).

Asserts that calling DELETE /me removes every row that belongs to the
caller (sessions, API keys, runs, actions, errors, assets, user_config,
users), anonymizes the audit_log entries to ``user_deleted``, and does
not touch another user's data.
"""

from __future__ import annotations

import json

import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from mediarefinery.service.app import API_V1_PREFIX, create_app  # noqa: E402
from mediarefinery.service.config import ServiceConfig  # noqa: E402
from mediarefinery.service.security import (  # noqa: E402
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
)

ALICE_PW = "alice-pw-not-real"
BOB_PW = "bob-pw-not-real"
ALICE_TOKEN = "alice-immich-token-AAAA"
BOB_TOKEN = "bob-immich-token-BBBB"


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


def _row_count(conn, sql: str, params: tuple) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def test_delete_me_purges_all_user_rows(app):
    with TestClient(app) as client:
        csrf = _login(client, "alice@x.invalid", ALICE_PW)
        h = {"X-CSRF-Token": csrf}

        client.put(
            f"{API_V1_PREFIX}/me/categories",
            json={"categories": {"pets": {"enabled": True}}},
            headers=h,
        )
        r = client.post(
            f"{API_V1_PREFIX}/me/api-key",
            json={"api_key": "alice-only-key", "label": "alice"},
            headers=h,
        )
        assert r.status_code == 201

        store = app.state.store
        conn = store._conn

        # Pre-purge: rows present, audit entries owned by alice.
        assert _row_count(conn, "SELECT COUNT(*) FROM users WHERE user_id = ?", ("user-alice",)) == 1
        assert _row_count(conn, "SELECT COUNT(*) FROM sessions WHERE user_id = ?", ("user-alice",)) >= 1
        assert _row_count(conn, "SELECT COUNT(*) FROM user_api_keys WHERE user_id = ?", ("user-alice",)) >= 1
        assert _row_count(conn, "SELECT COUNT(*) FROM user_config WHERE user_id = ?", ("user-alice",)) >= 1
        pre_audit = _row_count(conn, "SELECT COUNT(*) FROM audit_log WHERE user_id = ?", ("user-alice",))
        assert pre_audit >= 1

        r = client.delete(f"{API_V1_PREFIX}/me", headers=h)
        assert r.status_code == 204

        # Cookies cleared on the response.
        assert SESSION_COOKIE_NAME not in r.cookies or r.cookies.get(SESSION_COOKIE_NAME) in (None, "")

        # Every user-scoped table is empty for alice.
        for table in (
            "sessions",
            "user_api_keys",
            "actions",
            "errors",
            "assets",
            "runs",
            "user_config",
            "users",
        ):
            assert (
                _row_count(conn, f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", ("user-alice",))
                == 0
            ), f"{table} still has rows for purged user"

        # Audit_log entries are anonymized to the sentinel.
        assert _row_count(conn, "SELECT COUNT(*) FROM audit_log WHERE user_id = ?", ("user-alice",)) == 0
        anon = _row_count(conn, "SELECT COUNT(*) FROM audit_log WHERE user_id = ?", ("user_deleted",))
        assert anon >= pre_audit


def test_delete_me_does_not_touch_other_users(app):
    with TestClient(app) as client:
        # Bob signs up first, configures categories + api key.
        bob_csrf = _login(client, "bob@x.invalid", BOB_PW)
        b_h = {"X-CSRF-Token": bob_csrf}
        client.put(
            f"{API_V1_PREFIX}/me/categories",
            json={"categories": {"bob-secret": {"enabled": True}}},
            headers=b_h,
        )
        client.post(
            f"{API_V1_PREFIX}/me/api-key",
            json={"api_key": "bob-only-key", "label": "bob"},
            headers=b_h,
        )

        # Alice logs in (separate cookie jar swap), purges herself.
        client.cookies.clear()
        alice_csrf = _login(client, "alice@x.invalid", ALICE_PW)
        a_h = {"X-CSRF-Token": alice_csrf}
        client.put(
            f"{API_V1_PREFIX}/me/categories",
            json={"categories": {"alice-secret": {"enabled": True}}},
            headers=a_h,
        )
        r = client.delete(f"{API_V1_PREFIX}/me", headers=a_h)
        assert r.status_code == 204

        # Bob's data is intact.
        store = app.state.store
        conn = store._conn
        assert _row_count(conn, "SELECT COUNT(*) FROM users WHERE user_id = ?", ("user-bob",)) == 1
        assert _row_count(conn, "SELECT COUNT(*) FROM user_config WHERE user_id = ?", ("user-bob",)) == 1
        assert _row_count(conn, "SELECT COUNT(*) FROM user_api_keys WHERE user_id = ?", ("user-bob",)) == 1
        assert _row_count(conn, "SELECT COUNT(*) FROM audit_log WHERE user_id = ?", ("user-bob",)) >= 1
