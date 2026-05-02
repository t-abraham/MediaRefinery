from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from typing import Any
import json
import re
import sqlite3

from .classifier import ClassificationResult
from .decision import ActionPlan
from .immich import AssetRef


SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS config_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hash TEXT NOT NULL UNIQUE,
    source_name TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS model_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    backend TEXT NOT NULL,
    profile_name TEXT NOT NULL,
    version TEXT NOT NULL,
    model_identity_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(backend, profile_name, version, model_identity_hash)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TEXT,
    status TEXT NOT NULL,
    dry_run INTEGER NOT NULL,
    command TEXT,
    config_snapshot_id INTEGER,
    model_version_id INTEGER,
    summary_json TEXT,
    FOREIGN KEY(config_snapshot_id) REFERENCES config_snapshots(id),
    FOREIGN KEY(model_version_id) REFERENCES model_versions(id)
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id TEXT PRIMARY KEY,
    media_type TEXT NOT NULL,
    immich_checksum_or_version TEXT,
    first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_processed TEXT
);

CREATE TABLE IF NOT EXISTS classification_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    asset_id TEXT NOT NULL,
    config_snapshot_id INTEGER,
    model_version_id INTEGER,
    category_id TEXT NOT NULL,
    raw_scores_json TEXT NOT NULL,
    processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(run_id) REFERENCES runs(id),
    FOREIGN KEY(asset_id) REFERENCES assets(asset_id),
    FOREIGN KEY(config_snapshot_id) REFERENCES config_snapshots(id),
    FOREIGN KEY(model_version_id) REFERENCES model_versions(id)
);

CREATE TABLE IF NOT EXISTS action_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    asset_id TEXT NOT NULL,
    action_name TEXT NOT NULL,
    dry_run INTEGER NOT NULL,
    would_apply INTEGER NOT NULL,
    success INTEGER,
    error_code TEXT,
    ran_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(run_id) REFERENCES runs(id),
    FOREIGN KEY(asset_id) REFERENCES assets(asset_id)
);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    asset_id TEXT,
    stage TEXT NOT NULL,
    message_code TEXT NOT NULL,
    message TEXT,
    details_json TEXT,
    at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(run_id) REFERENCES runs(id),
    FOREIGN KEY(asset_id) REFERENCES assets(asset_id)
);

CREATE INDEX IF NOT EXISTS idx_classification_runs_asset_processed
    ON classification_runs(asset_id, processed_at);

CREATE INDEX IF NOT EXISTS idx_action_runs_asset_ran_at
    ON action_runs(asset_id, ran_at);

CREATE INDEX IF NOT EXISTS idx_errors_asset_at
    ON errors(asset_id, at);

