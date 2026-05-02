"""Tests for the catalog loader."""

from __future__ import annotations

import json

import pytest

from mediarefinery.service.model_catalog import (
    SUPPORTED_SCHEMA_VERSION,
    CatalogError,
    find_entry,
    load_catalog,
)


def _write(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def _entry(**overrides):
    base = {
        "id": "m-1",
        "name": "M1",
        "kind": "generic_image_classifier",
        "status": "verified",
        "url": "https://example.invalid/model.onnx",
        "sha256": "a" * 64,
        "size_bytes": 100,
        "license": "Apache-2.0",
        "license_url": "https://example.invalid/LICENSE",
        "presets": ["generic"],
    }
    base.update(overrides)
    return base


def test_load_catalog_real_file():
    entries = load_catalog()
    ids = {e.id for e in entries}
    assert "mobilenet-v2-imagenet-onnx" in ids
    assert all(e.installable for e in entries if e.status == "verified")


def test_missing_file_raises(tmp_path):
    with pytest.raises(CatalogError, match="not found"):
        load_catalog(tmp_path / "missing.json")


def test_invalid_json_raises(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(CatalogError, match="not valid JSON"):
        load_catalog(p)


def test_unsupported_schema_raises(tmp_path):
    p = tmp_path / "c.json"
    _write(p, {"$schema_version": "9", "models": []})
    with pytest.raises(CatalogError, match="schema"):
        load_catalog(p)


def test_models_must_be_list(tmp_path):
    p = tmp_path / "c.json"
    _write(p, {"$schema_version": SUPPORTED_SCHEMA_VERSION, "models": {}})
    with pytest.raises(CatalogError, match="must be a list"):
        load_catalog(p)


def test_duplicate_id_rejected(tmp_path):
    p = tmp_path / "c.json"
    _write(p, {"$schema_version": SUPPORTED_SCHEMA_VERSION, "models": [_entry(), _entry()]})
    with pytest.raises(CatalogError, match="duplicate"):
        load_catalog(p)


def test_verified_must_be_https(tmp_path):
    p = tmp_path / "c.json"
    _write(
        p,
        {
            "$schema_version": SUPPORTED_SCHEMA_VERSION,
            "models": [_entry(url="http://example.invalid/m.onnx")],
        },
    )
    with pytest.raises(CatalogError, match="https://"):
        load_catalog(p)


def test_verified_must_have_64_char_sha(tmp_path):
    p = tmp_path / "c.json"
    _write(
        p,
        {
            "$schema_version": SUPPORTED_SCHEMA_VERSION,
            "models": [_entry(sha256="short")],
        },
    )
    with pytest.raises(CatalogError, match="sha256"):
        load_catalog(p)


def test_unavailable_skips_strict_checks(tmp_path):
    p = tmp_path / "c.json"
    _write(
        p,
        {
            "$schema_version": SUPPORTED_SCHEMA_VERSION,
            "models": [
                _entry(
                    id="dead",
                    status="unavailable",
                    url="http://gone/m.onnx",
                    sha256="UPSTREAM_NOT_FOUND",
                )
            ],
        },
    )
    entries = load_catalog(p)
    assert len(entries) == 1
    assert not entries[0].installable


def test_find_entry():
    entries = load_catalog()
    assert find_entry(entries, "mobilenet-v2-imagenet-onnx") is not None
    assert find_entry(entries, "nope") is None


def test_missing_required_field(tmp_path):
    p = tmp_path / "c.json"
    bad = _entry()
    bad.pop("status")
    _write(p, {"$schema_version": SUPPORTED_SCHEMA_VERSION, "models": [bad]})
    with pytest.raises(CatalogError, match="missing field"):
        load_catalog(p)
