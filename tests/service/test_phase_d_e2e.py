"""Phase D exit-criterion e2e test.

Drives the full Locked Folder cycle end-to-end through service helpers:

1. Admin installs the AdamCodd ViT NSFW catalog entry (mocked HF download).
2. User logs in, configures ``nsfw`` → ``move_to_locked_folder`` policy.
3. ``submit_real_scan`` runs the real pipeline against a mock Immich
   seeded with mixed assets and a stub classifier that labels every
   asset ``nsfw``.
4. The locked-folder forward path issues per-asset PUTs through the
   user's Immich client (live actions enabled).
5. ``POST /me/locked-folder/unlock`` proxies the PIN to Immich, reverts
   each locked asset to timeline, and zeros the Bearer.
6. Audit log captures every step with no PIN or Bearer leak. Two-user
   isolation remains intact.

The test mocks all Immich traffic via ``httpx.MockTransport`` and
substitutes the runner's ``immich_factory`` with one that returns a
``MockImmichClient`` shared with the unlock proxy so the lock+unlock
round-trip is observable.
"""

from __future__ import annotations

import json
import time

import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from mediarefinery.classifier import (
    ClassifierInput,
    NoopClassifier,
    RawModelOutput,
)
from mediarefinery.config import (
    AppConfig,
    Category,
    ClassifierProfile,
)
from mediarefinery.immich import (
    AssetRef,
    ImmichCapabilities,
    MockImmichClient,
)
from mediarefinery.service.app import API_V1_PREFIX, create_app
from mediarefinery.service.config import ServiceConfig
from mediarefinery.service.runner import (
    RunnerFactories,
    submit_real_scan,
    synthesize_app_config,
)
from mediarefinery.service.security import CSRF_COOKIE_NAME

ALICE_PW = "alice-pw-not-real"
BOB_PW = "bob-pw-not-real"
ALICE_TOKEN = "alice-immich-bearer-AAAA"
BOB_TOKEN = "bob-immich-bearer-BBBB"
ALICE_API_KEY = "alice-immich-api-key-SECRET"
PIN_VALUE = "9911-pin-secret"