PRAGMA user_version = 1;
"""

SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|authorization|bearer|password|secret|token)", re.IGNORECASE
)
SECRET_ASSIGNMENT_RE = re.compile(
    r"\b(api[_-]?key|authorization|bearer|password|secret|token)\b"
    r"\s*[:=]\s*['\"]?[^'\"\s,;]+",
    re.IGNORECASE,
)
WINDOWS_USER_PATH_RE = re.compile(
    r"[A-Za-z]:\\Users\\[^\\\s,;]+(?:\\[^\s,;]+)*"
)
POSIX_USER_PATH_RE = re.compile(r"(?:/home|/Users)/[^/\s,;]+(?:/[^\s,;]+)*")
DATA_URI_RE = re.compile(
    r"data:(?:image|video)/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=]+",
    re.IGNORECASE,
)
LONG_BASE64_RE = re.compile(r"[A-Za-z0-9+/]{80,}={0,2}")
MEDIA_DETAIL_KEY_RE = re.compile(
    r"(blob|bytes|extracted[_-]?frame|frame|image|media|preview|thumbnail|video)",
    re.IGNORECASE,
)
SAFE_CODE_RE = re.compile(r"[^A-Za-z0-9_.:-]+")


@dataclass(frozen=True)
class ActionReportCount:
    action_name: str
    total: int
    dry_run: int
    would_apply: int
    succeeded: int
    failed: int
    error_code: str | None = None


@dataclass(frozen=True)
class ErrorReportCount:
    stage: str
    message_code: str
    total: int
    affected_assets: int


@dataclass(frozen=True)
class RunReport:
    run_id: int
    command: str | None
    status: str
    started_at: str
    ended_at: str | None
    dry_run: bool
    config_source_name: str | None
    config_hash: str | None
    model_backend: str | None
    model_profile_name: str | None
    model_version: str | None
    processed: int
    skipped: int
    errors: int
    by_category: dict[str, int]
    action_counts: tuple[ActionReportCount, ...]
    error_counts: tuple[ErrorReportCount, ...]

    @property
    def mode(self) -> str:
        return "dry-run" if self.dry_run else "live"

    @property
    def partial_failure(self) -> bool:
        return self.errors > 0 or self.status == "completed_with_errors"


class StateStore:
    def __init__(self, sqlite_path: str | Path):
        self.path = Path(sqlite_path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    def initialize(self) -> None:
        existing_version = self.schema_version()
        if existing_version > SCHEMA_VERSION:
            raise RuntimeError(
                "state schema version "
                f"{existing_version} is newer than supported version "
                f"{SCHEMA_VERSION}; restore a compatible state backup or run "
                "a newer MediaRefinery release"
            )

        self._conn.executescript(SCHEMA_SQL)
        self._ensure_v1_columns()
        self.check_schema_version()
        self._conn.commit()

    def schema_version(self) -> int:
        cursor = self._conn.execute("PRAGMA user_version")
        row = cursor.fetchone()
        return int(row[0])

    def check_schema_version(self) -> int:
        version = self.schema_version()
        if version != SCHEMA_VERSION:
            raise RuntimeError(
                f"state schema version {version} does not match {SCHEMA_VERSION}"
            )
        return version

    def start_run(
        self,
        dry_run: bool,
        command: str,
        *,
        config_snapshot_id: int | None = None,
        model_version_id: int | None = None,
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO runs(
                status,
                dry_run,
                command,
                config_snapshot_id,
                model_version_id
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "running",
                int(dry_run),
                command,
                config_snapshot_id,
                model_version_id,
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, summary: dict[str, Any]) -> None:
        self._conn.execute(
            """
            UPDATE runs
            SET status = ?, ended_at = CURRENT_TIMESTAMP, summary_json = ?
            WHERE id = ?
            """,
            (status, json.dumps(summary, sort_keys=True), run_id),
        )
        self._conn.commit()

    def record_config_snapshot(
        self,
        config_data: Mapping[str, Any] | None = None,
        *,
        config_hash: str | None = None,
        source: str | Path | None = None,
    ) -> int:
        if config_hash is None:
            if config_data is None:
                raise ValueError("config_data or config_hash is required")
            config_hash = stable_hash(config_data)

        source_name = _safe_source_name(source)
        self._conn.execute(
            """
            INSERT OR IGNORE INTO config_snapshots(hash, source_name)
            VALUES (?, ?)
            """,
            (config_hash, source_name),
        )
        cursor = self._conn.execute(
            "SELECT id FROM config_snapshots WHERE hash = ?", (config_hash,)
        )
        row = cursor.fetchone()
        self._conn.commit()
        if row is None:  # pragma: no cover - guarded by insert/select invariant
            raise RuntimeError("config snapshot was not persisted")
        return int(row["id"])

    def record_model_version(
        self,
        *,
        backend: str,
        profile_name: str,
        version: str | None = None,
        model_path: str | Path | None = None,
        model_identity_hash: str | None = None,
    ) -> int:
        backend = _require_text("backend", backend)
        profile_name = _require_text("profile_name", profile_name)
        version = _require_text("version", version or "unversioned")
        if model_identity_hash is None:
            model_identity_hash = stable_hash(
                {
                    "backend": backend,
                    "profile_name": profile_name,
                    "version": version,
                    "model_path": str(model_path) if model_path is not None else None,
                }
            )

        self._conn.execute(
            """
            INSERT OR IGNORE INTO model_versions(
                backend,
                profile_name,
                version,
                model_identity_hash
            )
            VALUES (?, ?, ?, ?)
            """,
            (backend, profile_name, version, model_identity_hash),
        )
        cursor = self._conn.execute(
            """
            SELECT id FROM model_versions
            WHERE backend = ?
              AND profile_name = ?
              AND version = ?
              AND model_identity_hash = ?
            """,
            (backend, profile_name, version, model_identity_hash),
        )
        row = cursor.fetchone()
        self._conn.commit()
        if row is None:  # pragma: no cover - guarded by insert/select invariant
            raise RuntimeError("model version was not persisted")
        return int(row["id"])

    def upsert_asset(self, asset: AssetRef) -> None:
        self._upsert_asset(asset)
        self._conn.commit()

    def record_classification_run(
        self,
        run_id: int,
        asset: AssetRef,
        result: ClassificationResult,
        *,
        config_snapshot_id: int | None = None,
        model_version_id: int | None = None,
    ) -> int:
        classification_id = self._record_classification_run(
            run_id,
            asset,
            result,
            config_snapshot_id=config_snapshot_id,
            model_version_id=model_version_id,
        )
        self._conn.commit()
        return classification_id

    def record_action_run(
        self,
        run_id: int,
        asset_id: str,
        action_name: str,
        *,
        dry_run: bool,
        would_apply: bool,
        success: bool | None = None,
        error_code: str | None = None,
    ) -> int:
        action_id = self._record_action_run(
            run_id,
            asset_id,
            action_name,
            dry_run=dry_run,
            would_apply=would_apply,
            success=success,
            error_code=error_code,
        )
        self._conn.commit()
        return action_id

    def record_classification(
        self,
        run_id: int,
        asset: AssetRef,
        result: ClassificationResult,
        action_plan: ActionPlan,
        *,
        config_snapshot_id: int | None = None,
        model_version_id: int | None = None,
    ) -> int:
        classification_id = self._record_classification_run(
            run_id,
            asset,
            result,
            config_snapshot_id=config_snapshot_id,
            model_version_id=model_version_id,
        )
        for planned_action in action_plan.intended_actions:
            self._record_action_run(
                run_id,
                asset.asset_id,
                planned_action.name,
                dry_run=action_plan.dry_run,
                would_apply=planned_action.would_apply,
                success=True,
                error_code=action_plan.error_code,
            )
        self._conn.commit()
        return classification_id

    def record_error(
        self,
        *,
        stage: str,
        message_code: str,
        run_id: int | None = None,
        asset_id: str | None = None,
        message: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> int:
        details_json = None
        if details is not None:
            details_json = json.dumps(
                _safe_error_value(details),
                sort_keys=True,
                separators=(",", ":"),
            )

        cursor = self._conn.execute(
            """
            INSERT INTO errors(
                run_id,
                asset_id,
                stage,
                message_code,
                message,
                details_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                asset_id,
                _safe_code(stage),
                _safe_code(message_code),
                _safe_error_text(message) if message is not None else None,
                details_json,
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def needs_processing(self, asset: AssetRef, *, reprocess: bool = False) -> bool:
        if reprocess:
            return True

        stored_asset = self.get_asset(asset.asset_id)
        if stored_asset is None:
            return True
        if stored_asset["last_processed"] is None:
            return True

        stored_checksum = stored_asset["immich_checksum_or_version"]
        if asset.checksum is None:
            return False
        if stored_checksum is None:
            return True
        return stored_checksum != asset.checksum

    def get_asset(self, asset_id: str) -> sqlite3.Row | None:
        cursor = self._conn.execute(
            "SELECT * FROM assets WHERE asset_id = ?", (asset_id,)
        )
        return cursor.fetchone()

    def list_assets(self) -> list[sqlite3.Row]:
        cursor = self._conn.execute("SELECT * FROM assets ORDER BY asset_id")
        return list(cursor.fetchall())

    def get_run(self, run_id: int) -> sqlite3.Row | None:
        cursor = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
        return cursor.fetchone()

    def latest_run_id(self) -> int | None:
        cursor = self._conn.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        if row is None:
            return None
        return int(row["id"])

    def get_run_report(self, run_id: int | None = None) -> RunReport | None:
        selected_run_id = self.latest_run_id() if run_id is None else run_id
        if selected_run_id is None:
            return None

        cursor = self._conn.execute(
            """
            SELECT
                runs.id,
                runs.started_at,
                runs.ended_at,
                runs.status,
                runs.dry_run,
                runs.command,
                runs.summary_json,
                config_snapshots.hash AS config_hash,
                config_snapshots.source_name AS config_source_name,
                model_versions.backend AS model_backend,
                model_versions.profile_name AS model_profile_name,
                model_versions.version AS model_version
            FROM runs
            LEFT JOIN config_snapshots
                ON config_snapshots.id = runs.config_snapshot_id
            LEFT JOIN model_versions
                ON model_versions.id = runs.model_version_id
            WHERE runs.id = ?
            """,
            (selected_run_id,),
        )
        run_row = cursor.fetchone()
        if run_row is None:
            return None

        summary = _summary_json(run_row["summary_json"])
        category_counts = self._report_category_counts(selected_run_id)
        action_counts = self._report_action_counts(selected_run_id)
        if not action_counts:
            action_counts = _summary_action_counts(
                summary,
                dry_run=bool(run_row["dry_run"]),
            )
        error_counts = self._report_error_counts(selected_run_id)
        error_total = sum(error_count.total for error_count in error_counts)

        return RunReport(
            run_id=int(run_row["id"]),
            command=run_row["command"],
            status=run_row["status"],
            started_at=run_row["started_at"],
            ended_at=run_row["ended_at"],
            dry_run=bool(run_row["dry_run"]),
            config_source_name=run_row["config_source_name"],
            config_hash=run_row["config_hash"],
            model_backend=run_row["model_backend"],
            model_profile_name=run_row["model_profile_name"],
            model_version=run_row["model_version"],
            processed=_summary_int(
                summary,
                "processed",
                fallback=self._report_processed_count(selected_run_id),
            ),
            skipped=_summary_int(summary, "skipped", fallback=0),
            errors=_summary_int(summary, "errors", fallback=error_total),
            by_category=_summary_counts(
                summary,
                "by_category",
                fallback=category_counts,
            ),
            action_counts=action_counts,
            error_counts=error_counts,
        )

    def get_config_snapshot(self, snapshot_id: int) -> sqlite3.Row | None:
        cursor = self._conn.execute(
            "SELECT * FROM config_snapshots WHERE id = ?", (snapshot_id,)
        )
        return cursor.fetchone()

    def get_config_snapshot_by_hash(self, config_hash: str) -> sqlite3.Row | None:
        cursor = self._conn.execute(
            "SELECT * FROM config_snapshots WHERE hash = ?", (config_hash,)
        )
        return cursor.fetchone()

    def list_config_snapshots(self) -> list[sqlite3.Row]:
        cursor = self._conn.execute("SELECT * FROM config_snapshots ORDER BY id")
        return list(cursor.fetchall())

    def get_model_version(self, model_version_id: int) -> sqlite3.Row | None:
        cursor = self._conn.execute(
            "SELECT * FROM model_versions WHERE id = ?", (model_version_id,)
        )
        return cursor.fetchone()

    def list_model_versions(self) -> list[sqlite3.Row]:
        cursor = self._conn.execute("SELECT * FROM model_versions ORDER BY id")
        return list(cursor.fetchall())

    def get_classification_run(self, classification_id: int) -> sqlite3.Row | None:
        cursor = self._conn.execute(
            "SELECT * FROM classification_runs WHERE id = ?", (classification_id,)
        )
        return cursor.fetchone()

    def list_classifications(
        self,
        *,
        asset_id: str | None = None,
    ) -> list[sqlite3.Row]:
        if asset_id is None:
            cursor = self._conn.execute(
                "SELECT * FROM classification_runs ORDER BY id"
            )
        else:
            cursor = self._conn.execute(
                """
                SELECT * FROM classification_runs
                WHERE asset_id = ?
                ORDER BY id
                """,
                (asset_id,),
            )
        return list(cursor.fetchall())

    def get_action_run(self, action_id: int) -> sqlite3.Row | None:
        cursor = self._conn.execute(
            "SELECT * FROM action_runs WHERE id = ?", (action_id,)
        )
        return cursor.fetchone()

    def list_action_runs(
        self,
        *,
        asset_id: str | None = None,
    ) -> list[sqlite3.Row]:
        if asset_id is None:
            cursor = self._conn.execute("SELECT * FROM action_runs ORDER BY id")
        else:
            cursor = self._conn.execute(
                """
                SELECT * FROM action_runs
                WHERE asset_id = ?
                ORDER BY id
                """,
                (asset_id,),
            )
        return list(cursor.fetchall())

    def get_error(self, error_id: int) -> sqlite3.Row | None:
        cursor = self._conn.execute("SELECT * FROM errors WHERE id = ?", (error_id,))
        return cursor.fetchone()

    def list_errors(
        self,
        *,
        asset_id: str | None = None,
    ) -> list[sqlite3.Row]:
        if asset_id is None:
            cursor = self._conn.execute("SELECT * FROM errors ORDER BY id")
        else:
            cursor = self._conn.execute(
                "SELECT * FROM errors WHERE asset_id = ? ORDER BY id",
                (asset_id,),
            )
        return list(cursor.fetchall())

    def table_names(self) -> list[str]:
        cursor = self._conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
        return [str(row["name"]) for row in cursor.fetchall()]

    def column_types(self, table_name: str) -> dict[str, str]:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name):
            raise ValueError("table_name must be a SQLite identifier")
        cursor = self._conn.execute(f"PRAGMA table_info({table_name})")
        return {row["name"]: row["type"].upper() for row in cursor.fetchall()}

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "StateStore":
        self.initialize()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _ensure_v1_columns(self) -> None:
        self._ensure_column("runs", "config_snapshot_id", "INTEGER")
        self._ensure_column("runs", "model_version_id", "INTEGER")
        self._ensure_column("classification_runs", "config_snapshot_id", "INTEGER")
        self._ensure_column("classification_runs", "model_version_id", "INTEGER")

    def _ensure_column(self, table_name: str, column_name: str, definition: str) -> None:
        if column_name in self.column_types(table_name):
            return
        self._conn.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
        )

    def _upsert_asset(self, asset: AssetRef) -> None:
        self._conn.execute(
            """
            INSERT INTO assets(asset_id, media_type, immich_checksum_or_version)
            VALUES (?, ?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
                media_type = excluded.media_type,
                immich_checksum_or_version = excluded.immich_checksum_or_version
            """,
            (asset.asset_id, asset.media_type, asset.checksum),
        )

    def _mark_asset_processed(self, asset_id: str) -> None:
        self._conn.execute(
            """
            UPDATE assets
            SET last_processed = CURRENT_TIMESTAMP
            WHERE asset_id = ?
            """,
            (asset_id,),
        )

    def _record_classification_run(
        self,
        run_id: int,
        asset: AssetRef,
        result: ClassificationResult,
        *,
        config_snapshot_id: int | None,
        model_version_id: int | None,
    ) -> int:
        if result.asset_id != asset.asset_id:
            raise ValueError("classification result asset_id must match asset")

        self._upsert_asset(asset)
        cursor = self._conn.execute(
            """
            INSERT INTO classification_runs(
                run_id,
                asset_id,
                config_snapshot_id,
                model_version_id,
                category_id,
                raw_scores_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                asset.asset_id,
                config_snapshot_id,
                model_version_id,
                result.category_id,
                json.dumps(result.raw_scores, sort_keys=True),
            ),
        )
        self._mark_asset_processed(asset.asset_id)
        return int(cursor.lastrowid)

    def _record_action_run(
        self,
        run_id: int,
        asset_id: str,
        action_name: str,
        *,
        dry_run: bool,
        would_apply: bool,
        success: bool | None,
        error_code: str | None = None,
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO action_runs(
                run_id,
                asset_id,
                action_name,
                dry_run,
                would_apply,
                success,
                error_code
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                asset_id,
                action_name,
                int(dry_run),
                int(would_apply),
                None if success is None else int(success),
                error_code,
            ),
        )
        return int(cursor.lastrowid)

    def _report_processed_count(self, run_id: int) -> int:
        cursor = self._conn.execute(
            "SELECT COUNT(*) AS count FROM classification_runs WHERE run_id = ?",
            (run_id,),
        )
        return int(cursor.fetchone()["count"])

    def _report_category_counts(self, run_id: int) -> dict[str, int]:
        cursor = self._conn.execute(
            """
            SELECT category_id, COUNT(*) AS count
            FROM classification_runs
            WHERE run_id = ?
            GROUP BY category_id
            ORDER BY category_id
            """,
            (run_id,),
        )
        return {str(row["category_id"]): int(row["count"]) for row in cursor}

    def _report_action_counts(self, run_id: int) -> tuple[ActionReportCount, ...]:
        cursor = self._conn.execute(
            """
            SELECT
                action_name,
                error_code,
                COUNT(*) AS total,
                SUM(CASE WHEN dry_run = 1 THEN 1 ELSE 0 END) AS dry_run_count,
                SUM(CASE WHEN would_apply = 1 THEN 1 ELSE 0 END) AS would_apply_count,
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS succeeded_count,
                SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failed_count
            FROM action_runs
            WHERE run_id = ?
            GROUP BY action_name, error_code
            ORDER BY action_name, error_code
            """,
            (run_id,),
        )
        return tuple(
            ActionReportCount(
                action_name=str(row["action_name"]),
                error_code=row["error_code"],
                total=int(row["total"]),
                dry_run=int(row["dry_run_count"] or 0),
                would_apply=int(row["would_apply_count"] or 0),
                succeeded=int(row["succeeded_count"] or 0),
                failed=int(row["failed_count"] or 0),
            )
            for row in cursor
        )

    def _report_error_counts(self, run_id: int) -> tuple[ErrorReportCount, ...]:
        cursor = self._conn.execute(
            """
            SELECT
                stage,
                message_code,
                COUNT(*) AS total,
                COUNT(DISTINCT asset_id) AS affected_assets
            FROM errors
            WHERE run_id = ?
            GROUP BY stage, message_code
            ORDER BY stage, message_code
            """,
            (run_id,),
        )
        return tuple(
            ErrorReportCount(
                stage=str(row["stage"]),
                message_code=str(row["message_code"]),
                total=int(row["total"]),
                affected_assets=int(row["affected_assets"] or 0),
            )
            for row in cursor
        )


def stable_hash(value: Any) -> str:
    payload = json.dumps(
        _json_safe(value),
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _summary_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        summary = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(summary, dict):
        return {}
    return summary


def _summary_int(
    summary: Mapping[str, Any],
    key: str,
    *,
    fallback: int,
) -> int:
    value = summary.get(key)
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    return fallback


def _summary_counts(
    summary: Mapping[str, Any],
    key: str,
    *,
    fallback: dict[str, int],
) -> dict[str, int]:
    value = summary.get(key)
    if not isinstance(value, Mapping):
        return dict(fallback)

    counts: dict[str, int] = {}
    for name, count in value.items():
        if isinstance(count, bool) or not isinstance(count, int):
            return dict(fallback)
        counts[str(name)] = count
    return counts


def _summary_action_counts(
    summary: Mapping[str, Any],
    *,
    dry_run: bool,
) -> tuple[ActionReportCount, ...]:
    intended_actions = _summary_counts(summary, "intended_actions", fallback={})
    return tuple(
        ActionReportCount(
            action_name=action_name,
            total=count,
            dry_run=count if dry_run else 0,
            would_apply=0,
            succeeded=0,
            failed=0,
        )
        for action_name, count in sorted(intended_actions.items())
    )


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    return str(value)


def _safe_error_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, bytes):
        return "<redacted-bytes>"
    if isinstance(value, str):
        return _safe_error_text(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return _safe_error_text(str(value))
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if SECRET_KEY_RE.search(key_text) or MEDIA_DETAIL_KEY_RE.search(key_text):
                safe["redacted"] = "<redacted>"
                continue
            safe[_safe_code(key_text)] = _safe_error_value(item)
        return safe
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_error_value(item) for item in value]
    return _safe_error_text(str(value))


def _safe_error_text(value: str) -> str:
    text = DATA_URI_RE.sub("<redacted-media-data>", value)
    text = SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}=<redacted>",
        text,
    )
    text = WINDOWS_USER_PATH_RE.sub("<user-home-path>", text)
    text = POSIX_USER_PATH_RE.sub("<user-home-path>", text)
    text = LONG_BASE64_RE.sub("<redacted-data>", text)
    if len(text) > 500:
        return text[:497] + "..."
    return text


def _safe_code(value: str) -> str:
    code = SAFE_CODE_RE.sub("_", str(value).strip())[:80].strip("_")
    return code or "unknown"


def _safe_source_name(source: str | Path | None) -> str | None:
    if source is None:
        return None
    source_text = str(source)
    if "\\" in source_text:
        name = PureWindowsPath(source_text).name
    else:
        name = Path(source).name
    return _safe_error_text(name) if name else None


def _require_text(field_name: str, value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value
