"""Encryption-at-rest for v2 stored secrets.

Per ADR-0010, Immich session tokens and unattended-scan API keys
must never reach disk in plaintext. This module provides:

- :class:`AesGcmCipher` — AES-256-GCM with a 12-byte nonce, AEAD tag
  baked into the ciphertext, and a one-byte format version prefix so
  payloads can be migrated across cipher revisions.
- :func:`load_or_create_master_key` — resolves the master key from the
  ``MR_MASTER_KEY`` environment variable (urlsafe-base64, 32 bytes) or
  from a key file (default ``/data/master.key`` in the container,
  ``0600`` on POSIX). Generates and persists a fresh key only when both
  sources are absent; this is the bootstrap path for first-run.
- :func:`rotate_encrypted_columns` — re-encrypts every stored secret
  with a new cipher. Operators trigger this when rotating
  ``MR_MASTER_KEY``; the procedure is documented in
  ``docs/v2/operations.md``.

CSRF, rate-limit, and cookie helpers also live in this module
(:class:`SessionCookieSigner`, :func:`derive_cookie_signing_key`,
:class:`InMemoryRateLimiter`, :func:`configure_json_logging`).
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import secrets
import sqlite3
import stat
import threading
import time
from collections import deque
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from itsdangerous import BadSignature, TimestampSigner

MASTER_KEY_ENV = "MR_MASTER_KEY"
MASTER_KEY_BYTES = 32
NONCE_BYTES = 12
FORMAT_V1 = 0x01
DEFAULT_MASTER_KEY_PATH = Path("/data/master.key")


class MasterKeyError(RuntimeError):
    """Raised when the master key cannot be resolved or is malformed."""


@dataclass(frozen=True)
class MasterKey:
    """A 32-byte master key plus its provenance (for logging/audit)."""

    key: bytes
    source: str  # "env", "file", or "generated"

    def __post_init__(self) -> None:
        if not isinstance(self.key, (bytes, bytearray)) or len(self.key) != MASTER_KEY_BYTES:
            raise MasterKeyError(
                f"master key must be exactly {MASTER_KEY_BYTES} bytes"
            )


class AesGcmCipher:
    """AES-256-GCM with a versioned ciphertext layout.

    Layout (big-endian, no separators)::

        version(1) || nonce(12) || ciphertext+tag(N)

    The version byte allows future migrations (e.g. switching to
    XChaCha20-Poly1305) without losing the ability to decrypt legacy
    rows. ``decrypt`` rejects unknown versions.
    """

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, (bytes, bytearray)) or len(key) != MASTER_KEY_BYTES:
            raise MasterKeyError("cipher key must be 32 bytes")
        self._aead = AESGCM(bytes(key))

    def encrypt(self, plaintext: bytes, *, associated_data: bytes | None = None) -> bytes:
        if not isinstance(plaintext, (bytes, bytearray)):
            raise TypeError("plaintext must be bytes")
        nonce = secrets.token_bytes(NONCE_BYTES)
        ct = self._aead.encrypt(nonce, bytes(plaintext), associated_data)
        return bytes([FORMAT_V1]) + nonce + ct

    def decrypt(self, blob: bytes, *, associated_data: bytes | None = None) -> bytes:
        if not isinstance(blob, (bytes, bytearray)):
            raise TypeError("blob must be bytes")
        if len(blob) < 1 + NONCE_BYTES + 16:
            raise ValueError("ciphertext too short")
        version = blob[0]
        if version != FORMAT_V1:
            raise ValueError(f"unsupported ciphertext version {version:#x}")
        nonce = bytes(blob[1 : 1 + NONCE_BYTES])
        ct = bytes(blob[1 + NONCE_BYTES :])
        try:
            return self._aead.decrypt(nonce, ct, associated_data)
        except InvalidTag as exc:  # tampered or wrong-key
            raise ValueError("ciphertext authentication failed") from exc


def _decode_master_key_b64(value: str) -> bytes:
    value = value.strip()
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise MasterKeyError(f"{MASTER_KEY_ENV} is not valid urlsafe-base64") from exc
    if len(raw) != MASTER_KEY_BYTES:
        raise MasterKeyError(
            f"{MASTER_KEY_ENV} must decode to exactly {MASTER_KEY_BYTES} bytes"
        )
    return raw


def _read_master_key_file(path: Path) -> bytes:
    raw = path.read_bytes()
    if len(raw) != MASTER_KEY_BYTES:
        raise MasterKeyError(
            f"master key file {path} must contain exactly "
            f"{MASTER_KEY_BYTES} bytes (got {len(raw)})"
        )
    return raw


def _write_master_key_file(path: Path, key: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Open with O_EXCL so we never silently clobber an existing key.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    binary_flag = getattr(os, "O_BINARY", 0)
    fd = os.open(str(path), flags | binary_flag, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    if os.name == "posix":
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def load_or_create_master_key(
    *,
    path: Path | str | None = None,
    env: dict[str, str] | None = None,
    generate_if_missing: bool = True,
) -> MasterKey:
    """Resolve the master key.

    Order of precedence:
    1. ``MR_MASTER_KEY`` env var (urlsafe-base64 of 32 bytes).
    2. The key file at *path* (default :data:`DEFAULT_MASTER_KEY_PATH`).
    3. A freshly generated 32-byte key written to *path* with mode
       ``0600``, only when *generate_if_missing* is true.

    Raises :class:`MasterKeyError` when no source is available and
    *generate_if_missing* is false.
    """

    env = os.environ if env is None else env
    raw_env = env.get(MASTER_KEY_ENV)
    if raw_env:
        return MasterKey(key=_decode_master_key_b64(raw_env), source="env")

    if path is None:
        path = DEFAULT_MASTER_KEY_PATH
    path = Path(path)

    if path.exists():
        return MasterKey(key=_read_master_key_file(path), source="file")

    if not generate_if_missing:
        raise MasterKeyError(
            f"no master key in env {MASTER_KEY_ENV} or file {path}"
        )

    new_key = secrets.token_bytes(MASTER_KEY_BYTES)
    _write_master_key_file(path, new_key)
    return MasterKey(key=new_key, source="generated")


def rotate_encrypted_columns(
    conn: sqlite3.Connection,
    *,
    old_cipher: AesGcmCipher,
    new_cipher: AesGcmCipher,
) -> dict[str, int]:
    """Re-encrypt every secret BLOB in v2 state with *new_cipher*.

    Returns a count per table for the operator runbook. Runs in a
    single transaction; on any failure the DB is left untouched.
    """

    counts = {"sessions": 0, "user_api_keys": 0}
    try:
        with conn:  # transactional
            for row in conn.execute(
                "SELECT session_id, encrypted_immich_token FROM sessions"
            ).fetchall():
                pt = old_cipher.decrypt(bytes(row["encrypted_immich_token"]))
                new_blob = new_cipher.encrypt(pt)
                conn.execute(
                    "UPDATE sessions SET encrypted_immich_token = ? "
                    "WHERE session_id = ?",
                    (new_blob, row["session_id"]),
                )
                counts["sessions"] += 1

            for row in conn.execute(
                "SELECT id, encrypted_key FROM user_api_keys"
            ).fetchall():
                pt = old_cipher.decrypt(bytes(row["encrypted_key"]))
                new_blob = new_cipher.encrypt(pt)
                conn.execute(
                    "UPDATE user_api_keys SET encrypted_key = ? WHERE id = ?",
                    (new_blob, row["id"]),
                )
                counts["user_api_keys"] += 1
    except ValueError:
        # Decrypt failure under old_cipher: refuse to partially rotate.
        raise
    return counts


SESSION_COOKIE_NAME = "mr_session"
CSRF_COOKIE_NAME = "mr_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"


def derive_cookie_signing_key(master_key: bytes) -> bytes:
    """Derive a cookie-signing subkey from the master key.

    Distinct from the AES-GCM key so that a cookie-signing leak does not
    expose stored secrets and vice versa.
    """

    if len(master_key) != MASTER_KEY_BYTES:
        raise MasterKeyError("master key must be 32 bytes")
    return sha256(b"mr.v1.cookie-signing\x00" + master_key).digest()


class SessionCookieSigner:
    """Signs and verifies opaque session ids for the session cookie.

    Uses :class:`itsdangerous.TimestampSigner` so a stolen cookie can
    be invalidated by lowering the configured max-age and so we get
    constant-time signature comparison for free.
    """

    def __init__(self, signing_key: bytes, *, max_age_seconds: int) -> None:
        self._signer = TimestampSigner(signing_key)
        self._max_age = int(max_age_seconds)

    def sign(self, session_id: str) -> str:
        return self._signer.sign(session_id.encode("ascii")).decode("ascii")

    def verify(self, signed_value: str) -> str:
        try:
            raw = self._signer.unsign(signed_value, max_age=self._max_age)
        except BadSignature as exc:
            raise ValueError("session cookie signature invalid or expired") from exc
        return raw.decode("ascii")


def issue_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_tokens_match(cookie_value: str | None, header_value: str | None) -> bool:
    if not cookie_value or not header_value:
        return False
    return hmac.compare_digest(cookie_value, header_value)


class InMemoryRateLimiter:
    """Sliding-window rate limiter, IP-keyed.

    Suitable for single-replica deployments only. v2.0 ships single-node;
    a Redis-backed limiter can replace this without touching callers.
    """

    def __init__(self, *, max_events: int, window_seconds: float = 60.0) -> None:
        if max_events <= 0:
            raise ValueError("max_events must be positive")
        self._max = max_events
        self._window = float(window_seconds)
        self._lock = threading.Lock()
        self._events: dict[str, deque[float]] = {}

    def check(self, key: str, *, now: float | None = None) -> bool:
        """Return True if the event is permitted; False if it exceeds the
        configured window. Records the event when it is permitted.
        """

        timestamp = time.monotonic() if now is None else float(now)
        cutoff = timestamp - self._window
        with self._lock:
            bucket = self._events.setdefault(key, deque())
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self._max:
                return False
            bucket.append(timestamp)
            return True

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._events.clear()
            else:
                self._events.pop(key, None)


class _JsonFormatter(logging.Formatter):
    """Log formatter that emits one JSON object per line."""

    _SAFE_RECORD_KEYS = {
        "name",
        "levelname",
        "message",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Allow callers to attach structured fields via ``extra={...}``.
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in (
                "args",
                "asctime",
                "created",
                "exc_info",
                "exc_text",
                "filename",
                "funcName",
                "levelname",
                "levelno",
                "lineno",
                "message",
                "module",
                "msecs",
                "msg",
                "name",
                "pathname",
                "process",
                "processName",
                "relativeCreated",
                "stack_info",
                "thread",
                "threadName",
                "taskName",
            ):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except TypeError:
                payload[key] = repr(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, sort_keys=True)


def configure_json_logging(level: int = logging.INFO) -> logging.Logger:
    """Install a JSON stream handler on the root logger.

    Idempotent: safe to call from multiple call sites.
    """

    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, "_mr_json", False):
            root.setLevel(level)
            return root
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    handler._mr_json = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)
    return root


__all__ = [
    "AesGcmCipher",
    "CSRF_COOKIE_NAME",
    "CSRF_HEADER_NAME",
    "DEFAULT_MASTER_KEY_PATH",
    "FORMAT_V1",
    "InMemoryRateLimiter",
    "MASTER_KEY_BYTES",
    "MASTER_KEY_ENV",
    "MasterKey",
    "MasterKeyError",
    "NONCE_BYTES",
    "SESSION_COOKIE_NAME",
    "SessionCookieSigner",
    "configure_json_logging",
    "csrf_tokens_match",
    "derive_cookie_signing_key",
    "issue_csrf_token",
    "load_or_create_master_key",
    "rotate_encrypted_columns",
]
