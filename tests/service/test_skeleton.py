"""Phase B PR 1: smoke tests for the service skeleton.

These tests must pass without the ``[service]`` extra installed for
import-safety, and exercise ``create_app`` only when FastAPI is
available.
"""

from __future__ import annotations

import importlib

import pytest


def test_service_package_imports_without_extras():
    pkg = importlib.import_module("mediarefinery.service")
    for name in ("app", "routers", "deps", "security", "scheduler", "audit", "models"):
        importlib.import_module(f"mediarefinery.service.{name}")
        assert name in pkg.__all__


def test_create_app_when_fastapi_available(tmp_path):
    pytest.importorskip("fastapi")
    from mediarefinery.service.app import API_V1_PREFIX, create_app
    from mediarefinery.service.config import ServiceConfig

    config = ServiceConfig(
        immich_base_url="http://immich.invalid",
        base_url="http://localhost:8080",
        data_dir=tmp_path,
        trusted_proxies=(),
        session_ttl_seconds=3600,
        revalidate_interval_seconds=300,
        login_rate_per_min=5,
        cookie_secure=False,
    )
    app = create_app(config=config)
    assert app.title == "MediaRefinery"
    assert app.openapi_url == f"{API_V1_PREFIX}/openapi.json"
