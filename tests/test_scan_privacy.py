from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
import re
import socket
import sqlite3
import subprocess

from mediarefinery.config import load_config, validate_config_data
from mediarefinery.immich import AssetRef, MockImmichClient, SYNTHETIC_IMAGE_PREVIEW_BYTES
from mediarefinery.onnx_backend import OnnxClassifierBackend
from mediarefinery.observability import log_event
from mediarefinery.pipeline import run_scan


LONG_BASE64_RE = re.compile(r"[A-Za-z0-9+/]{80,}={0,2}")


def test_scan_dry_run_summary_is_safe_and_has_no_media_bytes(tmp_path, capsys) -> None:
    config = load_config("templates/config.example.yml")

    summary = run_scan(config, state_path=tmp_path / "state.sqlite3")

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert summary.processed == 3
    assert summary.skipped == 0
    assert summary.errors == 0
    assert summary.by_category == {"needs_review": 1, "ok": 2}
    assert summary.intended_actions == {
        "add_tag": 1,
        "add_to_review_album": 1,
        "no_action": 2,
    }
    assert "Processed: 3" in output
    assert "Skipped: 0" in output
    assert "Errors: 0" in output
    assert "Intended actions (not applied):" in output
    assert "add_to_review_album=1" in output
    assert "add_tag=1" in output
    assert "dry_run=true" in output
    assert "data:image" not in output
    assert not LONG_BASE64_RE.search(output)


def test_scan_dry_run_default_does_not_use_network_or_api_key(
    tmp_path,
    monkeypatch,
) -> None:
    config = load_config("templates/config.example.yml")
    monkeypatch.delenv("IMMICH_API_KEY", raising=False)

    class BlockedSocket:
        def __init__(self, *args, **kwargs):
            raise AssertionError("network access is not allowed in default scan tests")

    monkeypatch.setattr(socket, "socket", BlockedSocket)

    summary = run_scan(config, state_path=tmp_path / "state.sqlite3")

    assert summary.processed == 3


def test_scan_dry_run_uses_previews_without_mutating_immich(tmp_path) -> None:
    config = load_config("templates/config.example.yml")

    class ReadOnlyTrapClient(MockImmichClient):
        def add_to_album(self, *args, **kwargs):  # pragma: no cover - fail path
            raise AssertionError("dry-run scanner must not mutate Immich")

        def create_or_get_album(self, *args, **kwargs):  # pragma: no cover - fail path
            raise AssertionError("dry-run scanner must not mutate Immich")

        def create_or_get_tag(self, *args, **kwargs):  # pragma: no cover - fail path
            raise AssertionError("dry-run scanner must not mutate Immich")

        def add_tag_to_asset(self, *args, **kwargs):  # pragma: no cover - fail path
            raise AssertionError("dry-run scanner must not mutate Immich")

        def archive_asset(self, *args, **kwargs):  # pragma: no cover - fail path
            raise AssertionError("dry-run scanner must not mutate Immich")

    client = ReadOnlyTrapClient()

    summary = run_scan(
        config,
        state_path=tmp_path / "state.sqlite3",
        client=client,
    )

    assert summary.processed == 3
    assert client.preview_requests == [
        "mock-image-001",
        "mock-image-002",
        "mock-image-003",
    ]


def test_scan_skips_unchanged_assets_and_reprocess_overrides(tmp_path) -> None:
    config = load_config("templates/config.example.yml")
    state_path = tmp_path / "state.sqlite3"

    first_summary = run_scan(config, state_path=state_path)
    second_summary = run_scan(config, state_path=state_path)

    reprocess_data = copy.deepcopy(config.raw)
    reprocess_data["scanner"]["reprocess"] = True
    reprocess_config = validate_config_data(reprocess_data)
    reprocess_summary = run_scan(reprocess_config, state_path=state_path)

    assert first_summary.processed == 3
    assert second_summary.processed == 0
    assert second_summary.skipped == 3
    assert reprocess_summary.processed == 3
    assert reprocess_summary.skipped == 0


