"""Install / uninstall ONNX models from the curated catalog.

Per the service-mode invariants (ADR-0010) and threat-model T08:
- No bundled weights. First-run downloads to ``/data/models/<id>.onnx``.
- SHA256 verification on every download; mismatch → file deleted,
  install refused.
- Explicit license acceptance is captured in ``audit_log`` with the
  acting admin user_id, model id, sha256, and timestamp.
- HTTPS only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .model_catalog import CatalogEntry

log = logging.getLogger("mediarefinery.service.models")

CHUNK_SIZE = 64 * 1024
MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024  # 1 GiB cap; refuse oversized payloads


class InstallError(RuntimeError):
    """Raised on any install-time failure that is not a hash mismatch."""


class HashMismatch(InstallError):
    pass


@dataclass(frozen=True)
class InstalledModel:
    id: int
    name: str
    version: str
    sha256: str
    license: str | None
    active: bool
    path: Path | None  # None for legacy rows from v1 model_versions


def model_storage_path(data_dir: Path, entry: CatalogEntry) -> Path:
    return data_dir / "models" / f"{entry.id}.onnx"


def install_model(
    *,
    entry: CatalogEntry,
    data_dir: Path,
    conn: sqlite3.Connection,
    actor_user_id: str,
    license_accepted: bool,
    client: httpx.Client | None = None,
    timeout: float = 60.0,
) -> InstalledModel:
    if not entry.installable:
        raise InstallError(f"model {entry.id} is not installable (status={entry.status})")
    if not license_accepted:
        raise InstallError("license must be accepted before install")
    if not entry.url.startswith("https://"):
        raise InstallError(f"refusing non-HTTPS download URL: {entry.url}")

    target = model_storage_path(data_dir, entry)
    target.parent.mkdir(parents=True, exist_ok=True)

    existing = _find_registry_row(conn, name=entry.name, sha256=entry.sha256)
    if existing is not None and target.exists():
        # Idempotent: model already installed and on disk.
        _set_active(conn, existing["id"])
        _audit(
            conn=conn,
            user_id=actor_user_id,
            action="model.install",
            sha256=entry.sha256,
            model_id=entry.id,
            license=entry.license,
            already_installed=True,
        )
        return _row_to_installed(existing, target)

    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True)
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=f"{entry.id}.", suffix=".part", dir=target.parent)
    tmp_path = Path(tmp_name)
    hasher = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(tmp_fd, "wb") as fh, client.stream("GET", entry.url) as response:
            if response.status_code != 200:
                raise InstallError(f"download failed: HTTP {response.status_code}")
            for chunk in response.iter_bytes(chunk_size=CHUNK_SIZE):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise InstallError(
                        f"download exceeded {MAX_DOWNLOAD_BYTES} byte cap"
                    )
                hasher.update(chunk)
                fh.write(chunk)
        actual = hasher.hexdigest()
        if actual != entry.sha256:
            raise HashMismatch(
                f"sha256 mismatch for {entry.id}: expected {entry.sha256}, got {actual}"
            )
        if entry.size_bytes is not None and total != entry.size_bytes:
            raise InstallError(
                f"size mismatch for {entry.id}: expected {entry.size_bytes}, got {total}"
            )
        # Atomic rename onto target.
        if target.exists():
            target.unlink()
        os.replace(tmp_path, target)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise
    finally:
        if own_client:
            client.close()

    row_id = _insert_registry_row(
        conn,
        name=entry.name,
        version=entry.id,
        sha256=entry.sha256,
        license_=entry.license,
    )
    _set_active(conn, row_id)
    _audit(
        conn=conn,
        user_id=actor_user_id,
        action="model.install",
        sha256=entry.sha256,
        model_id=entry.id,
        license=entry.license,
        size_bytes=total,
    )
    log.info(
        "model installed",
        extra={
            "event": "model.install",
            "user_id": actor_user_id,
            "model_id": entry.id,
            "sha256": entry.sha256,
        },
    )
    return InstalledModel(
        id=row_id,
        name=entry.name,
        version=entry.id,
        sha256=entry.sha256,
        license=entry.license,
        active=True,
        path=target,
    )


def uninstall_model(
    *,
    registry_id: int,
    data_dir: Path,
    conn: sqlite3.Connection,
    actor_user_id: str,
) -> None:
    cursor = conn.execute(
        "SELECT id, name, version, sha256 FROM model_registry WHERE id = ?",
        (registry_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise InstallError(f"installed model {registry_id} not found")
    target = data_dir / "models" / f"{row['version']}.onnx"
    if target.exists():
        try:
            target.unlink()
        except OSError as exc:
            raise InstallError(f"unable to remove {target}: {exc}") from exc
    conn.execute("DELETE FROM model_registry WHERE id = ?", (registry_id,))
    conn.commit()
    _audit(
        conn=conn,
        user_id=actor_user_id,
        action="model.uninstall",
        sha256=row["sha256"],
        model_id=row["version"],
    )
    log.info(
        "model uninstalled",
        extra={
            "event": "model.uninstall",
            "user_id": actor_user_id,
            "model_id": row["version"],
        },
    )


def list_installed(*, conn: sqlite3.Connection, data_dir: Path) -> list[InstalledModel]:
    cursor = conn.execute(
        "SELECT id, name, version, sha256, license, active "
        "FROM model_registry ORDER BY id"
    )
    out: list[InstalledModel] = []
    for row in cursor.fetchall():
        path = data_dir / "models" / f"{row['version']}.onnx"
        out.append(
            InstalledModel(
                id=int(row["id"]),
                name=row["name"],
                version=row["version"],
                sha256=row["sha256"],
                license=row["license"],
                active=bool(row["active"]),
                path=path if path.exists() else None,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _find_registry_row(
    conn: sqlite3.Connection, *, name: str, sha256: str
) -> sqlite3.Row | None:
    cursor = conn.execute(
        "SELECT * FROM model_registry WHERE name = ? AND sha256 = ?",
        (name, sha256),
    )
    return cursor.fetchone()


def _insert_registry_row(
    conn: sqlite3.Connection,
    *,
    name: str,
    version: str,
    sha256: str,
    license_: str | None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO model_registry(name, version, sha256, license, active)
        VALUES (?, ?, ?, ?, 0)
        """,
        (name, version, sha256, license_),
    )
    conn.commit()
    return int(cursor.lastrowid)


