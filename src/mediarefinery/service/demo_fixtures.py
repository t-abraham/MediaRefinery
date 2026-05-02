"""Synthetic Immich responses for ``MR_DEMO=1`` deployments.

This module exists so that a demo container can boot without a real
Immich behind it. It covers three surfaces:

1. **Auth.** An ``httpx.MockTransport`` returns canned responses for
   the endpoints the service actually calls (``/auth/login``,
   ``/auth/logout``, ``/users/me``, ``/server/about``). Anyone can
   sign in.
2. **Model registry.** :func:`seed_demo_model` inserts a synthetic
   active model into ``model_registry`` so the wizard's install step
   is satisfied without downloading a real ONNX file. The classifier
   factory is the existing noop backend; demo classification is
   deterministic.
3. **Scanner.** :func:`build_demo_runner_factories` returns the
   ``RunnerFactories`` the runner should use in demo mode: a
   ``MockImmichClient`` populated with three synthetic assets, the
   noop classifier, and the regular config synthesiser. Scans
   complete end-to-end without a real Immich or model.

Threat model: **T16**. Loaded only when ``ServiceConfig.demo_mode is
True``; production deployments cannot reach this code path because
demo mode is mutually exclusive with ``MR_MASTER_KEY`` (enforced in
:func:`load_service_config`).
"""

from __future__ import annotations

import json
import sqlite3

import httpx

from ..immich import AssetRef, MockImmichClient
from .runner import (
    RunnerFactories,
    _default_classifier_factory,
    synthesize_app_config,
)

DEMO_USER_ID = "demo-user"
DEMO_USER_EMAIL = "demo@mediarefinery.invalid"
DEMO_USER_NAME = "Demo User"
DEMO_ACCESS_TOKEN = "demo-bearer-token-not-real"


def _login_response() -> httpx.Response:
    return httpx.Response(
        201,
        json={
            "accessToken": DEMO_ACCESS_TOKEN,
            "userId": DEMO_USER_ID,
            "userEmail": DEMO_USER_EMAIL,
            "name": DEMO_USER_NAME,
            "isAdmin": True,
            "profileImagePath": "",
            "shouldChangePassword": False,
            "isOnboarded": True,
        },
    )


def synthetic_immich_handler(request: httpx.Request) -> httpx.Response:
    """``MockTransport`` handler simulating a minimal Immich.

    Any non-empty email/password is accepted. Logout, ``/users/me``,
    and ``/server/about`` return success. Everything else returns 404
    so a misrouted call surfaces in tests rather than appearing to
    silently succeed.
    """

    path = request.url.path
    if path == "/api/auth/login":
        try:
            body = json.loads(request.content or b"{}")
        except ValueError:
            return httpx.Response(400, json={"error": "Bad Request"})
        if body.get("email") and body.get("password"):
            return _login_response()
        return httpx.Response(401, json={"error": "Unauthorized"})
    if path == "/api/auth/logout":
        return httpx.Response(200, json={"successful": True})
    if path == "/api/users/me":
        return httpx.Response(
            200,
            json={
                "id": DEMO_USER_ID,
                "email": DEMO_USER_EMAIL,
                "name": DEMO_USER_NAME,
                "isAdmin": True,
            },
        )
    if path == "/api/server/about":
        return httpx.Response(
            200, json={"version": "demo-fixture", "demo": True}
        )
    return httpx.Response(404, json={"error": "demo fixture: path not stubbed"})


def build_demo_immich_client(*, base_url: str) -> httpx.Client:
    """Return an ``httpx.Client`` whose transport is the synthetic
    handler. Used by :func:`mediarefinery.service.app.create_app` when
    ``config.demo_mode`` is True.
    """

    return httpx.Client(
        base_url=base_url,
        timeout=10.0,
        transport=httpx.MockTransport(synthetic_immich_handler),
    )


# ---------------------------------------------------------------------------
# Model + scanner fixtures
# ---------------------------------------------------------------------------

DEMO_MODEL_NAME = "demo-classifier"
DEMO_MODEL_VERSION = "demo-1.0"
# 64-char synthetic sha so the registry's UNIQUE(name, version, sha256)
# constraint is satisfied. The string itself is never written to disk;
# the noop classifier never reads bytes.
DEMO_MODEL_SHA256 = "demo" + "0" * 60
DEMO_MODEL_LICENSE = "synthetic"


def seed_demo_model(conn: sqlite3.Connection) -> None:
    """Insert the synthetic active model into ``model_registry``.

    Idempotent: subsequent calls are no-ops because of the UNIQUE
    constraint on ``(name, version, sha256)``. Marked ``active = 1``
    so :func:`StateStoreV2.active_model_sha256` returns the demo sha
    and :func:`submit_real_scan` does not refuse with
    ``no_active_model``.
    """

    cursor = conn.execute(
        "SELECT id FROM model_registry WHERE name = ? AND version = ? AND sha256 = ?",
        (DEMO_MODEL_NAME, DEMO_MODEL_VERSION, DEMO_MODEL_SHA256),
    )
    if cursor.fetchone() is not None:
        return
    conn.execute("UPDATE model_registry SET active = 0")
    conn.execute(
        """
        INSERT INTO model_registry(name, version, sha256, license, active)
        VALUES (?, ?, ?, ?, 1)
        """,
        (DEMO_MODEL_NAME, DEMO_MODEL_VERSION, DEMO_MODEL_SHA256, DEMO_MODEL_LICENSE),
    )
    conn.commit()


def synthetic_assets() -> tuple[AssetRef, ...]:
    """Three deterministic image assets for demo scans."""

    return (
        AssetRef(
            asset_id="demo-asset-001",
            media_type="image",
            checksum="sha256:demo-001",
            metadata={"mock_raw_label": "demo"},
        ),
        AssetRef(
            asset_id="demo-asset-002",
            media_type="image",
            checksum="sha256:demo-002",
            metadata={"mock_raw_label": "demo"},
        ),
        AssetRef(
            asset_id="demo-asset-003",
            media_type="image",
            checksum="sha256:demo-003",
            metadata={"mock_raw_label": "demo"},
        ),
    )


def _demo_immich_factory(_user_id: str) -> MockImmichClient:
    return MockImmichClient(assets=list(synthetic_assets()))


def build_demo_runner_factories() -> RunnerFactories:
    """Runner injection seam for ``MR_DEMO=1``.

    - Immich: ``MockImmichClient`` over :func:`synthetic_assets`.
    - Classifier: the standard noop factory (no ONNX disk reads).
    - Config: the standard per-user config synthesiser.
    """

    return RunnerFactories(
        immich_factory=_demo_immich_factory,
        classifier_factory=_default_classifier_factory,
        config_factory=synthesize_app_config,
    )


__all__ = [
    "DEMO_ACCESS_TOKEN",
    "DEMO_MODEL_LICENSE",
    "DEMO_MODEL_NAME",
    "DEMO_MODEL_SHA256",
    "DEMO_MODEL_VERSION",
    "DEMO_USER_EMAIL",
    "DEMO_USER_ID",
    "DEMO_USER_NAME",
    "build_demo_immich_client",
    "build_demo_runner_factories",
    "seed_demo_model",
    "synthesize_app_config",
    "synthetic_assets",
    "synthetic_immich_handler",
]