def test_scan_records_dry_run_intended_actions_in_state(tmp_path) -> None:
    config = load_config("templates/config.example.yml")
    state_path = tmp_path / "state.sqlite3"

    summary = run_scan(config, state_path=state_path)

    assert summary.intended_actions["add_to_review_album"] == 1
    action_rows = _sqlite_rows(
        state_path,
        """
        SELECT action_name, dry_run, would_apply, success, error_code
        FROM action_runs
        ORDER BY action_name, id
        """,
    )
    assert action_rows == [
        ("add_tag", 1, 1, 1, None),
        ("add_to_review_album", 1, 1, 1, None),
        ("no_action", 1, 0, 1, None),
        ("no_action", 1, 0, 1, None),
    ]


def test_scan_missing_policy_summary_is_safe_and_explicit(tmp_path, capsys) -> None:
    data = copy.deepcopy(load_config("templates/config.example.yml").raw)
    del data["policies"]["needs_review"]["image"]
    config = validate_config_data(data)

    summary = run_scan(config, state_path=tmp_path / "state.sqlite3")

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert summary.processed == 3
    assert summary.errors == 1
    assert summary.intended_actions == {"no_action": 3}
    assert "Errors: 1" in output
    assert "Intended actions (not applied): no_action=3" in output
    assert "data:image" not in output
    assert not LONG_BASE64_RE.search(output)


def test_scan_records_extractor_errors_without_media_bytes(tmp_path, capsys) -> None:
    config = load_config("templates/config.example.yml")
    state_path = tmp_path / "state.sqlite3"
    corrupt_preview = b"not an image"
    client = MockImmichClient(
        preview_bytes_by_asset_id={"mock-image-002": corrupt_preview}
    )

    summary = run_scan(config, state_path=state_path, client=client)

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert summary.processed == 2
    assert summary.errors == 1
    assert summary.by_category == {"ok": 2}
    assert summary.intended_actions == {"no_action": 2}
    assert client.preview_requests == [
        "mock-image-001",
        "mock-image-002",
        "mock-image-003",
    ]
    assert "not an image" not in output
    assert "data:image" not in output
    assert not LONG_BASE64_RE.search(output)

    error_rows = _sqlite_rows(
        state_path,
        "SELECT asset_id, stage, message_code, message, details_json FROM errors",
    )
    assert len(error_rows) == 1
    assert error_rows[0][0:3] == (
        "mock-image-002",
        "extractor",
        "unsupported_image_format",
    )
    assert "unsupported format" in error_rows[0][3]

    stored_text = _sqlite_text(state_path)
    assert corrupt_preview.decode("ascii") not in stored_text
    assert "data:image" not in stored_text
    assert not LONG_BASE64_RE.search(stored_text)


def test_scan_records_missing_preview_bytes_and_continues(tmp_path) -> None:
    config = load_config("templates/config.example.yml")
    state_path = tmp_path / "state.sqlite3"
    client = MockImmichClient(preview_bytes_by_asset_id={"mock-image-001": b""})

    summary = run_scan(config, state_path=state_path, client=client)

    assert summary.processed == 2
    assert summary.errors == 1
    error_rows = _sqlite_rows(
        state_path,
        "SELECT asset_id, stage, message_code FROM errors",
    )
    assert error_rows == [
        ("mock-image-001", "extractor", "missing_image_bytes")
    ]


def test_scan_structured_logs_use_safe_fields(tmp_path, caplog) -> None:
    config = load_config("templates/config.example.yml")
    client = MockImmichClient(preview_bytes_by_asset_id={"mock-image-002": b"bad"})
    caplog.set_level(logging.INFO, logger="mediarefinery.pipeline")

    summary = run_scan(config, state_path=tmp_path / "state.sqlite3", client=client)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert summary.errors == 1
    assert "event=scan.start" in messages
    assert "event=asset.classified" in messages
    assert "event=asset.error" in messages
    assert "event=action.result" in messages
    assert "event=scan.finish" in messages
    assert "asset_id=mock-image-002" in messages
    assert "error_code=unsupported_image_format" in messages
    assert "duration_ms=" in messages
    assert "data:image" not in messages
    assert "thumbnail" not in messages.lower()
    assert not LONG_BASE64_RE.search(messages)

    for record in caplog.records:
        fields = getattr(record, "mediarefinery_fields", {})
        assert set(fields).issubset(
            {
                "event",
                "asset_id",
                "category_id",
                "action_name",
                "duration_ms",
                "error_code",
            }
        )


