"""PIN-unlock + revert flow for the Immich Locked Folder (Phase D PR 4).

Implements the asymmetric privacy contract pinned by the threat model
(T09 + T10):

- The forward write (``visibility="locked"``) goes through the user's
  stored API key — see PR 3.
- The reverse path requires a Bearer-session that has been
  PIN-unlocked via ``POST /api/auth/session/unlock``. Only PR 4 walks
  that path. The PIN is forwarded from the request straight to Immich
  with no intermediate logging, no audit-log entry, and no DB row.
- The PIN-unlocked Bearer token survives only for the duration of the
  request handler. It is rebound to ``None`` in a ``finally`` block
  before the response is built, and is never persisted to
  ``state-v2.db``.

Per Phase A discovery (Immich v2.7.5), the unlocked session is
**still the same Bearer string** as before unlock — Immich flips an
internal flag on the session record. We therefore must not store the
post-unlock token; the original encrypted Bearer in ``sessions``
remains the long-lived credential.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

UNLOCK_PATH = "/api/auth/session/unlock"
LOCK_PATH = "/api/auth/session/lock"


class UnlockError(RuntimeError):
    """Base for PIN-unlock failures (mapped to HTTP statuses by the route)."""


class InvalidPin(UnlockError):
    pass


class UpstreamUnavailable(UnlockError):
    pass


@dataclass(frozen=True)
class RevertOutcome:
    reverted_count: int
    failed_asset_ids: tuple[str, ...]


def unlock_and_revert(
    *,
    immich_base_url: str,
    bearer: str,
    pin: str,
    asset_ids: list[str],
    client: httpx.Client | None = None,
    timeout: float = 10.0,
) -> RevertOutcome:
    """PIN-unlock the user's Immich session, revert each asset to
    ``visibility="timeline"``, then re-lock the session.

    The function never logs ``pin`` or ``bearer``. The caller is
    responsible for rebinding the bearer to ``None`` after this
    returns; this helper only refuses to persist either value
    anywhere itself.
    """

    if not bearer:
        raise UnlockError("missing bearer")
    if not pin:
        raise InvalidPin("missing pin")

    own_client = client is None
    if client is None:
        client = httpx.Client(base_url=immich_base_url, timeout=timeout)

    headers = {"Authorization": f"Bearer {bearer}"}
    try:
        try:
            unlock_resp = client.post(
                UNLOCK_PATH, headers=headers, json={"pinCode": pin}
            )
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable("upstream Immich unreachable") from exc

        if unlock_resp.status_code in (400, 401, 403):
            raise InvalidPin("Immich rejected the PIN")
        if unlock_resp.status_code not in (200, 201, 204):
            raise UnlockError(
                f"unexpected Immich unlock status {unlock_resp.status_code}"
            )

        reverted = 0
        failed: list[str] = []
        for asset_id in asset_ids:
            try:
                resp = client.put(
                    f"/api/assets/{asset_id}",
                    headers=headers,
                    json={"visibility": "timeline"},
                )
            except httpx.HTTPError:
                failed.append(asset_id)
                continue
            if resp.status_code in (200, 204):
                reverted += 1
            else:
                failed.append(asset_id)

        # Best-effort relock so the session does not stay PIN-unlocked.
        try:
            client.post(LOCK_PATH, headers=headers)
        except httpx.HTTPError:
            pass

        return RevertOutcome(
            reverted_count=reverted,
            failed_asset_ids=tuple(failed),
        )
    finally:
        if own_client:
            client.close()


__all__ = [
    "InvalidPin",
    "LOCK_PATH",
    "RevertOutcome",
    "UNLOCK_PATH",
    "UnlockError",
    "UpstreamUnavailable",
    "unlock_and_revert",
]
