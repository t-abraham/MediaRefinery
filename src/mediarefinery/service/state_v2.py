"""v2 state store: multi-tenant SQLite schema + per-user scoped accessor.

This module is intentionally separate from :mod:`mediarefinery.state` (the
v1 CLI state). Per ADR-0010, v2 starts with a fresh
``state-v2.db``; there is no in-place migration from v1.

The schema adds five v2-only tables (``users``, ``sessions``,
``user_api_keys``, ``audit_log``, ``model_registry``) and re-issues the
v1 pipeline tables with a non-nullable ``user_id`` column so the
multi-tenant isolation invariant is enforced at the database layer:
every row that belongs to a tenant carries that tenant's id, and every
read goes through :meth:`StateStoreV2.with_user`, which transparently
scopes queries by ``user_id``.

Encryption-at-rest for ``sessions.encrypted_immich_token`` and
``user_api_keys.encrypted_key`` is handled by ``service.security`` in
PR 3; this module stores opaque ``BLOB`` payloads and does not import
the cryptography package.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION_V2 = 1

SCHEMA_SQL_V2 = """
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    name TEXT,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    encrypted_immich_token BLOB NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL,
    last_revalidated_at TEXT,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS user_api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    label TEXT,
    encrypted_key BLOB NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_user_api_keys_user ON user_api_keys(user_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(user_id),
    at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    action TEXT NOT NULL,
    target_asset_id TEXT,
    run_id INTEGER,
    before_state TEXT,
    after_state TEXT,
    details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_user_at ON audit_log(user_id, at);

CREATE TABLE IF NOT EXISTS model_registry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    license TEXT,
    installed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    active INTEGER NOT NULL DEFAULT 0,
    UNIQUE(name, version, sha256)
);

