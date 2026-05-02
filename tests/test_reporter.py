from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import yaml

from mediarefinery.classifier import ClassificationResult
from mediarefinery.reporter import PARTIAL_FAILURE_EXIT_CODE, Reporter
from mediarefinery.state import StateStore
from mediarefinery.immich import AssetRef


LONG_BASE64 = "A" * 100


def test_state_report_snapshot_uses_small_synthetic_sqlite_fixture(tmp_path) -> None:
    state_path, run_id = _partial_failure_state(tmp_path)

    with StateStore(state_path) as store:
        report = store.get_run_report(run_id)

    assert report is not None
    assert report.run_id == run_id
    assert report.command == "scan"
    assert report.status == "completed_with_errors"
    assert report.mode == "dry-run"
    assert report.processed == 2
    assert report.skipped == 1
    assert report.errors == 1
    assert report.by_category == {"needs_review": 1, "ok": 1}
    assert [(count.action_name, count.total, count.failed) for count in report.action_counts] == [
        ("add_tag", 1, 1),
        ("no_action", 1, 0),
    ]
    assert [(count.stage, count.message_code, count.total) for count in report.error_counts] == [
        ("action", "tag_unsupported", 1)
    ]
    assert report.partial_failure is True


def test_markdown_report_includes_counts_and_omits_private_fields(tmp_path) -> None:
    state_path, run_id = _partial_failure_state(tmp_path)

    with StateStore(state_path) as store:
        report = store.get_run_report(run_id)

    assert report is not None
    rendered = Reporter().render_run_report(report)

    assert "# MediaRefinery Run Report" in rendered
    assert "| Mode | dry-run |" in rendered
    assert "- Processed: 2" in rendered
    assert "- Skipped: 1" in rendered
    assert "- Errors: 1" in rendered
    assert "| needs_review | 1 |" in rendered
    assert "| add_tag | tag_unsupported | 1 | 1 | 0 | 1 |" in rendered
    assert "| action | tag_unsupported | 1 | 1 |" in rendered
    assert "| Partial failures | yes |" in rendered
    assert f"| Operational exit code | {PARTIAL_FAILURE_EXIT_CODE} |" in rendered
    assert r"C:\Users\Alice" not in rendered
    assert "/home/alice" not in rendered
    assert "super-secret" not in rendered
    assert "data:image" not in rendered
    assert LONG_BASE64 not in rendered
    assert "thumbnail" not in rendered.lower()
    assert "frame-000001.png" not in rendered


