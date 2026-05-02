"""Unit tests for cookie signer, CSRF helper, rate limiter, JSON logger."""

from __future__ import annotations

import io
import json
import logging
import time

import pytest

pytest.importorskip("itsdangerous")

from mediarefinery.service.security import (  # noqa: E402
    InMemoryRateLimiter,
    SessionCookieSigner,
    configure_json_logging,
    csrf_tokens_match,
    derive_cookie_signing_key,
    issue_csrf_token,
)


def test_cookie_sign_verify_roundtrip():
    signer = SessionCookieSigner(b"\x01" * 32, max_age_seconds=60)
    token = signer.sign("session-abc")
    assert signer.verify(token) == "session-abc"


def test_cookie_tampered_signature_rejected():
    signer = SessionCookieSigner(b"\x01" * 32, max_age_seconds=60)
    token = signer.sign("session-abc")
    with pytest.raises(ValueError):
        signer.verify(token + "x")


def test_cookie_max_age_enforced():
    signer = SessionCookieSigner(b"\x02" * 32, max_age_seconds=1)
    token = signer.sign("session-x")
    time.sleep(2.1)
    with pytest.raises(ValueError):
        signer.verify(token)


def test_derive_cookie_signing_key_distinct_from_master():
    master = b"\xab" * 32
    derived = derive_cookie_signing_key(master)
    assert derived != master
    assert len(derived) == 32
    # Stable derivation.
    assert derive_cookie_signing_key(master) == derived


def test_csrf_tokens_match():
    t = issue_csrf_token()
    assert csrf_tokens_match(t, t) is True
    assert csrf_tokens_match(t, t + "x") is False
    assert csrf_tokens_match(None, t) is False
    assert csrf_tokens_match(t, None) is False
    assert csrf_tokens_match("", "") is False


def test_rate_limiter_sliding_window():
    limiter = InMemoryRateLimiter(max_events=3, window_seconds=10.0)
    now = 1000.0
    assert limiter.check("ip-a", now=now) is True
    assert limiter.check("ip-a", now=now + 1) is True
    assert limiter.check("ip-a", now=now + 2) is True
    assert limiter.check("ip-a", now=now + 3) is False
    # Different key not affected.
    assert limiter.check("ip-b", now=now + 3) is True
    # After the window slides, the bucket reopens.
    assert limiter.check("ip-a", now=now + 11) is True


def test_rate_limiter_reset():
    limiter = InMemoryRateLimiter(max_events=1, window_seconds=10.0)
    assert limiter.check("ip", now=0.0) is True
    assert limiter.check("ip", now=0.1) is False
    limiter.reset("ip")
    assert limiter.check("ip", now=0.2) is True


def test_json_logging_emits_json_per_line():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    from mediarefinery.service.security import _JsonFormatter

    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        configure_json_logging(level=logging.INFO)
        log = logging.getLogger("test.mr")
        log.info("hello", extra={"event": "test", "user_id": "u1"})
    finally:
        root.removeHandler(handler)

    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    assert lines
    record = json.loads(lines[-1])
    assert record["msg"] == "hello"
    assert record["event"] == "test"
    assert record["user_id"] == "u1"


def test_configure_json_logging_idempotent():
    before = len(logging.getLogger().handlers)
    configure_json_logging()
    once = len(logging.getLogger().handlers)
    configure_json_logging()
    twice = len(logging.getLogger().handlers)
    assert once - before <= 1
    assert twice == once  # no new handler on second call
