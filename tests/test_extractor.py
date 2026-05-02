from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

from mediarefinery.extractor import MediaExtractionError, MediaExtractor
from mediarefinery.immich import SYNTHETIC_IMAGE_PREVIEW_BYTES


def test_image_extractor_returns_classifier_ready_input() -> None:
    classifier_input = MediaExtractor().image_input(
        asset_id="asset-1",
        media_type="image",
        image_bytes=SYNTHETIC_IMAGE_PREVIEW_BYTES,
        metadata={"mock_raw_label": "raw_fixture"},
    )

    assert classifier_input.asset_id == "asset-1"
    assert classifier_input.media_type == "image"
    assert classifier_input.data == SYNTHETIC_IMAGE_PREVIEW_BYTES
    assert classifier_input.content_type == "image/png"
    assert classifier_input.source == "preview"
    assert classifier_input.metadata == {
        "mock_raw_label": "raw_fixture",
        "extraction_source": "preview",
        "image_content_type": "image/png",
        "image_format": "png",
        "image_height": "1",
        "image_width": "1",
    }
    assert "PNG" not in repr(classifier_input)


@pytest.mark.parametrize(
    ("image_bytes", "message_code"),
    [
        (b"", "missing_image_bytes"),
        (b"\x89PNG\r\n\x1a\ntruncated", "corrupt_image_bytes"),
        (b"not an image", "unsupported_image_format"),
    ],
)
def test_image_extractor_reports_structured_input_errors(
    image_bytes: bytes,
    message_code: str,
) -> None:
    with pytest.raises(MediaExtractionError) as exc_info:
        MediaExtractor().image_input(
            asset_id="asset-1",
            media_type="image",
            image_bytes=image_bytes,
        )

    error = exc_info.value
    assert error.asset_id == "asset-1"
    assert error.media_type == "image"
    assert error.source == "preview"
    assert error.message_code == message_code
    assert error.as_details()["asset_id"] == "asset-1"
    assert "not an image" not in str(error.as_details())


def test_image_extractor_rejects_non_image_assets_without_preview_download() -> None:
    with pytest.raises(MediaExtractionError) as exc_info:
        MediaExtractor().image_input(
            asset_id="video-1",
            media_type="video",
            image_bytes=None,
        )

    assert exc_info.value.message_code == "unsupported_media_type"
    assert exc_info.value.as_details()["supported_media_types"] == ["image"]


def test_extractor_does_not_log_image_bytes_on_errors(caplog) -> None:
    private_marker = b"private-preview-marker"
    with pytest.raises(MediaExtractionError):
        MediaExtractor().image_input(
            asset_id="asset-1",
            media_type="image",
            image_bytes=b"\x89PNG\r\n\x1a\n" + private_marker,
        )

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert private_marker.decode("ascii") not in log_text
    assert "data:image" not in log_text


def test_video_extractor_is_disabled_by_default(tmp_path) -> None:
    video_path = tmp_path / "input.mp4"
    video_path.write_bytes(b"synthetic placeholder")

    with pytest.raises(MediaExtractionError) as exc_info:
        with MediaExtractor().video_frame_inputs(
            asset_id="video-1",
            media_type="video",
            video_path=video_path,
            video_config={},
            runtime_config={"temp_dir": str(tmp_path / "frames")},
        ):
            pass

    assert exc_info.value.message_code == "video_processing_disabled"


def test_video_extractor_deletes_temp_frames_on_success(
    tmp_path,
    monkeypatch,
) -> None:
    video_path = tmp_path / "input.mp4"
    video_path.write_bytes(b"synthetic placeholder")
    temp_root = tmp_path / "frames"
    monkeypatch.setattr(
        "mediarefinery.extractor.subprocess.run",
        _fake_ffmpeg_success,
    )

    with MediaExtractor().video_frame_inputs(
        asset_id="video-1",
        media_type="video",
        video_path=video_path,
        metadata={"mock_raw_label": "raw_fixture", "video_path": str(video_path)},
        video_config={"enabled": True, "frame_count": 2, "ffmpeg_path": "ffmpeg"},
        runtime_config={"temp_dir": str(temp_root)},
    ) as inputs:
        assert len(inputs) == 2
        assert [item.metadata["video_frame_index"] for item in inputs] == ["0", "1"]
        assert all(item.content_type == "image/png" for item in inputs)
        assert all(item.data == SYNTHETIC_IMAGE_PREVIEW_BYTES for item in inputs)
        assert all("video_path" not in item.metadata for item in inputs)
        assert any(temp_root.iterdir())

    assert list(temp_root.iterdir()) == []


