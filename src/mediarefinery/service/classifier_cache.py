"""Process-cached ONNX classifier sessions for v2 service mode (Phase D PR 2).

``onnxruntime.InferenceSession`` construction is heavyweight (model
parse + graph optimisation + provider warm-up). Each scan would pay
that cost on every asset if the runner constructed a fresh classifier
per call. This module provides a small per-process cache keyed on the
active model's sha256 — the same key the catalog and ``model_registry``
agree on — plus a factory adapter that plugs into the runner's
``classifier_factory`` seam introduced in PR 1.

The cache is intentionally simple:

- One ``ConfiguredClassifier`` per sha; loaded lazily on first request
  for that sha and reused thereafter.
- A swap to a different active model lazily loads the new entry; the
  old session stays cached so a second swap back is free. Memory
  footprint is bounded by the catalog (3 entries today).
- ``invalidate()`` clears the cache when an admin uninstalls a model.

The actual ONNX backend is injectable so tests can exercise the cache
without a real model file. The default backend factory uses
``OnnxClassifierBackend`` from the v1 codebase unchanged.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from ..classifier import (
    ClassifierBackend,
    ConfiguredClassifier,
)
from ..config import ClassifierProfile
from ..onnx_backend import OnnxClassifierBackend
from .model_catalog import CatalogEntry

BackendFactory = Callable[[ClassifierProfile], ClassifierBackend]
ClassifierFactory = Callable[[str | None], ConfiguredClassifier]


def profile_from_catalog_entry(
    entry: CatalogEntry, models_dir: Path
) -> ClassifierProfile:
    """Build a v1 :class:`ClassifierProfile` from a catalog entry.

    Pulls preprocessing fields (``input_size``, ``input_mean``,
    ``input_std``, ``output_classes``) out of ``entry.raw`` since
    :class:`CatalogEntry` only models the registry-relevant subset.
    """

    raw = entry.raw
    input_size_raw = raw.get("input_size") or [224, 224]
    if isinstance(input_size_raw, (list, tuple)) and input_size_raw:
        input_size = int(input_size_raw[0])
    else:
        input_size = int(input_size_raw)

    def _triplet(key: str, default: tuple[float, float, float]) -> tuple[float, float, float]:
        value = raw.get(key)
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            return default
        return (float(value[0]), float(value[1]), float(value[2]))

    mean = _triplet("input_mean", (0.0, 0.0, 0.0))
    std = _triplet("input_std", (1.0, 1.0, 1.0))

    classes = raw.get("output_classes")
    if not isinstance(classes, (list, tuple)) or not classes:
        classes = ["uncategorised"]
    output_mapping = {str(label): str(label) for label in classes}

    model_path = Path(models_dir) / f"{entry.id}.onnx"
    return ClassifierProfile(
        name=entry.id,
        backend="onnx",
        model_path=str(model_path),
        output_mapping=output_mapping,
        model_version=entry.id,
        input_size=input_size,
        input_mean=mean,
        input_std=std,
    )


def _default_backend_factory(profile: ClassifierProfile) -> ClassifierBackend:
    return OnnxClassifierBackend(profile)


class UnknownModelError(LookupError):
    """Raised when the active sha has no matching catalog entry."""


class ClassifierSessionCache:
    """Per-process cache of :class:`ConfiguredClassifier` keyed on sha256.

    Thread-safe under the v2 single-replica execution model: a lock
    guards cache reads/writes so two scans starting simultaneously do
    not both pay the cold-load cost.
    """

    def __init__(
        self,
        *,
        models_dir: Path | str,
        catalog: list[CatalogEntry],
        backend_factory: BackendFactory | None = None,
    ) -> None:
        self._dir = Path(models_dir)
        self._catalog = list(catalog)
        self._backend_factory = backend_factory or _default_backend_factory
        self._cache: dict[str, ConfiguredClassifier] = {}
        self._lock = threading.Lock()

    @property
    def cached_shas(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._cache.keys())

    def get(self, sha256: str | None) -> ConfiguredClassifier | None:
        if sha256 is None:
            return None
        with self._lock:
            cached = self._cache.get(sha256)
            if cached is not None:
                return cached
            entry = self._find_entry_by_sha(sha256)
            if entry is None:
                raise UnknownModelError(
                    f"no catalog entry matches active sha {sha256[:8]}…"
                )
            profile = profile_from_catalog_entry(entry, self._dir)
            backend = self._backend_factory(profile)
            classifier = ConfiguredClassifier(profile, backend)
            self._cache[sha256] = classifier
            return classifier

    def invalidate(self, sha256: str | None = None) -> None:
        with self._lock:
            if sha256 is None:
                self._cache.clear()
            else:
                self._cache.pop(sha256, None)

    def _find_entry_by_sha(self, sha256: str) -> CatalogEntry | None:
        for entry in self._catalog:
            if entry.sha256 == sha256:
                return entry
        return None


def make_cached_classifier_factory(
    cache: ClassifierSessionCache,
) -> ClassifierFactory:
    """Adapter for ``service.runner.RunnerFactories.classifier_factory``."""

    def factory(active_sha: str | None) -> ConfiguredClassifier:
        classifier = cache.get(active_sha)
        if classifier is None:
            raise UnknownModelError("classifier requested with no active model sha")
        return classifier

    return factory


__all__ = [
    "BackendFactory",
    "ClassifierFactory",
    "ClassifierSessionCache",
    "UnknownModelError",
    "make_cached_classifier_factory",
    "profile_from_catalog_entry",
]
