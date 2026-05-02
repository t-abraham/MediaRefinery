"""Phase C PR 3 — bootstrap endpoint + first-user-admin + reclassify CTA."""

from __future__ import annotations

import hashlib
import json

import pytest

httpx = pytest.importorskip("httpx")
fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from mediarefinery.service.app import API_V1_PREFIX, create_app  # noqa: E402
from mediarefinery.service.config import ServiceConfig  # noqa: E402
from mediarefinery.service.security import CSRF_COOKIE_NAME  # noqa: E402

PASSWORD = "pw"
PAYLOAD = b"y" * 4096
PAYLOAD_SHA = hashlib.sha256(PAYLOAD).hexdigest()


def _login_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if body.get("password") != PASSWORD:
        return httpx.Response(401)
    is_admin = body["email"].startswith("admin@")
    user_id = "admin-1" if is_admin else f"user-{body['email'].split('@')[0]}"
    return httpx.Response(
        201,
        json={
            "accessToken": "tok",
            "userId": user_id,
            "userEmail": body["email"],
            "name": body["email"],
            "isAdmin": is_admin,
            "profileImagePath": "",
            "shouldChangePassword": False,
            "isOnboarded": True,
        },
    )


def _handler(request):
    if "/api/auth/login" in request.url.path:
        return _login_handler(request)
    if "/api/auth/logout" in request.url.path:
        return httpx.Response(200)
    if "/api/users/me" in request.url.path:
        return httpx.Response(200, json={"id": "ok"})
    if request.url.host == "example.invalid":
        return httpx.Response(200, content=PAYLOAD)
    return httpx.Response(404)


