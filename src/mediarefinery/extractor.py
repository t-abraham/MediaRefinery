from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from struct import unpack_from
import shutil
import subprocess
import tempfile
from typing import Mapping

from .classifier import ClassifierInput


DEFAULT_IMAGE_SOURCE = "preview"
DEFAULT_VIDEO_FRAME_SOURCE = "ffmpeg_frame"
DEFAULT_VIDEO_FFMPEG_PATH = "ffmpeg"
DEFAULT_VIDEO_FRAME_COUNT = 3
DEFAULT_VIDEO_FRAME_STRATEGY = "uniform"
DEFAULT_VIDEO_MAX_DURATION_SECONDS = 300
SUPPORTED_IMAGE_FORMATS = frozenset({"gif", "jpeg", "png"})
VIDEO_SOURCE_METADATA_KEYS = frozenset(
    {"file_path", "local_path", "path", "video_path"}
)


@dataclass(frozen=True)
class ImageInfo:
    format: str
    content_type: str
    width: int
    height: int


class MediaExtractionError(Exception):
    def __init__(
        self,
        *,
        asset_id: str,
        media_type: str,
        source: str,
        message_code: str,
        message: str,
        details: Mapping[str, object] | None = None,
    ):
        self.asset_id = asset_id
        self.media_type = media_type
        self.source = source
        self.message_code = message_code
        self.message = message
        self.details = dict(details or {})
        super().__init__(message)

    def as_details(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "media_type": self.media_type,
            "source": self.source,
            **self.details,
        }


class MediaExtractor:
    """Prepare media bytes for classifier backends without persisting content."""

    def image_input(
        self,
        *,
        asset_id: str,
        media_type: str,
        image_bytes: bytes | None,
        metadata: Mapping[str, str] | None = None,
        source: str = DEFAULT_IMAGE_SOURCE,
    ) -> ClassifierInput:
        if media_type != "image":
            raise MediaExtractionError(
                asset_id=asset_id,
                media_type=media_type,
                source=source,
                message_code="unsupported_media_type",
                message="image extractor only supports image assets",
                details={"supported_media_types": ["image"]},
            )
        if not image_bytes:
            raise MediaExtractionError(
                asset_id=asset_id,
                media_type=media_type,
                source=source,
                message_code="missing_image_bytes",
                message="image source did not return bytes",
            )

        image_info = inspect_image_bytes(
            image_bytes,
            asset_id=asset_id,
            media_type=media_type,
            source=source,
        )
        input_metadata = dict(metadata or {})
        input_metadata.update(
            {
                "extraction_source": source,
                "image_content_type": image_info.content_type,
                "image_format": image_info.format,
                "image_height": str(image_info.height),
                "image_width": str(image_info.width),
            }
        )
        return ClassifierInput(
            asset_id=asset_id,
            media_type=media_type,
            metadata=input_metadata,
            data=image_bytes,
            content_type=image_info.content_type,
            source=source,
        )

    @contextmanager
    def video_frame_inputs(
        self,
        *,
        asset_id: str,
        media_type: str,
        video_path: str | Path | None,
        metadata: Mapping[str, str] | None = None,
        video_config: Mapping[str, object] | None = None,
        runtime_config: Mapping[str, object] | None = None,
    ) -> Iterator[list[ClassifierInput]]:
        config = dict(video_config or {})
        if media_type != "video":
            raise MediaExtractionError(
                asset_id=asset_id,
                media_type=media_type,
                source=DEFAULT_VIDEO_FRAME_SOURCE,
                message_code="unsupported_media_type",
                message="video extractor only supports video assets",
                details={"supported_media_types": ["video"]},
            )
        if not bool(config.get("enabled", False)):
            raise MediaExtractionError(
                asset_id=asset_id,
                media_type=media_type,
                source=DEFAULT_VIDEO_FRAME_SOURCE,
                message_code="video_processing_disabled",
                message="video processing requires video.enabled=true",
            )
        if video_path is None or not str(video_path).strip():
            raise MediaExtractionError(
                asset_id=asset_id,
                media_type=media_type,
                source=DEFAULT_VIDEO_FRAME_SOURCE,
                message_code="missing_video_source",
                message="video asset did not include a local ffmpeg-readable source",
            )

        frame_count = _positive_int(
            config.get("frame_count"),
            DEFAULT_VIDEO_FRAME_COUNT,
        )
        max_duration_seconds = _positive_int(
            config.get("max_duration_seconds"),
            DEFAULT_VIDEO_MAX_DURATION_SECONDS,
        )
        frame_strategy = str(
            config.get("frame_strategy") or DEFAULT_VIDEO_FRAME_STRATEGY
        )
        if frame_strategy != DEFAULT_VIDEO_FRAME_STRATEGY:
            raise MediaExtractionError(
                asset_id=asset_id,
                media_type=media_type,
                source=DEFAULT_VIDEO_FRAME_SOURCE,
                message_code="unsupported_frame_strategy",
                message="video frame strategy is not supported",
                details={"supported_frame_strategies": [DEFAULT_VIDEO_FRAME_STRATEGY]},
            )
        ffmpeg_path = str(config.get("ffmpeg_path") or DEFAULT_VIDEO_FFMPEG_PATH)

        try:
            temp_root = _temp_root(runtime_config)
            with tempfile.TemporaryDirectory(
                prefix="mediarefinery-video-",
                dir=temp_root,
            ) as temp_dir:
                frame_paths = _extract_video_frame_paths(
                    asset_id=asset_id,
                    video_path=Path(video_path),
                    output_dir=Path(temp_dir),
                    ffmpeg_path=ffmpeg_path,
                    frame_count=frame_count,
                    max_duration_seconds=max_duration_seconds,
                )
                classifier_inputs = [
                    _frame_classifier_input(
                        asset_id=asset_id,
                        frame_path=frame_path,
                        frame_index=index,
                        frame_total=len(frame_paths),
                        frame_strategy=frame_strategy,
                        metadata=metadata,
                    )
                    for index, frame_path in enumerate(frame_paths)
                ]
                yield classifier_inputs
        except OSError as exc:
            raise MediaExtractionError(
                asset_id=asset_id,
                media_type=media_type,
                source=DEFAULT_VIDEO_FRAME_SOURCE,
                message_code="video_temp_failed",
                message="video frame temp storage failed",
                details={"reason": type(exc).__name__},
            ) from exc


