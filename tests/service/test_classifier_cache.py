"""Phase D, PR 2 — ONNX classifier session cache tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from mediarefinery.classifier import (
    ClassifierBackend,
    ClassifierInput,
    NoopClassifierBackend,
    RawModelOutput,
)
from mediarefinery.config import ClassifierProfile
from mediarefinery.service.classifier_cache import (
    ClassifierSessionCache,
    UnknownModelError,
    make_cached_classifier_factory,
    profile_from_catalog_entry,
)
from mediarefinery.service.model_catalog import CatalogEntry


def _entry(id_: str, sha: str) -> CatalogEntry:
    raw = {
        "id": id_,
        "name": id_,
        "kind": "binary_nsfw_classifier",
        "status": "verified",
        "url": f"https://example.invalid/{id_}.onnx",
        "sha256": sha,
        "size_bytes": 1024,
        "license": "Apache-2.0",
        "license_url": "https://example.invalid/license",
        "input_size": [224, 224],
        "input_layout": "NCHW",
        "input_mean": [0.5, 0.5, 0.5],
        "input_std": [0.5, 0.5, 0.5],
        "output_classes": ["sfw", "nsfw"],
    }
    return CatalogEntry(
        id=id_,
        name=raw["name"],
        kind=raw["kind"],
        status=raw["status"],
        url=raw["url"],
        sha256=raw["sha256"],
        size_bytes=raw["size_bytes"],
        license=raw["license"],
        license_url=raw["license_url"],
        presets=(),
        raw=raw,
    )


class _CountingBackend:
    """Test-only backend that records load/predict calls without ORT."""

    constructed = 0

    def __init__(self, profile: ClassifierProfile) -> None:
        type(self).constructed += 1
        self.profile = profile
        self.version = "test"
        self.loaded = False
        self.load_calls = 0

    def load(self) -> None:
        self.load_calls += 1
        self.loaded = True

    def predict_batch(self, inputs):
        return [
            RawModelOutput(
                asset_id=item.asset_id,
                raw_label="sfw",
                raw_scores={"sfw": 1.0, "nsfw": 0.0},
            )
            for item in inputs
        ]


@pytest.fixture(autouse=True)
def _reset_counter():
    _CountingBackend.constructed = 0


def test_profile_from_catalog_entry_pulls_preprocessing(tmp_path):
    entry = _entry("model-a", "a" * 64)
    profile = profile_from_catalog_entry(entry, tmp_path)
    assert profile.backend == "onnx"
    assert profile.input_size == 224
    assert profile.input_mean == (0.5, 0.5, 0.5)
    assert profile.input_std == (0.5, 0.5, 0.5)
    assert profile.output_mapping == {"sfw": "sfw", "nsfw": "nsfw"}
    assert profile.model_path.endswith("model-a.onnx")


def test_cache_hit_returns_same_instance(tmp_path):
    entry = _entry("model-a", "a" * 64)
    cache = ClassifierSessionCache(
        models_dir=tmp_path,
        catalog=[entry],
        backend_factory=_CountingBackend,
    )
    first = cache.get("a" * 64)
    second = cache.get("a" * 64)
    assert first is second
    assert _CountingBackend.constructed == 1
    assert first.backend.load_calls == 1


def test_cache_warm_reload_on_model_swap(tmp_path):
    a = _entry("model-a", "a" * 64)
    b = _entry("model-b", "b" * 64)
    cache = ClassifierSessionCache(
        models_dir=tmp_path,
        catalog=[a, b],
        backend_factory=_CountingBackend,
    )
    first = cache.get("a" * 64)
    second = cache.get("b" * 64)
    assert first is not second
    assert _CountingBackend.constructed == 2
    # Swapping back hits the cache, no rebuild.
    again = cache.get("a" * 64)
    assert again is first
    assert _CountingBackend.constructed == 2


def test_cache_unknown_sha_raises(tmp_path):
    cache = ClassifierSessionCache(
        models_dir=tmp_path,
        catalog=[_entry("model-a", "a" * 64)],
        backend_factory=_CountingBackend,
    )
    with pytest.raises(UnknownModelError):
        cache.get("z" * 64)


def test_cache_invalidate(tmp_path):
    entry = _entry("model-a", "a" * 64)
    cache = ClassifierSessionCache(
        models_dir=tmp_path,
        catalog=[entry],
        backend_factory=_CountingBackend,
    )
    first = cache.get("a" * 64)
    cache.invalidate("a" * 64)
    second = cache.get("a" * 64)
    assert first is not second
    assert _CountingBackend.constructed == 2
    cache.invalidate()
    assert cache.cached_shas == ()


def test_cache_get_none_returns_none(tmp_path):
    cache = ClassifierSessionCache(
        models_dir=tmp_path,
        catalog=[],
        backend_factory=_CountingBackend,
    )
    assert cache.get(None) is None


def test_factory_adapter_matches_runner_seam(tmp_path):
    entry = _entry("model-a", "a" * 64)
    cache = ClassifierSessionCache(
        models_dir=tmp_path,
        catalog=[entry],
        backend_factory=_CountingBackend,
    )
    factory = make_cached_classifier_factory(cache)
    classifier = factory("a" * 64)
    assert classifier is cache.get("a" * 64)
    with pytest.raises(UnknownModelError):
        factory(None)
