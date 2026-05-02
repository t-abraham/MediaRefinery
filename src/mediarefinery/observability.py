from __future__ import annotations

from collections.abc import Mapping
from time import perf_counter
import logging
import re


SAFE_LOG_FIELD_NAMES = frozenset(
    {
        "event",
        "asset_id",
        "category_id",
        "action_name",
        "duration_ms",
        "error_code",
    }
)
SAFE_LOG_FIELD_ORDER = (
    "event",
    "asset_id",
    "category_id",
    "action_name",
    "duration_ms",
    "error_code",
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
SECRET_ASSIGNMENT_RE = re.compile(
    r"\b(api[_-]?key|authorization|bearer|password|secret|token)\b"
    r"\s*[:=]\s*['\"]?[^'\"\s,;]+",
    re.IGNORECASE,
)


def configure_logging(level_name: str | None) -> None:
    level = getattr(logging, str(level_name or "INFO").upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(message)s")


def monotonic_time() -> float:
    return perf_counter()


def elapsed_ms(start: float) -> int:
    return max(0, int(round((perf_counter() - start) * 1000)))


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    asset_id: object | None = None,
    category_id: object | None = None,
    action_name: object | None = None,
    duration_ms: int | None = None,
    error_code: object | None = None,
) -> None:
    fields = safe_log_fields(
        event=event,
        asset_id=asset_id,
        category_id=category_id,
        action_name=action_name,
        duration_ms=duration_ms,
        error_code=error_code,
    )
    logger.log(
        level,
        format_log_fields(fields),
        extra={"mediarefinery_fields": fields},
    )


def safe_log_fields(**values: object | None) -> dict[str, str | int]:
    fields: dict[str, str | int] = {}
    for name, value in values.items():
        if name not in SAFE_LOG_FIELD_NAMES or value is None:
            continue
        if name == "duration_ms":
            fields[name] = _safe_duration_ms(value)
        else:
            fields[name] = _safe_text(str(value))
    return fields


def format_log_fields(fields: Mapping[str, object]) -> str:
    return " ".join(
        f"{name}={fields[name]}"
        for name in SAFE_LOG_FIELD_ORDER
        if name in fields
    )


def _safe_duration_ms(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def _safe_text(value: str) -> str:
    text = DATA_URI_RE.sub("<redacted-media-data>", value)
    text = SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}=<redacted>",
        text,
    )
    text = WINDOWS_USER_PATH_RE.sub("<user-home-path>", text)
    text = POSIX_USER_PATH_RE.sub("<user-home-path>", text)
    text = LONG_BASE64_RE.sub("<redacted-data>", text)
    text = re.sub(r"[\r\n\t ]+", "_", text.strip())
    text = re.sub(r"[^A-Za-z0-9_.:@<>=-]+", "_", text)
    return (text[:200].strip("_")) or "unknown"