def inspect_image_bytes(
    image_bytes: bytes,
    *,
    asset_id: str,
    media_type: str = "image",
    source: str = DEFAULT_IMAGE_SOURCE,
) -> ImageInfo:
    try:
        if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return _inspect_png(image_bytes)
        if image_bytes.startswith((b"GIF87a", b"GIF89a")):
            return _inspect_gif(image_bytes)
        if image_bytes.startswith(b"\xff\xd8"):
            return _inspect_jpeg(image_bytes)
    except ValueError as exc:
        raise MediaExtractionError(
            asset_id=asset_id,
            media_type=media_type,
            source=source,
            message_code="corrupt_image_bytes",
            message="image bytes could not be decoded",
            details={"reason": str(exc)},
        ) from exc

    raise MediaExtractionError(
        asset_id=asset_id,
        media_type=media_type,
        source=source,
        message_code="unsupported_image_format",
        message="image bytes use an unsupported format",
        details={"supported_formats": sorted(SUPPORTED_IMAGE_FORMATS)},
    )


def _inspect_png(image_bytes: bytes) -> ImageInfo:
    if len(image_bytes) < 33:
        raise ValueError("truncated_png_header")
    ihdr_length = unpack_from(">I", image_bytes, 8)[0]
    chunk_type = image_bytes[12:16]
    if ihdr_length != 13 or chunk_type != b"IHDR":
        raise ValueError("missing_png_ihdr")
    width, height = unpack_from(">II", image_bytes, 16)
    _require_positive_dimensions(width, height, "png")
    return ImageInfo(
        format="png",
        content_type="image/png",
        width=width,
        height=height,
    )