def test_structured_log_sanitizer_redacts_private_fields(caplog) -> None:
    logger = logging.getLogger("mediarefinery.test")
    unsafe = (
        r"C:\Users\Alice\Pictures\frame-000001.png "
        f"api_key=super-secret data:image/png;base64,{'A' * 100}"
    )
    caplog.set_level(logging.INFO, logger="mediarefinery.test")

    log_event(
        logger,
        "asset.error",
        asset_id=unsafe,
        action_name="add_tag",
        duration_ms=3,
        error_code=unsafe,
    )

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert r"C:\Users\Alice" not in messages
    assert "super-secret" not in messages
    assert "data:image" not in messages
    assert "frame-000001.png" not in messages
    assert not LONG_BASE64_RE.search(messages)


def test_scan_finish_summary_json_contains_current_run_counts(tmp_path) -> None:
    config = load_config("templates/config.example.yml")
    state_path = tmp_path / "state.sqlite3"

    run_scan(config, state_path=state_path)

    rows = _sqlite_rows(state_path, "SELECT summary_json FROM runs")
    summary_json = json.loads(rows[0][0])
    assert summary_json == {
        "by_category": {"needs_review": 1, "ok": 2},
        "dry_run": True,
        "errors": 0,
        "intended_actions": {
            "add_tag": 1,
            "add_to_review_album": 1,
            "no_action": 2,
        },
        "processed": 3,
        "skipped": 0,
    }


def test_scan_video_asset_uses_temp_frames_and_aggregates_once(
    tmp_path,
    monkeypatch,
) -> None:
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"synthetic placeholder")
    temp_root = tmp_path / "frames"
    config = _video_enabled_config(tmp_path)
    client = MockImmichClient(
        assets=[
            AssetRef(
                asset_id="video-1",
                media_type="video",
                checksum="sha256:video",
                metadata={"mock_raw_label": "raw_flag", "video_path": str(video_path)},
            )
        ]
    )
    monkeypatch.setattr(
        "mediarefinery.extractor.subprocess.run",
        _fake_ffmpeg_success,
    )

    summary = run_scan(config, state_path=tmp_path / "state.sqlite3", client=client)

    assert summary.processed == 1
    assert summary.errors == 0
    assert summary.by_category == {"needs_review": 1}
    assert summary.intended_actions == {"manual_review": 1}
    assert list(temp_root.iterdir()) == []

    rows = _sqlite_rows(
        tmp_path / "state.sqlite3",
        "SELECT asset_id, category_id, raw_scores_json FROM classification_runs",
    )
    assert rows == [("video-1", "needs_review", '{"raw_flag": 1.0}')]
    stored_text = _sqlite_text(tmp_path / "state.sqlite3")
    assert str(video_path) not in stored_text
    assert "data:video" not in stored_text
    assert not LONG_BASE64_RE.search(stored_text)


def test_scan_video_ffmpeg_error_is_structured_and_sanitized(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"synthetic placeholder")
    temp_root = tmp_path / "frames"
    config = _video_enabled_config(tmp_path)
    client = MockImmichClient(
        assets=[
            AssetRef(
                asset_id="video-1",
                media_type="video",
                checksum="sha256:video",
                metadata={"mock_raw_label": "raw_flag", "video_path": str(video_path)},
            )
        ]
    )
    monkeypatch.setattr(
        "mediarefinery.extractor.subprocess.run",
        _fake_ffmpeg_failure,
    )

    summary = run_scan(config, state_path=tmp_path / "state.sqlite3", client=client)

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert summary.processed == 0
    assert summary.errors == 1
    assert list(temp_root.iterdir()) == []
    assert str(video_path) not in output
    assert not LONG_BASE64_RE.search(output)

    error_rows = _sqlite_rows(
        tmp_path / "state.sqlite3",
        "SELECT asset_id, stage, message_code, message, details_json FROM errors",
    )
    assert len(error_rows) == 1
    assert error_rows[0][0:3] == ("video-1", "extractor", "ffmpeg_failed")
    assert "failed to extract frames" in error_rows[0][3]

    stored_text = _sqlite_text(tmp_path / "state.sqlite3")
    assert str(video_path) not in stored_text
    assert "data:video" not in stored_text
    assert not LONG_BASE64_RE.search(stored_text)


