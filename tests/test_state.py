from __future__ import annotations

import json
import sqlite3

import pytest

from mediarefinery.classifier import ClassificationResult
from mediarefinery.decision import ActionPlan
from mediarefinery.immich import AssetRef
from mediarefinery.state import SCHEMA_VERSION, StateStore


SPRINT_004_TABLES = {
    "action_runs",
    "assets",
    "classification_runs",
    "config_snapshots",
    "errors",
    "model_versions",
    "runs",
}


def test_state_store_creates_schema_v1_tables_deterministically(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"

    with StateStore(path) as store:
        assert store.schema_version() == SCHEMA_VERSION
        assert set(store.table_names()) == SPRINT_004_TABLES
        first_columns = {
            table_name: store.column_types(table_name)
            for table_name in store.table_names()
        }

    with StateStore(path) as store:
        second_columns = {
            table_name: store.column_types(table_name)
            for table_name in store.table_names()
        }

    assert second_columns == first_columns


def test_state_store_rejects_newer_schema_versions(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version = 2")

    store = StateStore(path)
    try:
        with pytest.raises(RuntimeError) as exc_info:
            store.initialize()
    finally:
        store.close()

    message = str(exc_info.value)
    assert "state schema version 2 is newer than supported version 1" in message
    assert "compatible state backup" in message


def test_state_store_backfills_known_v1_beta_columns(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                ended_at TEXT,
                status TEXT NOT NULL,
                dry_run INTEGER NOT NULL,
                command TEXT,
                summary_json TEXT
            );
            CREATE TABLE classification_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                asset_id TEXT NOT NULL,
                category_id TEXT NOT NULL,
                raw_scores_json TEXT NOT NULL,
                processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            PRAGMA user_version = 1;
            """
        )

    with StateStore(path) as store:
        assert store.schema_version() == SCHEMA_VERSION
        assert store.column_types("runs")["config_snapshot_id"] == "INTEGER"
        assert store.column_types("runs")["model_version_id"] == "INTEGER"
        assert (
            store.column_types("classification_runs")["config_snapshot_id"]
            == "INTEGER"
        )
        assert (
            store.column_types("classification_runs")["model_version_id"]
            == "INTEGER"
        )


def test_state_store_records_and_reads_sprint_004_metadata(tmp_path) -> None:
    asset = AssetRef("asset-1", "image", checksum="sha256:test")
    result = ClassificationResult("asset-1", "custom_review", {"raw": 1.0})

    with StateStore(tmp_path / "state.sqlite3") as store:
        config_id = store.record_config_snapshot(
            {"version": 1, "categories": [{"id": "custom_review"}]},
            source=r"C:\Users\Alice\private\config.yml",
        )
        repeated_config_id = store.record_config_snapshot(
            {"version": 1, "categories": [{"id": "custom_review"}]},
            source=r"C:\Users\Alice\private\config.yml",
        )
        model_id = store.record_model_version(
            backend="noop",
            profile_name="default",
            version="noop",
            model_path=r"C:\Users\Alice\models\noop.onnx",
        )
        repeated_model_id = store.record_model_version(
            backend="noop",
            profile_name="default",
            version="noop",
            model_path=r"C:\Users\Alice\models\noop.onnx",
        )
        run_id = store.start_run(
            dry_run=True,
            command="scan",
            config_snapshot_id=config_id,
            model_version_id=model_id,
        )
        classification_id = store.record_classification_run(
            run_id,
            asset,
            result,
            config_snapshot_id=config_id,
            model_version_id=model_id,
        )
        action_id = store.record_action_run(
            run_id,
            asset.asset_id,
            "add_to_review_album",
            dry_run=True,
            would_apply=True,
            success=True,
        )
        error_id = store.record_error(
            run_id=run_id,
            asset_id=asset.asset_id,
            stage="classifier",
            message_code="mock_warning",
            message="mock warning only",
            details={"retryable": False},
        )
        store.finish_run(run_id, "succeeded", {"processed": 1})

        stored_asset = store.get_asset("asset-1")
        stored_run = store.get_run(run_id)
        stored_config = store.get_config_snapshot(config_id)
        stored_model = store.get_model_version(model_id)
        stored_classification = store.get_classification_run(classification_id)
        stored_action = store.get_action_run(action_id)
        stored_error = store.get_error(error_id)

        assert repeated_config_id == config_id
        assert repeated_model_id == model_id
        assert stored_asset is not None
        assert stored_asset["media_type"] == "image"
        assert stored_asset["last_processed"] is not None
        assert stored_run is not None
        assert stored_run["dry_run"] == 1
        assert stored_run["config_snapshot_id"] == config_id
        assert stored_run["model_version_id"] == model_id
        assert stored_config is not None
        assert len(stored_config["hash"]) == 64
        assert stored_config["source_name"] == "config.yml"
        assert stored_model is not None
        assert stored_model["backend"] == "noop"
        assert stored_model["profile_name"] == "default"
        assert len(stored_model["model_identity_hash"]) == 64
        assert stored_classification is not None
        assert stored_classification["category_id"] == "custom_review"
        assert isinstance(stored_classification["category_id"], str)
        assert json.loads(stored_classification["raw_scores_json"]) == {"raw": 1.0}
        assert stored_classification["config_snapshot_id"] == config_id
        assert stored_classification["model_version_id"] == model_id
        assert stored_action is not None
        assert stored_action["would_apply"] == 1
        assert stored_error is not None
        assert stored_error["stage"] == "classifier"
        assert store.list_classifications(asset_id=asset.asset_id)[0]["id"] == (
            classification_id
        )
        assert store.list_action_runs(asset_id=asset.asset_id)[0]["id"] == action_id
        assert store.list_errors(asset_id=asset.asset_id)[0]["id"] == error_id


def test_state_record_classification_keeps_existing_action_hook(tmp_path) -> None:
    asset = AssetRef("asset-1", "image", checksum="sha256:test")
    result = ClassificationResult("asset-1", "review_me", {"raw": 1.0})
    plan = ActionPlan(
        "review_me",
        "image",
        ("add_to_review_album", "manual_review", "no_action"),
        True,
        asset_id="asset-1",
        error_code="missing_policy",
    )

    with StateStore(tmp_path / "state.sqlite3") as store:
        run_id = store.start_run(dry_run=True, command="scan")
        store.record_classification(run_id, asset, result, plan)

        assert store.list_classifications()[0]["category_id"] == "review_me"
        action_rows = store.list_action_runs()
        assert [row["action_name"] for row in action_rows] == [
            "add_to_review_album",
            "manual_review",
            "no_action",
        ]
        assert [row["would_apply"] for row in action_rows] == [1, 0, 0]
        assert {row["dry_run"] for row in action_rows} == {1}
        assert {row["error_code"] for row in action_rows} == {"missing_policy"}


def test_state_schema_has_no_blob_or_media_columns(tmp_path) -> None:
    with StateStore(tmp_path / "state.sqlite3") as store:
        for table in SPRINT_004_TABLES:
            column_types = store.column_types(table)
            column_names = set(column_types)
            assert "BLOB" not in set(column_types.values())
            assert not {
                "blob",
                "bytes",
                "frame",
                "media_blob",
                "thumbnail",
            }.intersection(column_names)


def test_state_needs_processing_hook_respects_reprocess_and_checksum(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    asset = AssetRef("asset-1", "image", checksum="sha256:old")
    changed = AssetRef("asset-1", "image", checksum="sha256:new")
    result = ClassificationResult("asset-1", "review_me", {"raw": 1.0})
    plan = ActionPlan("review_me", "image", ("no_action",), True)

    with StateStore(path) as store:
        run_id = store.start_run(dry_run=True, command="scan")
        store.record_classification(run_id, asset, result, plan)

        assert store.needs_processing(asset, reprocess=False) is False
        assert store.needs_processing(changed, reprocess=False) is True
        assert store.needs_processing(asset, reprocess=True) is True


def test_asset_upsert_without_processing_remains_eligible(tmp_path) -> None:
    asset = AssetRef("asset-1", "image", checksum="sha256:test")

    with StateStore(tmp_path / "state.sqlite3") as store:
        store.upsert_asset(asset)

        assert store.get_asset(asset.asset_id)["last_processed"] is None
        assert store.needs_processing(asset, reprocess=False) is True


def test_error_records_redact_paths_secrets_and_media_like_values(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    asset = AssetRef("asset-1", "image", checksum="sha256:test")
    long_base64 = "A" * 100

    with StateStore(path) as store:
        store.upsert_asset(asset)
        run_id = store.start_run(dry_run=True, command="scan")
        error_id = store.record_error(
            run_id=run_id,
            asset_id=asset.asset_id,
            stage="extractor",
            message_code="failed_read",
            message=(
                r"failed C:\Users\Alice\Pictures\photo.jpg "
                f"api_key=super-secret data:image/png;base64,{long_base64}"
            ),
            details={
                "api_key": "super-secret",
                "thumbnail_bytes": b"synthetic-image-bytes",
                "path": r"C:\Users\Alice\Pictures\photo.jpg",
                "payload": long_base64,
            },
        )

        stored_error = store.get_error(error_id)
        assert stored_error is not None
        assert "<user-home-path>" in stored_error["message"]
        assert "super-secret" not in stored_error["message"]
        assert long_base64 not in stored_error["message"]
        details = json.loads(stored_error["details_json"])
        assert details["redacted"] == "<redacted>"
        assert details["path"] == "<user-home-path>"
        assert details["payload"] == "<redacted-data>"

    stored_text = _sqlite_text(path)
    assert "super-secret" not in stored_text
    assert r"C:\Users\Alice" not in stored_text
    assert long_base64 not in stored_text
    assert "synthetic-image-bytes" not in stored_text


def test_config_and_model_metadata_do_not_store_private_paths(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    private_config_path = r"C:\Users\Alice\private\config.yml"
    private_model_path = r"C:\Users\Alice\models\model.onnx"

    with StateStore(path) as store:
        store.record_config_snapshot({"version": 1}, source=private_config_path)
        store.record_model_version(
            backend="onnx",
            profile_name="default",
            version="unversioned",
            model_path=private_model_path,
        )

    stored_text = _sqlite_text(path)
    assert private_config_path not in stored_text
    assert private_model_path not in stored_text
    assert r"C:\Users\Alice" not in stored_text
    assert "config.yml" in stored_text


def _sqlite_text(path) -> str:
    values: list[str] = []
    with sqlite3.connect(path) as conn:
        cursor = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
        table_names = [row[0] for row in cursor.fetchall()]
        for table_name in table_names:
            for row in conn.execute(f"SELECT * FROM {table_name}"):
                values.extend(str(value) for value in row if value is not None)
    return "\n".join(values)
