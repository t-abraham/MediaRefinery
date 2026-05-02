"""Loader for the curated ONNX model catalog (`docs/models/catalog.json`).

The catalog ships in the repository (and inside the Docker image) and
is read at request-time by the model-lifecycle endpoints. Schema is
documented in catalog.json itself; this module enforces the bits the
backend cares about: every entry has a stable id, a verified or
unavailable status, and (when verified) a sha256 + size_bytes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

CATALOG_PATH = Path("docs/models/catalog.json")
SUPPORTED_SCHEMA_VERSION = "2"


class CatalogError(RuntimeError):
    """Raised when the catalog file is missing, malformed, or refers to
    an unknown schema version."""


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    name: str
    kind: str
    status: str
    url: str
    sha256: str
    size_bytes: int | None
    license: str
    license_url: str
    presets: tuple[str, ...]
    raw: dict

    @property
    def installable(self) -> bool:
        return self.status == "verified"


def load_catalog(path: Path | str | None = None) -> list[CatalogEntry]:
    target = Path(path) if path is not None else CATALOG_PATH
    if not target.exists():
        raise CatalogError(f"catalog file not found: {target}")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CatalogError(f"catalog is not valid JSON: {exc}") from exc

    schema = str(data.get("$schema_version", ""))
    if schema != SUPPORTED_SCHEMA_VERSION:
        raise CatalogError(
            f"catalog schema {schema!r} is not supported "
            f"(expected {SUPPORTED_SCHEMA_VERSION!r})"
        )

    raw_models = data.get("models")
    if not isinstance(raw_models, list):
        raise CatalogError("catalog.models must be a list")

    entries: list[CatalogEntry] = []
    seen_ids: set[str] = set()
    for raw in raw_models:
        if not isinstance(raw, dict):
            raise CatalogError("each catalog entry must be an object")
        try:
            entry = CatalogEntry(
                id=str(raw["id"]),
                name=str(raw["name"]),
                kind=str(raw["kind"]),
                status=str(raw["status"]),
                url=str(raw["url"]),
                sha256=str(raw["sha256"]),
                size_bytes=raw.get("size_bytes"),
                license=str(raw["license"]),
                license_url=str(raw.get("license_url", "")),
                presets=tuple(str(p) for p in raw.get("presets", ())),
                raw=raw,
            )
        except KeyError as exc:
            raise CatalogError(f"catalog entry missing field: {exc}") from exc
        if entry.id in seen_ids:
            raise CatalogError(f"duplicate catalog entry id: {entry.id}")
        seen_ids.add(entry.id)
        if entry.installable:
            if not entry.url.startswith("https://"):
                raise CatalogError(
                    f"verified entry {entry.id} must use https:// (got {entry.url!r})"
                )
            if not entry.sha256 or len(entry.sha256) != 64:
                raise CatalogError(
                    f"verified entry {entry.id} must have a 64-char sha256"
                )
        entries.append(entry)
    return entries


def find_entry(entries: list[CatalogEntry], model_id: str) -> CatalogEntry | None:
    for entry in entries:
        if entry.id == model_id:
            return entry
    return None


__all__ = [
    "CATALOG_PATH",
    "CatalogEntry",
    "CatalogError",
    "SUPPORTED_SCHEMA_VERSION",
    "find_entry",
    "load_catalog",
]
