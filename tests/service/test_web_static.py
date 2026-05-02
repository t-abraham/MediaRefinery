"""Phase E PR 1: dashboard static-serve and CSP headers.

These tests assert two load-bearing privacy guarantees:

1. Every response carries a strict Content-Security-Policy with no
   third-party origins (threat-model T01, T11).
2. The built dashboard bundle is served at ``/`` from the same origin
   as the API.

The frontend bundle is built into ``src/mediarefinery/web/`` by
``npm run build``; here we point the app at a tmp dir with a stub
``index.html`` via ``MR_WEB_ROOT`` so the tests do not depend on the
node toolchain.
"""

from __future__ import annotations

import pytest


def _make_app(tmp_path, monkeypatch, *, with_bundle: bool):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from mediarefinery.service.app import create_app
    from mediarefinery.service.config import ServiceConfig

    web_root = tmp_path / "web"
    web_root.mkdir()
    if with_bundle:
        (web_root / "index.html").write_text(
            "<!doctype html><html><body>stub</body></html>", encoding="utf-8"
        )
    monkeypatch.setenv("MR_WEB_ROOT", str(web_root))

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
    return TestClient(app)


def _assert_strict_csp(headers):
    csp = headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    # T11: no third-party origins anywhere in the policy.
    forbidden = ("http://", "https://", "*.")
    for token in forbidden:
        assert token not in csp, f"CSP must not reference {token!r}: {csp}"
    # T01: no inline script ever.
    assert "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";", 1)[0]
    assert "frame-ancestors 'none'" in csp
    assert "form-action 'self'" in csp
    assert "object-src 'none'" in csp


def test_security_headers_on_api_response(tmp_path, monkeypatch):
    client = _make_app(tmp_path, monkeypatch, with_bundle=False)
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    _assert_strict_csp(resp.headers)
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "same-origin"
    assert resp.headers["x-frame-options"] == "DENY"
    assert "interest-cohort=()" in resp.headers["permissions-policy"]
    # HSTS only when cookie_secure (i.e. https base_url).
    assert "strict-transport-security" not in (k.lower() for k in resp.headers)


def test_static_index_served_when_bundle_present(tmp_path, monkeypatch):
    client = _make_app(tmp_path, monkeypatch, with_bundle=True)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "stub" in resp.text
    _assert_strict_csp(resp.headers)


def test_root_404_when_bundle_missing(tmp_path, monkeypatch):
    client = _make_app(tmp_path, monkeypatch, with_bundle=False)
    resp = client.get("/")
    # No mount when bundle missing — backend tests still work without
    # invoking the node toolchain.
    assert resp.status_code == 404
    _assert_strict_csp(resp.headers)