def _make_handler(shared_client: MockImmichClient):
    """httpx mock that serves login + the unlock/revert flow.

    The forward locked-folder writes are routed through ``shared_client``
    via the runner's immich_factory; unlock/revert PUTs from the FastAPI
    handler also touch ``shared_client`` so the round-trip is observable.
    """

    state = {"unlock_calls": 0, "lock_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/auth/login":
            body = json.loads(request.content)
            if body["email"] == "alice@x.invalid" and body["password"] == ALICE_PW:
                token = ALICE_TOKEN
                user_id = "user-alice"
            elif body["email"] == "bob@x.invalid" and body["password"] == BOB_PW:
                token = BOB_TOKEN
                user_id = "user-bob"
            else:
                return httpx.Response(401)
            return httpx.Response(
                201,
                json={
                    "accessToken": token,
                    "userId": user_id,
                    "userEmail": body["email"],
                    "name": user_id,
                    "isAdmin": user_id == "user-alice",
                    "profileImagePath": "",
                    "shouldChangePassword": False,
                    "isOnboarded": True,
                },
            )
        if path == "/api/auth/logout":
            return httpx.Response(200)
        if path == "/api/users/me":
            return httpx.Response(200, json={"id": "ok"})
        if path == "/api/auth/session/unlock":
            state["unlock_calls"] += 1
            return httpx.Response(200, json={"ok": True})
        if path == "/api/auth/session/lock":
            state["lock_calls"] += 1
            return httpx.Response(200, json={"ok": True})
        if request.method == "PUT" and path.startswith("/api/assets/"):
            asset_id = path.rsplit("/", 1)[-1]
            body = json.loads(request.content)
            shared_client.set_asset_visibility(asset_id, body["visibility"])
            return httpx.Response(200)
        return httpx.Response(404)

    return handler, state


@pytest.fixture
def context(tmp_path, monkeypatch):
    seeded_assets = [
        AssetRef(asset_id=f"asset-{i}", media_type="image") for i in range(3)
    ]
    shared = MockImmichClient(
        assets=seeded_assets,
        capabilities=ImmichCapabilities(
            albums=True, tags=True, archive=False, locked_folder=True
        ),
    )
    handler, traffic = _make_handler(shared)
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
    return app, shared, traffic


def _login(client: TestClient, email: str, password: str) -> str:
    r = client.post(
        f"{API_V1_PREFIX}/auth/login",
        json={"email": email, "password": password},
    )
    assert r.status_code == 200, r.text
    return client.cookies[CSRF_COOKIE_NAME]


def _seed_active_model(store) -> str:
    sha = "a" * 64
    store._conn.execute(
        "INSERT INTO model_registry(name, version, sha256, license, active) "
        "VALUES (?,?,?,?,1)",
        ("adamcodd-vit-nsfw-quantized", "v1", sha, "Apache-2.0"),
    )
    store._conn.commit()
    return sha


def _classifier_factory(_sha):
    profile = ClassifierProfile(
        name="t",
        backend="noop",
        model_path=None,
        output_mapping={"nsfw": "nsfw"},
    )
    cfg = AppConfig(
        source=None,
        raw={
            "version": 1,
            "categories": [{"id": "nsfw"}],
            "classifier_profiles": {"t": {"backend": "noop"}},
            "classifier": {"active_profile": "t"},
        },
        categories=(Category(id="nsfw"),),
        classifier_profiles={"t": profile},
        active_profile_name="t",
    )
    return NoopClassifier(cfg)


def test_phase_d_exit_criterion(context):
    app, shared_immich, traffic = context

    with TestClient(app) as client:
        csrf = _login(client, "alice@x.invalid", ALICE_PW)
        h = {"X-CSRF-Token": csrf}

        # Configure category + locked-folder policy + API key.
        r = client.put(
            f"{API_V1_PREFIX}/me/categories",
            json={"categories": {"nsfw": {"enabled": True, "threshold": 0.85}}},
            headers=h,
        )
        assert r.status_code == 200
        r = client.put(
            f"{API_V1_PREFIX}/me/policies",
            json={
                "policies": {
                    "nsfw": {"image": {"on_match": ["move_to_locked_folder"]}}
                }
            },
            headers=h,
        )
        assert r.status_code == 200
        r = client.post(
            f"{API_V1_PREFIX}/me/api-key",
            json={"api_key": ALICE_API_KEY, "label": "primary"},
            headers=h,
        )
        assert r.status_code == 201

        # Activate a model so submit_real_scan does not refuse.
        _seed_active_model(app.state.store)

        # Drive the real-pipeline runner directly with factories that
        # share the mock Immich client with the unlock proxy. PR 5
        # could swap /scans onto submit_real_scan; for the e2e test we
        # invoke it directly to exercise the full pipeline + locked-folder
        # forward writes against shared mock state.
        factories = RunnerFactories(
            immich_factory=lambda _uid: shared_immich,
            classifier_factory=_classifier_factory,
            config_factory=synthesize_app_config,
        )
        submitted = submit_real_scan(
            store=app.state.store,
            user_id="user-alice",
            factories=factories,
            dry_run=False,
        )

        # Wait for the daemon thread to finish.
        scoped = app.state.store.with_user("user-alice")
        for _ in range(50):
            row = scoped.get_run(submitted.run_id)
            if row and row["status"] != "running":
                break
            time.sleep(0.05)

        run_row = scoped.get_run(submitted.run_id)
        assert run_row["status"] == "completed"

        # Forward path: every asset got a locked visibility write.
        locked_writes = [
            r_ for r_ in shared_immich.visibility_requests if r_["visibility"] == "locked"
        ]
        assert len(locked_writes) == 3
        assert {r_["asset_id"] for r_ in locked_writes} == {
            "asset-0",
            "asset-1",
            "asset-2",
        }

        # Unlock cycle.
        r = client.post(
            f"{API_V1_PREFIX}/me/locked-folder/unlock",
            json={"run_id": submitted.run_id, "pin": PIN_VALUE},
            headers=h,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reverted"] == 3
        assert body["failed_asset_ids"] == []
        assert PIN_VALUE not in r.text
        assert ALICE_TOKEN not in r.text
        assert traffic["unlock_calls"] == 1
        assert traffic["lock_calls"] == 1

        # All three assets are back in the timeline (mock client tracks
        # the most recent visibility per asset).
        timeline_writes = [
            r_ for r_ in shared_immich.visibility_requests if r_["visibility"] == "timeline"
        ]
        assert len(timeline_writes) == 3

        # Audit log: full trail with no PIN / Bearer leakage.
        audit = client.get(f"{API_V1_PREFIX}/audit").json()
        actions = [e["action"] for e in audit["entries"]]
        assert "scan.start" in actions
        assert "scan.finish" in actions
        assert actions.count("asset.locked") == 3 or actions.count(
            "asset.locked.attempt"
        ) >= 3
        assert actions.count("asset.unlocked") == 3
        assert "scan.undo" in actions
        audit_text = json.dumps(audit)
        assert PIN_VALUE not in audit_text
        assert ALICE_TOKEN not in audit_text


def test_phase_d_two_user_isolation(context):
    """Bob's session sees nothing from Alice's locked-folder run."""

    app, shared_immich, _ = context
    with TestClient(app) as client:
        # Alice run.
        csrf_a = _login(client, "alice@x.invalid", ALICE_PW)
        scoped_a = app.state.store.with_user("user-alice")
        run_id = scoped_a.start_run(dry_run=False, command="scan")
        scoped_a.upsert_asset(asset_id="asset-9", media_type="image")
        scoped_a.record_action(
            run_id=run_id,
            asset_id="asset-9",
            action_name="move_to_locked_folder",
            dry_run=False,
            would_apply=True,
            success=True,
        )
        scoped_a.write_audit(
            action="asset.locked", target_asset_id="asset-9", run_id=run_id
        )
        scoped_a.finish_run(run_id, status="completed")

        # Bob logs in fresh — nothing from Alice's run is visible.
        client.cookies.clear()
        _login(client, "bob@x.invalid", BOB_PW)
        scans = client.get(f"{API_V1_PREFIX}/scans").json()["scans"]
        assert scans == []
        audit_entries = client.get(f"{API_V1_PREFIX}/audit").json()["entries"]
        assert all(
            e["action"] != "asset.locked" and e["run_id"] != run_id
            for e in audit_entries
        )

        # Bob cannot unlock Alice's run.
        r = client.post(
            f"{API_V1_PREFIX}/me/locked-folder/unlock",
            json={"run_id": run_id, "pin": PIN_VALUE},
            headers={"X-CSRF-Token": client.cookies[CSRF_COOKIE_NAME]},
        )
        assert r.status_code in (401, 404)