def test_onnx_classifier_error_does_not_persist_media_or_model_details(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    model_path = tmp_path / "operator-model.onnx"
    model_path.write_bytes(b"synthetic placeholder model")
    private_detail = (
        r"C:\Users\Alice\Pictures\private-model.onnx "
        f"data:image/png;base64,{'A' * 100}"
    )
    data = copy.deepcopy(load_config("templates/config.example.yml").raw)
    data["classifier_profiles"]["default"].update(
        {
            "backend": "onnx",
            "model_path": str(model_path),
            "output_mapping": {
                "raw_safety": "ok",
                "raw_flag": "needs_review",
            },
        }
    )
    config = validate_config_data(data)
    monkeypatch.setattr(
        "mediarefinery.onnx_backend._load_onnx_dependencies",
        lambda: _FailingOnnxDependencies(private_detail),
    )
    monkeypatch.setattr(
        OnnxClassifierBackend,
        "_preprocess_input",
        lambda self, classifier_input: _FakeTensor([1.0]),
    )

    summary = run_scan(config, state_path=tmp_path / "state.sqlite3")

    captured = capsys.readouterr()
    combined_text = (
        captured.out
        + captured.err
        + "\n"
        + _sqlite_text(tmp_path / "state.sqlite3")
    )
    assert summary.processed == 0
    assert summary.errors == 3
    assert str(model_path) not in combined_text
    assert "synthetic placeholder model" not in combined_text
    assert "private-model.onnx" not in combined_text
    assert "Alice" not in combined_text
    assert "data:image" not in combined_text
    assert not LONG_BASE64_RE.search(combined_text)


def _sqlite_rows(path, query: str) -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return list(conn.execute(query))


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


def _video_enabled_config(tmp_path):
    data = copy.deepcopy(load_config("templates/config.example.yml").raw)
    data["scanner"]["media_types"] = ["video"]
    data["video"]["enabled"] = True
    data["video"]["frame_count"] = 2
    data["video"]["max_duration_seconds"] = 2
    data["runtime"]["temp_dir"] = str(tmp_path / "frames")
    data["policies"]["needs_review"]["video"] = {"on_match": ["manual_review"]}
    data["policies"]["ok"]["video"] = {"on_match": ["no_action"]}
    return validate_config_data(data)


def _fake_ffmpeg_success(command, **kwargs):
    if Path(command[0]).name.startswith("ffprobe"):
        return subprocess.CompletedProcess(command, 1, "", "")
    _write_fake_frames(command)
    return subprocess.CompletedProcess(command, 0, "", "")


def _fake_ffmpeg_failure(command, **kwargs):
    if Path(command[0]).name.startswith("ffprobe"):
        return subprocess.CompletedProcess(command, 1, "", "")
    _write_fake_frames(command)
    return subprocess.CompletedProcess(command, 1, "", "failed")


def _write_fake_frames(command) -> None:
    frame_count = int(command[command.index("-frames:v") + 1])
    output_pattern = Path(command[-1])
    for index in range(1, frame_count + 1):
        frame_path = output_pattern.parent / f"frame-{index:06d}.png"
        frame_path.write_bytes(SYNTHETIC_IMAGE_PREVIEW_BYTES)


class _FakeTensor:
    def __init__(self, value):
        self.value = value

    def astype(self, dtype, copy=False):
        return self


class _FailingNumpy:
    float32 = "float32"

    @staticmethod
    def stack(values, axis=0):
        return _FakeTensor(list(values))


class _OnnxNode:
    def __init__(self, name: str):
        self.name = name


class _FailingOnnxSession:
    def __init__(self, private_detail: str):
        self.private_detail = private_detail

    def get_inputs(self):
        return [_OnnxNode("image")]

    def get_outputs(self):
        return [_OnnxNode("scores")]

    def run(self, output_names, feed):
        raise RuntimeError(f"classifier runtime failed: {self.private_detail}")


class _FailingOnnxRuntime:
    def __init__(self, private_detail: str):
        self.private_detail = private_detail

    def InferenceSession(self, model_path, providers):
        return _FailingOnnxSession(self.private_detail)


class _FailingOnnxDependencies:
    def __init__(self, private_detail: str):
        self.ort = _FailingOnnxRuntime(private_detail)
        self.np = _FailingNumpy()
        self.image = object()
        self.image_ops = object()

