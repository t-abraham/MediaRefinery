"""Request-scoped FastAPI dependencies.

Exposes :func:`get_current_user`, the load-bearing auth gate that every
non-public endpoint depends on. The dependency:

1. Reads the signed session cookie.
2. Looks up the session row.
3. Refuses revoked or expired sessions.
4. Optionally revalidates the upstream Immich token (T15) at the
   configured cadence.

Returns the user id; the route handler then calls
``state.with_user(user_id)`` to scope its DB access. CSRF enforcement
is a separate dependency (:func:`require_csrf`) so GET handlers can
opt out.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

import httpx
from fastapi import Cookie, Depends, Header, HTTPException, Request, status

from . import auth as _auth
from .config import ServiceConfig
from .security import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    AesGcmCipher,
    SessionCookieSigner,
    csrf_tokens_match,
)
from .state_v2 import StateStoreV2


def get_state(request: Request) -> StateStoreV2:
    return request.app.state.store


def get_cipher(request: Request) -> AesGcmCipher:
    return request.app.state.cipher


def get_signer(request: Request) -> SessionCookieSigner:
    return request.app.state.signer


def get_service_config(request: Request) -> ServiceConfig:
    return request.app.state.config


def get_immich_client(request: Request) -> httpx.Client:
    return request.app.state.immich_client


def client_ip(request: Request, config: ServiceConfig) -> str:
    """Resolve the client IP, honouring ``MR_TRUSTED_PROXIES``.

    When the immediate peer is a configured trusted proxy, the
    rightmost ``X-Forwarded-For`` entry is used; otherwise the peer's
    own address is returned. Avoids the common bug of trusting
    arbitrary forwarded headers from untrusted clients.
    """

    peer = request.client.host if request.client else "0.0.0.0"
    if peer in config.trusted_proxies:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[-1].strip() or peer
    return peer


def get_current_user(
    request: Request,
    state: Annotated[StateStoreV2, Depends(get_state)],
    cipher: Annotated[AesGcmCipher, Depends(get_cipher)],
    signer: Annotated[SessionCookieSigner, Depends(get_signer)],
    config: Annotated[ServiceConfig, Depends(get_service_config)],
    immich: Annotated[httpx.Client, Depends(get_immich_client)],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
) -> str:
    if not session_cookie:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    try:
        session_id = signer.verify(session_cookie)
    except ValueError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid session") from exc

    row = _auth.lookup_session(conn=state._conn, session_id=session_id)
    if row is None or row["revoked_at"] is not None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="session revoked")

    expires_at = row["expires_at"]
    try:
        expiry = datetime.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid session expiry")
    if datetime.now(timezone.utc) >= expiry:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="session expired")

    if _auth.should_revalidate_session(
        row["last_revalidated_at"],
        interval_seconds=config.revalidate_interval_seconds,
    ):
        try:
            access_token = _auth.decrypt_session_token(cipher=cipher, row=row)
            _auth.revalidate_via_users_me(
                immich_base_url=config.immich_base_url,
                access_token=access_token,
                client=immich,
            )
        except _auth.InvalidCredentials:
            _auth.revoke_session(conn=state._conn, session_id=session_id)
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, detail="upstream session expired"
            )
        except _auth.AuthError as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, detail="upstream Immich unreachable"
            ) from exc
        _auth.mark_session_revalidated(conn=state._conn, session_id=session_id)

    request.state.session_id = session_id
    return str(row["user_id"])


def require_admin(
    request: Request,
    user_id: Annotated[str, Depends(get_current_user)],
    state: Annotated[StateStoreV2, Depends(get_state)],
) -> str:
    cursor = state._conn.execute(
        "SELECT is_admin FROM users WHERE user_id = ?", (user_id,)
    )
    row = cursor.fetchone()
    if row is None or not bool(row["is_admin"]):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="admin only")
    return user_id


def require_csrf(
    csrf_cookie: Annotated[str | None, Cookie(alias=CSRF_COOKIE_NAME)] = None,
    csrf_header: Annotated[str | None, Header(alias=CSRF_HEADER_NAME)] = None,
) -> None:
    if not csrf_tokens_match(csrf_cookie, csrf_header):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="csrf check failed")


__all__ = [
    "client_ip",
    "get_cipher",
    "get_current_user",
    "get_immich_client",
    "get_service_config",
    "get_signer",
    "get_state",
    "require_admin",
    "require_csrf",
]
