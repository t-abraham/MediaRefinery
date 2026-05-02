"""Immich auth-proxy and session lifecycle helpers.

The MR backend never stores user passwords. ``proxy_login`` forwards
the credentials to Immich's ``POST /api/auth/login`` (per
``docs/v2/immich-api-compat.md``), encrypts the returned access token,
and persists the session.

Token revalidation (T15): :func:`should_revalidate_session` decides
when to call Immich ``GET /api/users/me`` based on ``last_revalidated_at``;
:func:`revalidate_via_users_me` performs the call and returns the
fresh user payload (or raises on 401, signalling that the upstream
session is gone).
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from .security import AesGcmCipher

LOGIN_PATH = "/api/auth/login"
LOGOUT_PATH = "/api/auth/logout"
USERS_ME_PATH = "/api/users/me"


class AuthError(RuntimeError):
    """Raised when proxy login or revalidation fails for a non-credential
    reason (network error, malformed response). Wrong-password failures
    surface as :class:`InvalidCredentials` instead.
    """


class InvalidCredentials(AuthError):
    """The upstream Immich rejected the credentials."""


@dataclass(frozen=True)
class ImmichLoginResult:
    user_id: str
    email: str
    name: str | None
    is_admin: bool
    access_token: str


def proxy_login(
    *,
    immich_base_url: str,
    email: str,
    password: str,
    client: httpx.Client | None = None,
    timeout: float = 10.0,
) -> ImmichLoginResult:
    """Forward ``email``/``password`` to Immich and return the user identity.

    Never logs or returns the password. Callers must encrypt
    ``access_token`` before persisting.
    """

    payload = {"email": email, "password": password}
    own_client = client is None
    if client is None:
        client = httpx.Client(base_url=immich_base_url, timeout=timeout)
    try:
        response = client.post(LOGIN_PATH, json=payload)
    except httpx.HTTPError as exc:
        raise AuthError("upstream Immich unreachable") from exc
    finally:
        if own_client:
            client.close()

    if response.status_code in (400, 401, 403):
        raise InvalidCredentials("Immich rejected the credentials")
    if response.status_code != 201:
        raise AuthError(f"unexpected Immich login status {response.status_code}")

    try:
        body = response.json()
    except ValueError as exc:
        raise AuthError("Immich login returned non-JSON body") from exc

    try:
        return ImmichLoginResult(
            user_id=str(body["userId"]),
            email=str(body["userEmail"]),
            name=body.get("name"),
            is_admin=bool(body.get("isAdmin", False)),
            access_token=str(body["accessToken"]),
        )
    except KeyError as exc:
        raise AuthError(f"Immich login missing field: {exc}") from exc


def proxy_logout(
    *,
    immich_base_url: str,
    access_token: str,
    client: httpx.Client | None = None,
    timeout: float = 10.0,
) -> None:
    own_client = client is None
    if client is None:
        client = httpx.Client(base_url=immich_base_url, timeout=timeout)
    try:
        # Best-effort: a 401 here just means the token was already invalid.
        client.post(
            LOGOUT_PATH,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    except httpx.HTTPError:
        pass
    finally:
        if own_client:
            client.close()


def revalidate_via_users_me(
    *,
    immich_base_url: str,
    access_token: str,
    client: httpx.Client | None = None,
    timeout: float = 10.0,
) -> dict:
    own_client = client is None
    if client is None:
        client = httpx.Client(base_url=immich_base_url, timeout=timeout)
    try:
        response = client.get(
            USERS_ME_PATH,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    except httpx.HTTPError as exc:
        raise AuthError("upstream Immich unreachable") from exc
    finally:
        if own_client:
            client.close()

    if response.status_code == 401:
        raise InvalidCredentials("Immich session no longer valid")
    if response.status_code != 200:
        raise AuthError(f"unexpected Immich users/me status {response.status_code}")
    return response.json()


def should_revalidate_session(
    last_revalidated_at: str | None,
    *,
    interval_seconds: int,
    now: datetime | None = None,
) -> bool:
    if last_revalidated_at is None:
        return True
    now = now or datetime.now(timezone.utc)
    try:
        last = datetime.fromisoformat(last_revalidated_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds() >= interval_seconds


def mint_session_id() -> str:
    return secrets.token_urlsafe(32)


def session_expiry(*, now: datetime | None = None, ttl_seconds: int) -> str:
    now = now or datetime.now(timezone.utc)
    return (now + timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def persist_session(
    *,
    conn: sqlite3.Connection,
    user_id: str,
    session_id: str,
    encrypted_token: bytes,
    expires_at: str,
) -> None:
    """Insert a session row directly (bypasses UserScopedState because
    the cookie has not been issued yet at the call site).
    """

    conn.execute(
        """
        INSERT INTO sessions(session_id, user_id, encrypted_immich_token, expires_at,
                             last_revalidated_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (session_id, user_id, encrypted_token, expires_at),
    )
    conn.commit()


def revoke_session(*, conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute(
        "UPDATE sessions SET revoked_at = CURRENT_TIMESTAMP WHERE session_id = ?",
        (session_id,),
    )
    conn.commit()


def lookup_session(
    *,
    conn: sqlite3.Connection,
    session_id: str,
) -> sqlite3.Row | None:
    cursor = conn.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
    )
    return cursor.fetchone()


def decrypt_session_token(
    *,
    cipher: AesGcmCipher,
    row: sqlite3.Row,
) -> str:
    pt = cipher.decrypt(bytes(row["encrypted_immich_token"]))
    return pt.decode("utf-8")


def mark_session_revalidated(
    *, conn: sqlite3.Connection, session_id: str
) -> None:
    conn.execute(
        "UPDATE sessions SET last_revalidated_at = CURRENT_TIMESTAMP "
        "WHERE session_id = ?",
        (session_id,),
    )
    conn.commit()


__all__ = [
    "AuthError",
    "ImmichLoginResult",
    "InvalidCredentials",
    "LOGIN_PATH",
    "LOGOUT_PATH",
    "USERS_ME_PATH",
    "decrypt_session_token",
    "lookup_session",
    "mark_session_revalidated",
    "mint_session_id",
    "persist_session",
    "proxy_login",
    "proxy_logout",
    "revalidate_via_users_me",
    "revoke_session",
    "session_expiry",
    "should_revalidate_session",
]
