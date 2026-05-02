"""Synthetic Immich auth fixtures for ``MR_DEMO=1`` (threat-model T16)."""

from __future__ import annotations

import pytest

httpx = pytest.importorskip("httpx")

from mediarefinery.service.demo_fixtures import (  # noqa: E402
    DEMO_USER_EMAIL,
    DEMO_USER_ID,
    build_demo_immich_client,
    synthetic_immich_handler,
)


def _get(client: httpx.Client, path: str) -> httpx.Response:
    return client.get(path)


def _post(client: httpx.Client, path: str, body: dict) -> httpx.Response:
    return client.post(path, json=body)


def test_synthetic_login_accepts_any_credentials():
    client = build_demo_immich_client(base_url="http://demo.invalid")
    r = _post(
        client, "/api/auth/login", {"email": "anyone@x.invalid", "password": "x"}
    )
    assert r.status_code == 201
    body = r.json()
    assert body["userId"] == DEMO_USER_ID
    assert body["userEmail"] == DEMO_USER_EMAIL
    assert body["isAdmin"] is True
    assert "accessToken" in body


def test_synthetic_login_rejects_empty_password():
    client = build_demo_immich_client(base_url="http://demo.invalid")
    r = _post(client, "/api/auth/login", {"email": "x@y.invalid", "password": ""})
    assert r.status_code == 401


def test_synthetic_logout_succeeds():
    client = build_demo_immich_client(base_url="http://demo.invalid")
    r = client.post("/api/auth/logout")
    assert r.status_code == 200


def test_synthetic_users_me_returns_demo_user():
    client = build_demo_immich_client(base_url="http://demo.invalid")
    r = _get(client, "/api/users/me")
    assert r.status_code == 200
    assert r.json()["id"] == DEMO_USER_ID


def test_synthetic_server_about_returns_demo_marker():
    client = build_demo_immich_client(base_url="http://demo.invalid")
    r = _get(client, "/api/server/about")
    assert r.status_code == 200
    assert r.json().get("demo") is True


def test_unstubbed_path_returns_404_explicitly():
    # We want misrouted calls to surface, not silently succeed.
    r = synthetic_immich_handler(
        httpx.Request("GET", "http://demo.invalid/api/some/missing/path")
    )
    assert r.status_code == 404
    assert "demo fixture" in r.json().get("error", "")


def test_demo_mode_seeds_active_model(tmp_path):
    fastapi = pytest.importorskip("fastapi")  # noqa: F841
    from fastapi.testclient import TestClient

    from mediarefinery.service.app import create_app
    from mediarefinery.service.config import ServiceConfig
    from mediarefinery.service.demo_fixtures import DEMO_MODEL_SHA256

    cfg = ServiceConfig(
        immich_base_url="http://demo-immich.invalid",
        base_url="http://localhost:8080",
        data_dir=tmp_path,
        trusted_proxies=(),
        session_ttl_seconds=3600,
        revalidate_interval_seconds=10_000_000,
        login_rate_per_min=100,
        cookie_secure=False,
        demo_mode=True,
    )
    app = create_app(config=cfg)
    with TestClient(app):
        store = app.state.store
        assert store.active_model_sha256() == DEMO_MODEL_SHA256


def test_demo_mode_end_to_end_scan(tmp_path):
    """A demo container can sign in and run a scan to completion
    without a real Immich and without a real model on disk."""

    fastapi = pytest.importorskip("fastapi")  # noqa: F841
    import time

    from fastapi.testclient import TestClient

    from mediarefinery.service.app import API_V1_PREFIX, create_app
    from mediarefinery.service.config import ServiceConfig
    from mediarefinery.service.security import CSRF_COOKIE_NAME

    cfg = ServiceConfig(
        immich_base_url="http://demo-immich.invalid",
        base_url="http://localhost:8080",
        data_dir=tmp_path,
        trusted_proxies=(),
        session_ttl_seconds=3600,
        revalidate_interval_seconds=10_000_000,
        login_rate_per_min=100,
        cookie_secure=False,
        demo_mode=True,
    )
    app = create_app(config=cfg)
    with TestClient(app) as client:
        r = client.post(
            f"{API_V1_PREFIX}/auth/login",
            json={"email": "demo@x.invalid", "password": "anything"},
        )
        assert r.status_code == 200, r.text
        csrf = client.cookies[CSRF_COOKIE_NAME]
        h = {"X-CSRF-Token": csrf}

        r = client.post(f"{API_V1_PREFIX}/scans", headers=h)
        assert r.status_code == 202, r.text
        run_id = r.json()["run_id"]

        deadline = time.monotonic() + 5.0
        body: dict = {}
        while time.monotonic() < deadline:
            body = client.get(f"{API_V1_PREFIX}/scans/{run_id}").json()
            if body["status"] in ("completed", "failed"):
                break
            time.sleep(0.05)
        assert body.get("status") == "completed", body


def test_demo_mode_app_uses_synthetic_login(tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi")  # noqa: F841
    from fastapi.testclient import TestClient

    from mediarefinery.service.app import API_V1_PREFIX, create_app
    from mediarefinery.service.config import ServiceConfig

    cfg = ServiceConfig(
        immich_base_url="http://demo-immich.invalid",
        base_url="http://localhost:8080",
        data_dir=tmp_path,
        trusted_proxies=(),
        session_ttl_seconds=3600,
        revalidate_interval_seconds=10_000_000,
        login_rate_per_min=100,
        cookie_secure=False,
        demo_mode=True,
    )

    app = create_app(config=cfg)
    with TestClient(app) as client:
        r = client.post(
            f"{API_V1_PREFIX}/auth/login",
            json={"email": "demo@x.invalid", "password": "anything"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["user_id"] == DEMO_USER_ID
        assert body["email"] == DEMO_USER_EMAIL