def _inspect_gif(image_bytes: bytes) -> ImageInfo:
    if len(image_bytes) < 10:
        raise ValueError("truncated_gif_header")
    width, height = unpack_from("<HH", image_bytes, 6)
    _require_positive_dimensions(width, height, "gif")
    return ImageInfo(
        format="gif",
        content_type="image/gif",
        width=width,
        height=height,
    )


def _inspect_jpeg(image_bytes: bytes) -> ImageInfo:
    index = 2
    while index < len(image_bytes):
        while index < len(image_bytes) and image_bytes[index] == 0xFF:
            index += 1
        if index >= len(image_bytes):
            break

        marker = image_bytes[index]
        index += 1
        if marker in {0xD8, 0xD9}:
            continue
        if marker == 0xDA:
            break
        if index + 2 > len(image_bytes):
            raise ValueError("truncated_jpeg_segment")

        segment_length = unpack_from(">H", image_bytes, index)[0]
        if segment_length < 2 or index + segment_length > len(image_bytes):
            raise ValueError("invalid_jpeg_segment")
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            if segment_length < 7:
                raise ValueError("truncated_jpeg_frame")
            height = unpack_from(">H", image_bytes, index + 3)[0]
            width = unpack_from(">H", image_bytes, index + 5)[0]
            _require_positive_dimensions(width, height, "jpeg")
            return ImageInfo(
                format="jpeg",
                content_type="image/jpeg",
                width=width,
                height=height,
            )
        index += segment_length

    raise ValueError("missing_jpeg_frame")


def _require_positive_dimensions(width: int, height: int, image_format: str) -> None:
    if width < 1 or height < 1:
        raise ValueError(f"invalid_{image_format}_dimensions")