@pytest.fixture
def app(tmp_path, monkeypatch):
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "$schema_version": "2",
                "models": [
                    {
                        "id": "m-test",
                        "name": "Test",
                        "kind": "generic_image_classifier",
                        "status": "verified",
                        "url": "https://example.invalid/m.onnx",
                        "sha256": PAYLOAD_SHA,
                        "size_bytes": len(PAYLOAD),
                        "license": "Apache-2.0",
                        "license_url": "https://example.invalid/L",
                        "presets": ["generic"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = ServiceConfig(
        immich_base_url="http://immich.invalid",
        base_url="http://localhost",
        data_dir=tmp_path,
        trusted_proxies=(),
        session_ttl_seconds=3600,
        revalidate_interval_seconds=10_000_000,
        login_rate_per_min=100,
        cookie_secure=False,
    )
    original = httpx.Client

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_handler)
        return original(*args, **kwargs)

    monkeypatch.setattr("mediarefinery.service.app.httpx.Client", patched)
    monkeypatch.setattr("mediarefinery.service.model_lifecycle.httpx.Client", patched)
    a = create_app(config=cfg)
    a.state.catalog_path = catalog_path
    return a


def _login(client, email):
    r = client.post(
        f"{API_V1_PREFIX}/auth/login",
        json={"email": email, "password": PASSWORD},
    )
    assert r.status_code == 200, r.text
    return client.cookies[CSRF_COOKIE_NAME]


def test_bootstrap_status_initial(app):
    with TestClient(app) as client:
        r = client.get(f"{API_V1_PREFIX}/setup/bootstrap")
        body = r.json()
        assert body == {
            "terms_accepted": False,
            "users_exist": False,
            "admin_present": False,
            "ready": False,
        }


def test_bootstrap_accept_terms(app):
    with TestClient(app) as client:
        r = client.post(
            f"{API_V1_PREFIX}/setup/bootstrap",
            json={"accept_terms": True},
        )
        assert r.status_code == 200
        assert r.json()["terms_accepted"] is True

        # Re-bootstrap is refused.
        r2 = client.post(
            f"{API_V1_PREFIX}/setup/bootstrap",
            json={"accept_terms": True},
        )
        assert r2.status_code == 409


def test_bootstrap_refuses_without_acceptance(app):
    with TestClient(app) as client:
        r = client.post(
            f"{API_V1_PREFIX}/setup/bootstrap",
            json={"accept_terms": False},
        )
        assert r.status_code == 400


def test_first_user_promoted_to_admin_even_if_not_immich_admin(app):
    with TestClient(app) as client:
        csrf = _login(client, "alice@x.invalid")  # is_admin=False from Immich
        # Alice should still be able to install a model (admin-gated).
        r = client.post(
            f"{API_V1_PREFIX}/models/install",
            json={"model_id": "m-test", "license_accepted": True},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 201, r.text


def test_second_non_admin_user_is_not_admin(app):
    with TestClient(app) as client:
        # Alice logs in first, becomes admin.
        _login(client, "alice@x.invalid")
        client.cookies.clear()
        # Bob logs in second; admin already exists, no promotion.
        csrf = _login(client, "bob@x.invalid")
        r = client.post(
            f"{API_V1_PREFIX}/models/install",
            json={"model_id": "m-test", "license_accepted": True},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 403


def test_immich_admin_remains_admin(app):
    with TestClient(app) as client:
        # Pre-create a regular user (non-admin) so the promotion path
        # has already been spent.
        _login(client, "regular@x.invalid")
        client.cookies.clear()
        # admin@ has Immich isAdmin=True; should still be admin.
        csrf = _login(client, "admin@x.invalid")
        r = client.post(
            f"{API_V1_PREFIX}/models/install",
            json={"model_id": "m-test", "license_accepted": True},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 201


def test_reclassify_cta_appears_after_active_model_changes(app):
    with TestClient(app) as client:
        csrf = _login(client, "alice@x.invalid")
        h = {"X-CSRF-Token": csrf}

        # Initial state: no active model, no last_seen → no CTA.
        body = client.get(f"{API_V1_PREFIX}/me/categories").json()
        assert body["needs_reclassify"] is False
        assert body["active_model_sha256"] is None

        # Install a model → active sha set.
        client.post(
            f"{API_V1_PREFIX}/models/install",
            json={"model_id": "m-test", "license_accepted": True},
            headers=h,
        )
        body = client.get(f"{API_V1_PREFIX}/me/categories").json()
        # Still no CTA because last_seen has not been recorded yet.
        assert body["active_model_sha256"] == PAYLOAD_SHA
        assert body["last_seen_model_sha256"] is None
        assert body["needs_reclassify"] is False


def test_reclassify_cta_flips_when_model_swaps():
    """Direct state-layer test: needs_reclassify flips when the active
    model sha differs from the user's last-seen sha.
    """

    from mediarefinery.service.state_v2 import StateStoreV2

    db = StateStoreV2(":memory:")
    db.initialize()
    db.upsert_user(user_id="u1", email="u1@x.invalid")

    scoped = db.with_user("u1")
    scoped.mark_model_seen("a" * 64)

    # Pretend a model is active with a different sha.
    db._conn.execute(
        "INSERT INTO model_registry(name, version, sha256, license, active) "
        "VALUES (?, ?, ?, ?, 1)",
        ("M", "v", "b" * 64, "Apache-2.0"),
    )
    db._conn.commit()

    assert db.active_model_sha256() == "b" * 64
    assert scoped.last_seen_model_sha256() == "a" * 64
    db.close()


def test_admin_count_starts_at_zero():
    from mediarefinery.service.state_v2 import StateStoreV2

    db = StateStoreV2(":memory:")
    db.initialize()
    assert db.admin_count() == 0
    db.upsert_user(user_id="u", email="u@x.invalid", is_admin=False)
    assert db.admin_count() == 0
    db.promote_to_admin("u")
    assert db.admin_count() == 1
    db.close()


def test_service_settings_get_set():
    from mediarefinery.service.state_v2 import StateStoreV2

    db = StateStoreV2(":memory:")
    db.initialize()
    assert db.get_setting("k") is None
    db.set_setting("k", {"x": 1})
    assert db.get_setting("k") == {"x": 1}
    db.set_setting("k", {"x": 2})
    assert db.get_setting("k") == {"x": 2}
    db.close()