def test_report_command_writes_markdown_to_configured_output_dir(tmp_path) -> None:
    state_path, _run_id = _clean_state(tmp_path)
    config_path = _write_report_config(tmp_path, state_path)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mediarefinery",
            "report",
            "--config",
            str(config_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    report_path = tmp_path / "reports" / "mediarefinery-run-1.md"
    assert result.returncode == 0
    assert result.stdout.strip() == "Report written: mediarefinery-run-1.md"
    assert report_path.exists()
    assert "# MediaRefinery Run Report" in report_path.read_text(encoding="utf-8")


def test_report_command_returns_partial_failure_exit_code_for_failed_run(tmp_path) -> None:
    state_path, _run_id = _partial_failure_state(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mediarefinery",
            "report",
            "--state-path",
            str(state_path),
            "--output",
            "-",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == PARTIAL_FAILURE_EXIT_CODE
    assert "# MediaRefinery Run Report" in result.stdout
    assert "| Partial failures | yes |" in result.stdout
    assert r"C:\Users\Alice" not in result.stdout
    assert "super-secret" not in result.stdout


def test_report_command_rejects_deferred_json_and_csv_formats(tmp_path) -> None:
    state_path, _run_id = _clean_state(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mediarefinery",
            "report",
            "--state-path",
            str(state_path),
            "--output",
            "-",
            "--format",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "only markdown format is implemented" in result.stderr


def _partial_failure_state(tmp_path: Path) -> tuple[Path, int]:
    state_path = tmp_path / "partial.sqlite3"
    with StateStore(state_path) as store:
        config_id = store.record_config_snapshot(
            {"version": 1, "categories": [{"id": "ok"}, {"id": "needs_review"}]},
            source=r"C:\Users\Alice\private\config.yml",
        )
        model_id = store.record_model_version(
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
        ok_asset = AssetRef("asset-ok", "image", checksum="sha256:ok")
        review_asset = AssetRef("asset-review", "image", checksum="sha256:review")
        store.record_classification_run(
            run_id,
            ok_asset,
            ClassificationResult("asset-ok", "ok", {"raw_safety": 1.0}),
            config_snapshot_id=config_id,
            model_version_id=model_id,
        )
        store.record_classification_run(
            run_id,
            review_asset,
            ClassificationResult("asset-review", "needs_review", {"raw_flag": 1.0}),
            config_snapshot_id=config_id,
            model_version_id=model_id,
        )
        store.record_action_run(
            run_id,
            ok_asset.asset_id,
            "no_action",
            dry_run=True,
            would_apply=False,
            success=True,
        )
        store.record_action_run(
            run_id,
            review_asset.asset_id,
            "add_tag",
            dry_run=True,
            would_apply=True,
            success=False,
            error_code="tag_unsupported",
        )
        store.record_error(
            run_id=run_id,
            asset_id=review_asset.asset_id,
            stage="action",
            message_code="tag_unsupported",
            message=(
                r"failed C:\Users\Alice\Pictures\frame-000001.png "
                f"api_key=super-secret data:image/png;base64,{LONG_BASE64}"
            ),
            details={
                "thumbnail_bytes": b"synthetic-image-bytes",
                "frame_path": r"C:\Users\Alice\Pictures\frame-000001.png",
                "secret": "super-secret",
            },
        )
        store.finish_run(
            run_id,
            "completed_with_errors",
            {
                "processed": 2,
                "skipped": 1,
                "errors": 1,
                "by_category": {"needs_review": 1, "ok": 1},
                "intended_actions": {"add_tag": 1, "no_action": 1},
                "dry_run": True,
            },
        )
    return state_path, run_id


def _clean_state(tmp_path: Path) -> tuple[Path, int]:
    state_path = tmp_path / "clean.sqlite3"
    with StateStore(state_path) as store:
        config_id = store.record_config_snapshot({"version": 1}, source="config.yml")
        model_id = store.record_model_version(
            backend="noop",
            profile_name="default",
            version="noop",
        )
        run_id = store.start_run(
            dry_run=True,
            command="scan",
            config_snapshot_id=config_id,
            model_version_id=model_id,
        )
        asset = AssetRef("asset-ok", "image", checksum="sha256:ok")
        store.record_classification_run(
            run_id,
            asset,
            ClassificationResult("asset-ok", "ok", {"raw_safety": 1.0}),
            config_snapshot_id=config_id,
            model_version_id=model_id,
        )
        store.record_action_run(
            run_id,
            asset.asset_id,
            "no_action",
            dry_run=True,
            would_apply=False,
            success=True,
        )
        store.finish_run(
            run_id,
            "succeeded",
            {
                "processed": 1,
                "skipped": 0,
                "errors": 0,
                "by_category": {"ok": 1},
                "intended_actions": {"no_action": 1},
                "dry_run": True,
            },
        )
    return state_path, run_id


def _write_report_config(tmp_path: Path, state_path: Path) -> Path:
    data = yaml.safe_load(Path("templates/config.example.yml").read_text())
    data["state"]["sqlite_path"] = str(state_path)
    data["runtime"]["temp_dir"] = str(tmp_path / "tmp")
    data["reports"]["output_dir"] = str(tmp_path / "reports")
    config_path = tmp_path / "config.yml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return config_path