def _extract_video_frame_paths(
    *,
    asset_id: str,
    video_path: Path,
    output_dir: Path,
    ffmpeg_path: str,
    frame_count: int,
    max_duration_seconds: int,
) -> list[Path]:
    try:
        source_exists = video_path.is_file()
    except OSError as exc:
        raise MediaExtractionError(
            asset_id=asset_id,
            media_type="video",
            source=DEFAULT_VIDEO_FRAME_SOURCE,
            message_code="video_source_unavailable",
            message="video source could not be accessed",
            details={"reason": type(exc).__name__},
        ) from exc

    if not source_exists:
        raise MediaExtractionError(
            asset_id=asset_id,
            media_type="video",
            source=DEFAULT_VIDEO_FRAME_SOURCE,
            message_code="video_source_unavailable",
            message="video source is not available to ffmpeg",
        )

    duration_seconds = _probe_video_duration_seconds(ffmpeg_path, video_path)
    extraction_duration = min(
        duration_seconds or float(max_duration_seconds),
        float(max_duration_seconds),
    )
    extraction_duration = max(extraction_duration, 0.001)
    output_pattern = output_dir / "frame-%06d.png"
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-t",
        _format_seconds(extraction_duration),
        "-map",
        "0:v:0",
        "-vf",
        f"fps={frame_count}/{_format_seconds(extraction_duration)}",
        "-frames:v",
        str(frame_count),
        "-f",
        "image2",
        str(output_pattern),
    ]

    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=max(30, max_duration_seconds + 30),
        )
    except FileNotFoundError as exc:
        raise MediaExtractionError(
            asset_id=asset_id,
            media_type="video",
            source=DEFAULT_VIDEO_FRAME_SOURCE,
            message_code="ffmpeg_not_found",
            message="ffmpeg executable was not found",
        ) from exc
    except OSError as exc:
        raise MediaExtractionError(
            asset_id=asset_id,
            media_type="video",
            source=DEFAULT_VIDEO_FRAME_SOURCE,
            message_code="ffmpeg_failed",
            message="ffmpeg could not be started",
            details={"reason": type(exc).__name__},
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaExtractionError(
            asset_id=asset_id,
            media_type="video",
            source=DEFAULT_VIDEO_FRAME_SOURCE,
            message_code="ffmpeg_failed",
            message="ffmpeg timed out while extracting frames",
            details={"reason": "timeout"},
        ) from exc

    if result.returncode != 0:
        raise MediaExtractionError(
            asset_id=asset_id,
            media_type="video",
            source=DEFAULT_VIDEO_FRAME_SOURCE,
            message_code="ffmpeg_failed",
            message="ffmpeg failed to extract frames",
            details={
                "returncode": result.returncode,
                "stderr_present": bool(result.stderr),
            },
        )

    frame_paths = sorted(output_dir.glob("frame-*.png"))[:frame_count]
    if not frame_paths:
        raise MediaExtractionError(
            asset_id=asset_id,
            media_type="video",
            source=DEFAULT_VIDEO_FRAME_SOURCE,
            message_code="no_video_frames_extracted",
            message="ffmpeg did not extract any frames",
        )
    return frame_paths


def _frame_classifier_input(
    *,
    asset_id: str,
    frame_path: Path,
    frame_index: int,
    frame_total: int,
    frame_strategy: str,
    metadata: Mapping[str, str] | None,
) -> ClassifierInput:
    frame_bytes = frame_path.read_bytes()
    try:
        image_info = inspect_image_bytes(
            frame_bytes,
            asset_id=asset_id,
            media_type="video",
            source=DEFAULT_VIDEO_FRAME_SOURCE,
        )
    except MediaExtractionError as exc:
        raise MediaExtractionError(
            asset_id=asset_id,
            media_type="video",
            source=DEFAULT_VIDEO_FRAME_SOURCE,
            message_code="corrupt_video_frame",
            message="extracted video frame could not be decoded",
            details={"frame_index": frame_index, "reason": exc.message_code},
        ) from exc

    input_metadata = {
        key: value
        for key, value in dict(metadata or {}).items()
        if key not in VIDEO_SOURCE_METADATA_KEYS
    }
    input_metadata.update(
        {
            "extraction_source": DEFAULT_VIDEO_FRAME_SOURCE,
            "image_content_type": image_info.content_type,
            "image_format": image_info.format,
            "image_height": str(image_info.height),
            "image_width": str(image_info.width),
            "video_frame_count": str(frame_total),
            "video_frame_index": str(frame_index),
            "video_frame_strategy": frame_strategy,
        }
    )
    return ClassifierInput(
        asset_id=asset_id,
        media_type="video",
        metadata=input_metadata,
        data=frame_bytes,
        content_type=image_info.content_type,
        source=DEFAULT_VIDEO_FRAME_SOURCE,
    )


def _probe_video_duration_seconds(ffmpeg_path: str, video_path: Path) -> float | None:
    ffprobe_path = _ffprobe_path(ffmpeg_path)
    if not _executable_available(ffprobe_path):
        return None

    try:
        result = subprocess.run(
            [
                ffprobe_path,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None
    try:
        duration = float(result.stdout.strip().splitlines()[0])
    except (IndexError, ValueError):
        return None
    if duration <= 0:
        return None
    return duration


def _ffprobe_path(ffmpeg_path: str) -> str:
    ffmpeg = Path(ffmpeg_path)
    suffix = ".exe" if ffmpeg.name.lower().endswith(".exe") else ""
    if ffmpeg.parent != Path("."):
        return str(ffmpeg.with_name(f"ffprobe{suffix}"))
    return f"ffprobe{suffix}"


def _executable_available(path: str) -> bool:
    candidate = Path(path)
    if candidate.parent != Path("."):
        return candidate.is_file()
    return shutil.which(path) is not None


def _positive_int(value: object, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return value


def _temp_root(runtime_config: Mapping[str, object] | None) -> str | None:
    runtime = dict(runtime_config or {})
    temp_dir = runtime.get("temp_dir")
    if not isinstance(temp_dir, str) or not temp_dir.strip():
        return None
    temp_root = Path(temp_dir)
    temp_root.mkdir(parents=True, exist_ok=True)
    return str(temp_root)


def _format_seconds(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"
