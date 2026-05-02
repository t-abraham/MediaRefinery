"""MR_DEMO=1 startup gate (threat-model T16)."""

from __future__ import annotations

import logging

import pytest

from mediarefinery.service.config import load_service_config


def _base_env(**overrides: str) -> dict[str, str]:
    env = {"MR_IMMICH_BASE_URL": "http://immich.invalid"}
    env.update(overrides)
    return env


def test_demo_mode_unset_defaults_false():
    cfg = load_service_config(env=_base_env())
    assert cfg.demo_mode is False


def test_demo_mode_one_enables_flag():
    cfg = load_service_config(env=_base_env(MR_DEMO="1"))
    assert cfg.demo_mode is True


def test_demo_mode_other_values_treated_as_false():
    cfg = load_service_config(env=_base_env(MR_DEMO="true"))
    assert cfg.demo_mode is False


def test_demo_mode_with_master_key_rejects_startup():
    with pytest.raises(RuntimeError, match="mutually exclusive"):
        load_service_config(
            env=_base_env(MR_DEMO="1", MR_MASTER_KEY="any-non-empty"),
        )


def test_demo_banner_logged_on_create_app(tmp_path, monkeypatch, caplog):
    fastapi = pytest.importorskip("fastapi")  # noqa: F841
    import httpx

    from mediarefinery.service.app import create_app
    from mediarefinery.service.config import ServiceConfig

    cfg = ServiceConfig(
        immich_base_url="http://immich.invalid",
        base_url="http://localhost:8080",
        data_dir=tmp_path,
        trusted_proxies=(),
        session_ttl_seconds=3600,
        revalidate_interval_seconds=10_000_000,
        login_rate_per_min=100,
        cookie_secure=False,
        demo_mode=True,
    )

    original = httpx.Client

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(404)
        )
        return original(*args, **kwargs)

    monkeypatch.setattr("mediarefinery.service.app.httpx.Client", patched)

    with caplog.at_level(logging.WARNING, logger="mediarefinery.service"):
        create_app(config=cfg)

    messages = [rec.getMessage() for rec in caplog.records]
    assert any("MR_DEMO=1 active" in m for m in messages), messages