def test_video_extractor_deletes_temp_frames_on_classifier_failure(
    tmp_path,
    monkeypatch,
) -> None:
    video_path = tmp_path / "input.mp4"
    video_path.write_bytes(b"synthetic placeholder")
    temp_root = tmp_path / "frames"
    monkeypatch.setattr(
        "mediarefinery.extractor.subprocess.run",
        _fake_ffmpeg_success,
    )

    with pytest.raises(RuntimeError):
        with MediaExtractor().video_frame_inputs(
            asset_id="video-1",
            media_type="video",
            video_path=video_path,
            video_config={"enabled": True, "frame_count": 1, "ffmpeg_path": "ffmpeg"},
            runtime_config={"temp_dir": str(temp_root)},
        ):
            raise RuntimeError("classifier failed")

    assert list(temp_root.iterdir()) == []


def test_video_extractor_deletes_temp_frames_on_ffmpeg_failure(
    tmp_path,
    monkeypatch,
) -> None:
    video_path = tmp_path / "input.mp4"
    video_path.write_bytes(b"synthetic placeholder")
    temp_root = tmp_path / "frames"
    monkeypatch.setattr(
        "mediarefinery.extractor.subprocess.run",
        _fake_ffmpeg_failure,
    )

    with pytest.raises(MediaExtractionError) as exc_info:
        with MediaExtractor().video_frame_inputs(
            asset_id="video-1",
            media_type="video",
            video_path=video_path,
            video_config={"enabled": True, "frame_count": 1, "ffmpeg_path": "ffmpeg"},
            runtime_config={"temp_dir": str(temp_root)},
        ):
            pass

    assert exc_info.value.message_code == "ffmpeg_failed"
    assert list(temp_root.iterdir()) == []


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is unavailable")
def test_video_extractor_reads_generated_safe_video_when_ffmpeg_is_available(
    tmp_path,
) -> None:
    video_path = tmp_path / "generated.mp4"
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=16x16:rate=3",
            "-t",
            "1",
            "-c:v",
            "mpeg4",
            str(video_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip("ffmpeg could not generate the safe video fixture")

    temp_root = tmp_path / "frames"
    with MediaExtractor().video_frame_inputs(
        asset_id="video-1",
        media_type="video",
        video_path=video_path,
        video_config={
            "enabled": True,
            "frame_count": 2,
            "max_duration_seconds": 1,
            "ffmpeg_path": "ffmpeg",
        },
        runtime_config={"temp_dir": str(temp_root)},
    ) as inputs:
        assert 1 <= len(inputs) <= 2
        assert all(item.content_type == "image/png" for item in inputs)

    assert list(temp_root.iterdir()) == []


def _fake_ffmpeg_success(command, **kwargs):
    if _is_ffprobe(command):
        return subprocess.CompletedProcess(command, 1, "", "")
    _write_fake_frames(command)
    return subprocess.CompletedProcess(command, 0, "", "")


def _fake_ffmpeg_failure(command, **kwargs):
    if _is_ffprobe(command):
        return subprocess.CompletedProcess(command, 1, "", "")
    _write_fake_frames(command)
    return subprocess.CompletedProcess(command, 1, "", "failed")


def _write_fake_frames(command) -> None:
    frame_count = int(command[command.index("-frames:v") + 1])
    output_pattern = Path(command[-1])
    for index in range(1, frame_count + 1):
        frame_path = output_pattern.parent / f"frame-{index:06d}.png"
        frame_path.write_bytes(SYNTHETIC_IMAGE_PREVIEW_BYTES)


def _is_ffprobe(command) -> bool:
    return Path(command[0]).name.startswith("ffprobe")
