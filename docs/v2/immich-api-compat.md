# MediaRefinery v2 — Immich API compatibility

**Status:** Phase A discovery complete. Endpoint shapes verified against a
running Immich instance on **2026-04-30**. This document records the exact
Immich endpoints v2 depends on, the verified request/response shapes, and
the **minimum supported Immich version** v2 pins.

This is verified before any v2 service code lands (per ADR-0010 and
ADR-0010) by exercising each endpoint against a live Immich
instance and recording the observed shapes. Probe scripts and raw
responses live under `tmp/probe/` (gitignored); only sanitized field
names and types are recorded here.

## Minimum supported Immich version

- **Minimum supported:** **`v2.7.5`** (verified 2026-04-30).
- **Maximum tested:** `v2.7.5`.
- **Detection:** the service calls `GET /api/server/version` at startup
  and on the readiness probe; major+minor are checked against the
  supported range.
- **Policy:** v2.x supports a defined Immich version range; deviations
  outside the range surface a doctor warning and refuse service startup
  if a load-bearing endpoint shape (auth/login, users/me, asset-PUT
  visibility, session/unlock) differs.

## Endpoints required by v2

For each endpoint: confirmed reachability, request/response shape, error
modes, and authentication context. Field names are recorded; values are
not. Where field names themselves identify a user (email, name) only the
field name and type are recorded.