def _set_active(conn: sqlite3.Connection, registry_id: int) -> None:
    with conn:
        conn.execute("UPDATE model_registry SET active = 0")
        conn.execute(
            "UPDATE model_registry SET active = 1 WHERE id = ?", (registry_id,)
        )


def _audit(
    *,
    conn: sqlite3.Connection,
    user_id: str,
    action: str,
    sha256: str,
    model_id: str,
    license: str | None = None,
    size_bytes: int | None = None,
    already_installed: bool = False,
) -> None:
    details = {
        "model_id": model_id,
        "sha256": sha256,
        "license": license,
        "size_bytes": size_bytes,
        "already_installed": already_installed,
        "accepted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    conn.execute(
        """
        INSERT INTO audit_log(user_id, action, details_json)
        VALUES (?, ?, ?)
        """,
        (user_id, action, json.dumps(details, sort_keys=True)),
    )
    conn.commit()


def _row_to_installed(row: sqlite3.Row, path: Path | None) -> InstalledModel:
    return InstalledModel(
        id=int(row["id"]),
        name=row["name"],
        version=row["version"],
        sha256=row["sha256"],
        license=row["license"],
        active=True,
        path=path,
    )


__all__ = [
    "CHUNK_SIZE",
    "HashMismatch",
    "InstallError",
    "InstalledModel",
    "MAX_DOWNLOAD_BYTES",
    "install_model",
    "list_installed",
    "model_storage_path",
    "uninstall_model",
]