CREATE TABLE IF NOT EXISTS assets (
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    asset_id TEXT NOT NULL,
    media_type TEXT NOT NULL,
    immich_checksum_or_version TEXT,
    first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_processed TEXT,
    PRIMARY KEY (user_id, asset_id)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TEXT,
    status TEXT NOT NULL,
    dry_run INTEGER NOT NULL,
    command TEXT,
    summary_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_user ON runs(user_id);

CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    asset_id TEXT NOT NULL,
    action_name TEXT NOT NULL,
    dry_run INTEGER NOT NULL,
    would_apply INTEGER NOT NULL,
    success INTEGER,
    error_code TEXT,
    ran_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_actions_user_run ON actions(user_id, run_id);

CREATE TABLE IF NOT EXISTS user_config (
    user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    categories_json TEXT NOT NULL DEFAULT '{}',
    policies_json TEXT NOT NULL DEFAULT '{}',
    last_seen_model_sha256 TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS service_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    asset_id TEXT,
    stage TEXT NOT NULL,
    message_code TEXT NOT NULL,
    message TEXT,
    details_json TEXT,
    at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_errors_user ON errors(user_id);

PRAGMA user_version = 1;
"""

USER_ID_RE = re.compile(r"[A-Za-z0-9_.:@-]{1,128}")


def _validate_user_id(user_id: str) -> str:
    if not isinstance(user_id, str) or not USER_ID_RE.fullmatch(user_id):
        raise ValueError("user_id must match [A-Za-z0-9_.:@-]{1,128}")
    return user_id


@dataclass(frozen=True)
class UserRecord:
    user_id: str
    email: str
    name: str | None
    is_admin: bool


class StateStoreV2:
    """Connection-owning v2 state store. Use :meth:`with_user` to read or
    write tenant-scoped data; never touch ``self._conn`` directly from
    outside this module.
    """

    def __init__(self, sqlite_path: str | Path):
        self.path = Path(sqlite_path) if str(sqlite_path) != ":memory:" else sqlite_path
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn_target = str(self.path)
        else:
            conn_target = ":memory:"
        # FastAPI runs sync routes in a threadpool; the connection must
        # be reachable from request threads. SQLite serialises writers
        # internally, which is fine for the v2 single-replica model.
        self._conn = sqlite3.connect(conn_target, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    def initialize(self) -> None:
        self._conn.executescript(SCHEMA_SQL_V2)
        version = self.schema_version()
        if version != SCHEMA_VERSION_V2:
            raise RuntimeError(
                f"state-v2 schema version {version} does not match "
                f"{SCHEMA_VERSION_V2}"
            )
        self._conn.commit()

    def schema_version(self) -> int:
        row = self._conn.execute("PRAGMA user_version").fetchone()
        return int(row[0])

    def upsert_user(
        self,
        *,
        user_id: str,
        email: str,
        name: str | None = None,
        is_admin: bool = False,
    ) -> UserRecord:
        user_id = _validate_user_id(user_id)
        self._conn.execute(
            """
            INSERT INTO users(user_id, email, name, is_admin)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                email = excluded.email,
                name = excluded.name,
                is_admin = excluded.is_admin,
                last_seen_at = CURRENT_TIMESTAMP
            """,
            (user_id, email, name, int(is_admin)),
        )
        self._conn.commit()
        return UserRecord(user_id=user_id, email=email, name=name, is_admin=is_admin)

    def admin_count(self) -> int:
        cursor = self._conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE is_admin = 1"
        )
        return int(cursor.fetchone()["c"])

    def promote_to_admin(self, user_id: str) -> None:
        _validate_user_id(user_id)
        self._conn.execute(
            "UPDATE users SET is_admin = 1 WHERE user_id = ?", (user_id,)
        )
        self._conn.commit()

    def get_setting(self, key: str) -> object | None:
        cursor = self._conn.execute(
            "SELECT value_json FROM service_settings WHERE key = ?", (key,)
        )
        row = cursor.fetchone()
        if row is None:
            return None
        import json as _json

        return _json.loads(row["value_json"])

    def set_setting(self, key: str, value: object) -> None:
        import json as _json

        self._conn.execute(
            """
            INSERT INTO service_settings(key, value_json)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (key, _json.dumps(value, sort_keys=True)),
        )
        self._conn.commit()

    def active_model_sha256(self) -> str | None:
        cursor = self._conn.execute(
            "SELECT sha256 FROM model_registry WHERE active = 1 LIMIT 1"
        )
        row = cursor.fetchone()
        return None if row is None else str(row["sha256"])

    def list_users(self) -> list[UserRecord]:
        cursor = self._conn.execute(
            "SELECT user_id, email, name, is_admin FROM users ORDER BY user_id"
        )
        return [
            UserRecord(
                user_id=row["user_id"],
                email=row["email"],
                name=row["name"],
                is_admin=bool(row["is_admin"]),
            )
            for row in cursor.fetchall()
        ]

    def with_user(self, user_id: str) -> "UserScopedState":
        """Return a tenant-scoped accessor. The returned object refuses
        to act on rows belonging to other ``user_id``s.
        """

        return UserScopedState(self._conn, _validate_user_id(user_id))

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "StateStoreV2":
        self.initialize()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


class UserScopedState:
    """Per-tenant view over a :class:`StateStoreV2` connection.

    Every method either filters by ``user_id`` on read or stamps
    ``user_id`` on write. There is no unscoped read API exposed here.
    """

    def __init__(self, conn: sqlite3.Connection, user_id: str) -> None:
        self._conn = conn
        self._user_id = user_id

    @property
    def user_id(self) -> str:
        return self._user_id

    # -- assets ------------------------------------------------------

    def upsert_asset(
        self,
        *,
        asset_id: str,
        media_type: str,
        checksum: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO assets(user_id, asset_id, media_type, immich_checksum_or_version)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, asset_id) DO UPDATE SET
                media_type = excluded.media_type,
                immich_checksum_or_version = excluded.immich_checksum_or_version
            """,
            (self._user_id, asset_id, media_type, checksum),
        )
        self._conn.commit()

    def list_assets(self) -> list[sqlite3.Row]:
        cursor = self._conn.execute(
            "SELECT * FROM assets WHERE user_id = ? ORDER BY asset_id",
            (self._user_id,),
        )
        return list(cursor.fetchall())

    # -- runs --------------------------------------------------------

    def start_run(self, *, dry_run: bool, command: str) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO runs(user_id, status, dry_run, command)
            VALUES (?, 'running', ?, ?)
            """,
            (self._user_id, int(dry_run), command),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def list_runs(self) -> list[sqlite3.Row]:
        cursor = self._conn.execute(
            "SELECT * FROM runs WHERE user_id = ? ORDER BY id",
            (self._user_id,),
        )
        return list(cursor.fetchall())

    def get_run(self, run_id: int) -> sqlite3.Row | None:
        cursor = self._conn.execute(
            "SELECT * FROM runs WHERE id = ? AND user_id = ?",
            (run_id, self._user_id),
        )
        return cursor.fetchone()

    # -- actions / errors --------------------------------------------

    def record_action(
        self,
        *,
        run_id: int,
        asset_id: str,
        action_name: str,
        dry_run: bool,
        would_apply: bool,
        success: bool | None = None,
        error_code: str | None = None,
    ) -> int:
        self._assert_owns_run(run_id)
        cursor = self._conn.execute(
            """
            INSERT INTO actions(
                user_id, run_id, asset_id, action_name,
                dry_run, would_apply, success, error_code
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._user_id,
                run_id,
                asset_id,
                action_name,
                int(dry_run),
                int(would_apply),
                None if success is None else int(success),
                error_code,
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def list_actions(self) -> list[sqlite3.Row]:
        cursor = self._conn.execute(
            "SELECT * FROM actions WHERE user_id = ? ORDER BY id",
            (self._user_id,),
        )
        return list(cursor.fetchall())

    def record_error(
        self,
        *,
        stage: str,
        message_code: str,
        run_id: int | None = None,
        asset_id: str | None = None,
        message: str | None = None,
    ) -> int:
        if run_id is not None:
            self._assert_owns_run(run_id)
        cursor = self._conn.execute(
            """
            INSERT INTO errors(user_id, run_id, asset_id, stage, message_code, message)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (self._user_id, run_id, asset_id, stage, message_code, message),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def list_errors(self) -> list[sqlite3.Row]:
        cursor = self._conn.execute(
            "SELECT * FROM errors WHERE user_id = ? ORDER BY id",
            (self._user_id,),
        )
        return list(cursor.fetchall())

    # -- config ------------------------------------------------------

    def get_config(self) -> dict:
        cursor = self._conn.execute(
            "SELECT categories_json, policies_json FROM user_config WHERE user_id = ?",
            (self._user_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return {"categories": {}, "policies": {}}
        import json as _json

        return {
            "categories": _json.loads(row["categories_json"]),
            "policies": _json.loads(row["policies_json"]),
        }

    def set_categories(self, categories: dict) -> None:
        import json as _json

        self._conn.execute(
            """
            INSERT INTO user_config(user_id, categories_json)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                categories_json = excluded.categories_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (self._user_id, _json.dumps(categories, sort_keys=True)),
        )
        self._conn.commit()

    def mark_model_seen(self, sha256: str) -> None:
        self._conn.execute(
            """
            INSERT INTO user_config(user_id, last_seen_model_sha256)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                last_seen_model_sha256 = excluded.last_seen_model_sha256,
                updated_at = CURRENT_TIMESTAMP
            """,
            (self._user_id, sha256),
        )
        self._conn.commit()

    def last_seen_model_sha256(self) -> str | None:
        cursor = self._conn.execute(
            "SELECT last_seen_model_sha256 FROM user_config WHERE user_id = ?",
            (self._user_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return None if row["last_seen_model_sha256"] is None else str(row["last_seen_model_sha256"])

    def set_policies(self, policies: dict) -> None:
        import json as _json

        self._conn.execute(
            """
            INSERT INTO user_config(user_id, policies_json)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                policies_json = excluded.policies_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (self._user_id, _json.dumps(policies, sort_keys=True)),
        )
        self._conn.commit()

    # -- scans -------------------------------------------------------

    def has_active_run(self) -> bool:
        cursor = self._conn.execute(
            "SELECT 1 FROM runs WHERE user_id = ? AND status = 'running' LIMIT 1",
            (self._user_id,),
        )
        return cursor.fetchone() is not None

    def runs_started_today(self, *, since_iso: str) -> int:
        cursor = self._conn.execute(
            "SELECT COUNT(*) AS c FROM runs WHERE user_id = ? AND started_at >= ?",
            (self._user_id, since_iso),
        )
        return int(cursor.fetchone()["c"])

    def finish_run(self, run_id: int, *, status: str, summary_json: str | None = None) -> None:
        self._assert_owns_run(run_id)
        self._conn.execute(
            "UPDATE runs SET status = ?, ended_at = CURRENT_TIMESTAMP, summary_json = ? "
            "WHERE id = ? AND user_id = ?",
            (status, summary_json, run_id, self._user_id),
        )
        self._conn.commit()

    def revert_run_actions(self, run_id: int) -> int:
        self._assert_owns_run(run_id)
        cursor = self._conn.execute(
            "UPDATE actions SET success = 0, error_code = 'reverted' "
            "WHERE user_id = ? AND run_id = ? AND success = 1",
            (self._user_id, run_id),
        )
        self._conn.commit()
        return cursor.rowcount

    # -- audit -------------------------------------------------------

    def write_audit(
        self,
        *,
        action: str,
        target_asset_id: str | None = None,
        run_id: int | None = None,
        before_state: str | None = None,
        after_state: str | None = None,
        details_json: str | None = None,
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO audit_log(
                user_id, action, target_asset_id, run_id,
                before_state, after_state, details_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._user_id,
                action,
                target_asset_id,
                run_id,
                before_state,
                after_state,
                details_json,
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def list_audit(self) -> list[sqlite3.Row]:
        cursor = self._conn.execute(
            "SELECT * FROM audit_log WHERE user_id = ? ORDER BY id",
            (self._user_id,),
        )
        return list(cursor.fetchall())

    # -- sessions ----------------------------------------------------

    def create_session(
        self,
        *,
        session_id: str,
        encrypted_immich_token: bytes,
        expires_at: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO sessions(session_id, user_id, encrypted_immich_token, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, self._user_id, encrypted_immich_token, expires_at),
        )
        self._conn.commit()

    def list_sessions(self) -> list[sqlite3.Row]:
        cursor = self._conn.execute(
            "SELECT * FROM sessions WHERE user_id = ? ORDER BY created_at",
            (self._user_id,),
        )
        return list(cursor.fetchall())

    # -- api keys ----------------------------------------------------

    def store_api_key(
        self,
        *,
        encrypted_key: bytes,
        label: str | None = None,
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO user_api_keys(user_id, label, encrypted_key)
            VALUES (?, ?, ?)
            """,
            (self._user_id, label, encrypted_key),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def list_api_keys(self) -> list[sqlite3.Row]:
        cursor = self._conn.execute(
            "SELECT * FROM user_api_keys WHERE user_id = ? ORDER BY id",
            (self._user_id,),
        )
        return list(cursor.fetchall())

    # -- account purge -----------------------------------------------

    def purge(self) -> None:
        """Idempotent purge of every row that belongs to this tenant.

        - All user-scoped tables (sessions, user_api_keys, actions,
          errors, assets, runs, user_config) lose their rows.
        - ``sessions.encrypted_immich_token`` and
          ``user_api_keys.encrypted_key`` blobs are zeroed in place
          before the row is deleted, so a recovered DB page does not
          yield a decryptable ciphertext.
        - ``audit_log`` rows are anonymized in place by rewriting
          ``user_id`` to a sentinel ``"user_deleted"`` (the threat
          model accepts either delete-or-anonymize; anonymize-in-place
          preserves the audit trail).
        - The ``users`` row is then deleted.
        """

        conn = self._conn
        # Zero the encrypted blobs first, then delete the rows.
        conn.execute(
            "UPDATE sessions SET encrypted_immich_token = zeroblob(length(encrypted_immich_token)) "
            "WHERE user_id = ?",
            (self._user_id,),
        )
        conn.execute(
            "UPDATE user_api_keys SET encrypted_key = zeroblob(length(encrypted_key)) "
            "WHERE user_id = ?",
            (self._user_id,),
        )
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (self._user_id,))
        conn.execute("DELETE FROM user_api_keys WHERE user_id = ?", (self._user_id,))
        conn.execute("DELETE FROM actions WHERE user_id = ?", (self._user_id,))
        conn.execute("DELETE FROM errors WHERE user_id = ?", (self._user_id,))
        conn.execute("DELETE FROM assets WHERE user_id = ?", (self._user_id,))
        conn.execute("DELETE FROM runs WHERE user_id = ?", (self._user_id,))
        conn.execute("DELETE FROM user_config WHERE user_id = ?", (self._user_id,))

        # Ensure the anonymization sentinel user exists so the audit_log
        # FK stays satisfied after rewrite.
        conn.execute(
            "INSERT OR IGNORE INTO users(user_id, email, name, is_admin) "
            "VALUES ('user_deleted', '', NULL, 0)"
        )
        conn.execute(
            "UPDATE audit_log SET user_id = 'user_deleted' WHERE user_id = ?",
            (self._user_id,),
        )
        conn.execute("DELETE FROM users WHERE user_id = ?", (self._user_id,))
        conn.commit()

    # -- internals ---------------------------------------------------

    def _assert_owns_run(self, run_id: int) -> None:
        cursor = self._conn.execute(
            "SELECT user_id FROM runs WHERE id = ?", (run_id,)
        )
        row = cursor.fetchone()
        if row is None:
            raise LookupError(f"run {run_id} not found")
        if row["user_id"] != self._user_id:
            raise PermissionError(
                f"run {run_id} does not belong to user {self._user_id}"
            )


__all__ = [
    "SCHEMA_SQL_V2",
    "SCHEMA_VERSION_V2",
    "StateStoreV2",
    "UserRecord",
    "UserScopedState",
]


# Silence unused-import linters in environments that strip type-only imports.
_ = (Any, Mapping)