| # | Endpoint | Used for | Verified | Notes |
|---|----------|----------|----------|-------|
| 1 | `POST /api/auth/login` | Proxy login (Phase B) | **✅ v2.7.5** | Body `{email: str, password: str}`. Returns **201** with `{accessToken, userId, userEmail, name, isAdmin, profileImagePath, shouldChangePassword, isOnboarded}`. Token TTL is not in the response — see §"Session lifetime" below. |
| 2 | `POST /api/auth/logout` | Invalidate upstream session on MR logout | **✅ v2.7.5** | Bearer auth. **200** with `{successful: bool, redirectUri: str}`. **Confirmed:** subsequent `GET /api/users/me` with the same token returns **401**, i.e. Immich invalidates server-side. |
| 3 | `GET /api/users/me` | Identify current user, role, capabilities | **✅ v2.7.5** | **200** with `{id, email, name, profileImagePath, avatarColor, profileChangedAt, storageLabel, shouldChangePassword, isAdmin, createdAt, deletedAt, updatedAt, oauthId, quotaSizeInBytes, quotaUsageInBytes, status, license}`. Works with both Bearer session token **and** `x-api-key` provided the key has the `user.read` scope (verified 2026-04-30). |
| 4 | `GET /api/users` | List all users (NOT admin-only) | **✅ v2.7.5** | **200** with an array of `{id, email, name, profileImagePath, avatarColor, profileChangedAt}` for **every Immich user**, returned to any caller holding `user.read` — not gated on `isAdmin`. **Privacy note for v2:** this endpoint is a built-in user-enumeration vector on Immich. v2 MUST NOT relay this endpoint to MR tenants; admin views in v2.1 should call it server-side and filter, not proxy it. Recorded against threat-model T05 / T06. |
| 5 | `POST /api/api-keys` | Mint key | **✅ v2.7.5** | Body `{name: str, permissions: [str, ...]}`. **201** with `{secret, apiKey: {id, name, createdAt, updatedAt, permissions}}` when the requested permissions are a subset of the caller's and the caller holds `apiKey.create`. **400 "Cannot grant permissions you do not have"** when permissions exceed the caller's. Cleanup via `DELETE /api/api-keys/{id}` requires `apiKey.delete` — verified that a key with only `apiKey.read` cannot delete keys it just minted. v2 dashboard does **not** call this directly — it deep-links to Immich UI per ADR-0010 §3. |
| 6 | `GET /api/server/version` / `/about` / `/features` / `/config` | Readiness checks | **✅ v2.7.5** | `version` → `{major, minor, patch}`. `about` includes `version` (e.g. `"v2.7.5"`), build, ffmpeg/libvips/imagemagick versions. `features` reports `passwordLogin`, `oauth`, `trash`, etc. Reused from v1. |
| 7 | `POST /api/search/metadata` | Asset listing | **✅ v2.7.5** | Body accepts `{size, visibility, ...}`. With `visibility: "locked"`, requests with `x-api-key` alone returned **401** — locked-folder reads require a **PIN-unlocked session** (see #13). v1-compatible for default visibility. |
| 8 | `GET /api/assets/{id}/thumbnail` | Preview bytes | v1-verified | Reused from v1; on-demand only, never cached server-side. |
| 9 | `GET/POST /api/albums`, `PUT /api/albums/{id}/assets` | Review-album action | v1-verified | Reused from v1. |
| 10 | `GET/POST /api/tags`, `PUT /api/tags/{id}/assets` | Tag action | v1-verified | Reused from v1. |
| 11 | **`PUT /api/assets/{id}` with `{visibility: "locked"}`** | `move_to_locked_folder` action (Phase D) | **✅ v2.7.5 — load-bearing** | Body `{visibility: "locked"}` returns **200** with the updated asset (`visibility="locked"`). **Works with `x-api-key`** that has `asset.update` — **no PIN required for the WRITE.** This is the canonical Locked Folder move endpoint on v2.7.5. |
| 11b | **`PUT /api/assets/{id}` with `{visibility: "timeline"}`** | Undo `move_to_locked_folder` | **✅ v2.7.5 — load-bearing** | **Important asymmetry:** revert requires a **Bearer session token whose session has been PIN-unlocked** via `POST /api/auth/session/unlock`. Calling with `x-api-key` returned **400 "Not found or no asset.update access"** — the API key cannot see the locked asset. Bulk-array form (`PUT /api/assets {ids: [...], visibility: ...}`) returned **400** on v2.7.5; per-asset PUT is the supported shape. |
| 12 | Locked Folder list / read | Verifying an asset reached Locked Folder during e2e tests | **✅ v2.7.5** | `POST /api/search/metadata {visibility: "locked"}` with a Bearer token whose session has been unlocked (see #13). E2E tests use a fixture user with a known PIN supplied via the smoke env (`IMMICH_TEST_USER_LOCKED_PIN`). |
| 13 | **Locked Folder PIN flow** | Per-user PIN; never reaches MR backend | **✅ v2.7.5** | Endpoints (Bearer auth, body sanitized): `POST /api/auth/pin-code {pinCode: "<6-digit numeric>"}` (set), `PUT /api/auth/pin-code {newPinCode: "<6-digit>", ...}` (change), `POST /api/auth/session/lock` (**204**, locks current session's lock-context), `POST /api/auth/session/unlock {pinCode}` or `{password}` (**204**, unlocks for the duration of the session). See §"Locked Folder PIN flow" and threat-model T09. |
| 14 | Outbound webhook / event stream for new assets | Optional auto-scan-on-upload (Phase G) | **❌ not exposed on v2.7.5** | `GET /api/server/features` does not advertise outbound webhooks; no documented `/api/webhooks/*` outbound endpoints. v2 falls back to scheduled polling via `POST /api/search/metadata` on an APScheduler cadence. Re-evaluate on Immich version bumps. |
| 15 | Asset archive endpoint | Decide whether to implement `archive` action | **Decision: drop from v2 public action vocabulary** | Locked Folder (`visibility=locked`) supersedes archive for hide-style flows. Archive remains accessible to users via Immich UI. v2 public action vocabulary: `tag`, `add_to_album`, `move_to_locked_folder`, `no_action`. |

## Auth header conventions

- **Bearer session token** (from `/api/auth/login`): `Authorization: Bearer <token>`. Required for `/users/me`, `/auth/logout`, all `/auth/session/*`, `/auth/pin-code`, and locked-folder reads/reverts.
- **API key**: `x-api-key: <key>`. Sufficient for v1-style scans (read assets, write tags/albums, set `visibility=locked`). Not sufficient for locked-folder reverts or locked-folder reads.

## Session lifetime

- The login response does not surface a TTL. Immich's session expiry is server-controlled and may extend with use.
- v2 revalidates each cached Immich session token by calling `GET /api/users/me` on a configurable interval (default 1 hour); on **401** the MR session is invalidated. This implements threat-model T15.
- On user logout from MR, MR calls `POST /api/auth/logout` upstream; **server-side invalidation is confirmed** for v2.7.5.

## Locked Folder PIN flow

Verified against Immich v2.7.5 (2026-04-30):

1. **Setting the PIN** — user does this **in the Immich UI**, not via MR. MR never accepts a PIN. If the API path is ever needed for an admin tool, it is `POST /api/auth/pin-code` with `{pinCode: "<6-digit numeric>"}` over HTTPS to Immich, requiring the user's Bearer session token. This call must be browser-to-Immich.
2. **Locking the session** — `POST /api/auth/session/lock` returns **204**. v2 calls this from the dashboard at the end of a Locked-Folder review screen so the next viewer must re-enter the PIN.
3. **Unlocking the session** — `POST /api/auth/session/unlock` accepts `{pinCode}` **or** `{password}`. Returns **204**. **The PIN flows browser → Immich directly using the user's Bearer session token; MR's backend never sees the PIN bytes.**
4. **Move TO locked folder** (forward direction) — performed by MR backend with the user's stored encrypted Immich token (or per-user API key). No PIN required, no session-unlock required. This is the only Immich-mutating Locked-Folder operation MR performs unattended.
5. **Move OUT of locked folder (Undo)** — requires a PIN-unlocked session. Two viable shapes:
   - **(Preferred)** the dashboard performs Undo: browser unlocks the session against Immich (PIN browser→Immich), then the dashboard issues `PUT /api/assets/{id} {visibility: "timeline"}` to Immich directly using the user's Bearer token (which MR may surface to the SPA for the duration of the Undo flow only — see threat-model T01/T09).
   - **(Fallback)** if the dashboard cannot reach Immich directly (CORS / network), the user performs Undo manually in Immich UI; MR records the intent in the audit log and marks the run as `undo_pending_user`.

This makes T09 ("Locked Folder PIN exposure to MediaRefinery") concrete: **the PIN never crosses the MR trust boundary**, and the MR backend is incapable of moving an asset *out* of Locked Folder.

## API-key permission scopes (observed on v2.7.5)

A v2.7.5 Immich API key carries fine-grained permissions. Verified
scope names (observed on the smoke key list endpoint, 2026-04-30):
`server.about`, `asset.read`, `asset.view`, `asset.update`,
`album.read`, `album.create`, `albumAsset.create`, `tag.read`,
`tag.create`, `tag.asset`, `apiKey.create`, `apiKey.read`,
`apiKey.update`, `apiKey.delete`, `user.read`. A round-trip
mint+delete on `POST /api/api-keys` and `DELETE /api/api-keys/{id}`
(204) was verified end-to-end.

For v2 service mode, per-user API keys (used for unattended scans)
need at minimum:

- `asset.read`, `asset.update` — scanning, tagging, album add, locked move (forward)
- `tag.read`, `tag.create`, `tag.asset` — tag action
- `album.read`, `album.create`, `albumAsset.create` — album action
- `user.read` — `/users/me` for user identity in unattended jobs **(load-bearing — without it, v2 cannot identify which tenant a scan belongs to)**
- `server.about` — readiness checks

For Undo / locked-folder reads, MR still uses the user's interactive
Bearer session token rather than the API key (the API key is
structurally unable to revert a `visibility:"locked"` write — see #11b).

## Verification procedure (recorded for reproducibility)

The probe script (`tmp/probe/probe.py`, gitignored) loads `.env.smoke`
and:

1. Hits `/api/server/{version,about,features,config}`.
2. Logs in with `{email, password}` against `/api/auth/login`.
3. Calls `/api/users/me` with both Bearer and `x-api-key`.
4. Lists API keys; attempts to mint a key (records the 400 envelope).
5. Pulls one asset via `/api/search/metadata`, then exercises
   `PUT /api/assets/{id}` with `visibility="locked"` and reverts.
6. Exercises `/api/auth/pin-code` and `/api/auth/session/{lock,unlock}`
   to record the validation shapes and confirm session-lock returns 204.
7. Logs out and re-checks `/users/me` to confirm server-side invalidation.

Raw responses are saved to `tmp/probe/raw/*.json` for engineer review;
only field names and types are committed to this document. No PII,
tokens, or PINs are committed.

## Compatibility policy

- v2 starts pinned to Immich **v2.7.5+** (single-version range at v2.0
  release). The range expands as we test newer Immich releases.
- A doctor warning appears when the connected Immich is outside the
  range.
- Service startup fails closed when a load-bearing endpoint (#1, #3,
  #11, #11b, #13) returns an unrecognized response shape.
- Compatibility breaks in Immich are tracked in this file and rolled
  into v2 minor releases.

## Out of scope

- We do not vendor or proxy Immich's Swagger/OpenAPI spec.
- We do not call undocumented Immich internals.
- We do not require admin permissions on the operator's Immich for
  per-user features (each user authorizes their own actions via
  proxy login or their own API key).
