# MediaRefinery v2 — threat model

**Status:** Skeleton (Phase A draft). Each threat is enumerated with a
mitigation and the phase that implements it. The model is hardened in
Phase B–G as code lands; final form is a release-blocker for v2.0.

This is **not** a formal security audit. It is the engineering checklist
the project uses to keep the dashboard's attack surface small and to
make the load-bearing privacy guarantees auditable.

## Scope

- **In scope:** the v2 service (FastAPI app + APScheduler worker), the
  React dashboard, the SQLite state DB, the model artifacts on disk, the
  Immich proxy-login flow, per-user Immich API keys stored at rest, and
  the Locked Folder write path.
- **Out of scope:** the security of the Immich instance itself, the
  reverse proxy / TLS termination layer (we document operator
  responsibilities), the operator's host OS, and the operator's network.

## Trust boundaries

1. **Browser ⇄ Service** — assumed traversed over HTTPS, terminated at a
   reverse proxy operated by the deployer. Cookies are `Secure;
   HttpOnly; SameSite=Lax`.
2. **Service ⇄ Immich** — over HTTPS to the operator-configured Immich
   URL. The service holds per-user encrypted Immich session tokens
   and/or API keys.
3. **Service ⇄ disk (`/data`)** — SQLite DB, encrypted token blobs,
   model files, master key. 0600 file modes; container runs non-root.
4. **Service ⇄ model-catalog origin** — outbound HTTPS to a pinned URL
   set with SHA256 verification.

## Threats and mitigations

| # | Threat | Mitigation | Phase |
|---|--------|------------|-------|
| T01 | Session cookie theft (XSS, malicious extension) | `HttpOnly; Secure; SameSite=Lax`; strict CSP with **no inline script** and no third-party origins (Phase E PR 1 ships the policy in `service/web.py`; `'unsafe-inline'` is allowed for **styles only** to accommodate Headless UI / React inline `style` props — script execution stays locked); CSRF token on every state-changing endpoint; logout invalidates server-side session. | B, E |
| T02 | Credential interception during login | Service requires HTTPS; `MR_REQUIRE_TLS=1` rejects plaintext requests when not behind a trusted proxy; password is held in memory only long enough to forward to Immich `/auth/login`, then zeroed. | B |
| T03 | Brute-force against `/api/v1/auth/login` | Per-IP rate limit (5/min default) and per-username rate limit; exponential backoff after repeated failures; failures logged to audit without password material. | B |
| T04 | Stored Immich token / API key exposure | Encrypted at rest with `MR_MASTER_KEY` (AES-256-GCM); master key from env or `/data/master.key` with 0600; rotation procedure documented; never logged. | B, G |
| T05 | Multi-tenant data leakage (user A reads user B's data) | Every persisted row carries `user_id`; every query goes through a `with_user(user_id)` helper; e2e test asserts isolation by attacking the API directly. | B |
| T06 | Privilege escalation (regular user becomes admin) | Role stored server-side, never in cookie; admin-only routes guarded by a single dependency; audit log captures every role change. | B |
| T07 | Model-download MITM | Catalog pins SHA256 per model; download fails closed on hash mismatch; HTTPS-only URLs; license text is part of the catalog and shown for explicit acceptance. | C |
| T08 | Malicious model artifact (supply-chain) | Catalog is curated and pinned; new entries require a code review touching `docs/models/catalog.json`; runtime imports of model code are not supported (ONNX-only inference). | C |
| T09 | Locked Folder PIN exposure to MediaRefinery | **Forward write (lock):** uses the user's stored `x-api-key`; no PIN required. **Reverse path (unlock + revert):** Phase D PR 4 added `POST /api/v1/me/locked-folder/unlock`, which **proxies** the PIN from the browser to Immich's `POST /api/auth/session/unlock` using the user's decrypted Bearer session. The PIN is held only in the request handler's local frame, **never logged**, **never written to the audit log**, **never persisted to `state-v2.db`**, and the variable is rebound to `None` in a `finally` block before the response is built. The PIN-unlocked Bearer is the same string Immich already issued — MR does not store a post-unlock token. The route is rate-limited and CSRF-protected like every other state-changing route. **Why proxy instead of browser-direct:** Immich's PIN endpoints require the user's Bearer, which is encrypted at rest under `MR_MASTER_KEY`; surfacing it to the browser would defeat T04. The proxy is the smaller compromise. See `locked_folder.py` and `immich-api-compat.md` §"Locked Folder PIN flow". | A ✅, D |
| T10 | Wrongful Locked Folder move (model false positive locks user's library) | Every move is reversible via `POST /api/v1/scans/{run_id}/undo`; dashboard always shows "this run will lock N items" pre-flight; default new-user policy maps Locked Folder to manual_review unless the user opts in. | D, E |
| T11 | Webhook spoofing (fake "new asset" events) | `POST /api/v1/webhooks/immich` requires HMAC signature using a per-deployment shared secret; replay window enforced. | G |
| T12 | Resource exhaustion via runaway scans | Per-user concurrency cap (one scan at a time); per-user daily quota; APScheduler queue bounded; healthcheck reports queue depth. | B, G |
| T13 | PII / media bytes in logs or reports | Existing privacy CI gate extended to service responses; structured logs with allow-listed keys; reporter never embeds bytes; assertion test reads logs and fails on data-URI/base64 markers. | B, G |
| T14 | Audit log tampering | Audit table is append-only at the application layer; SQLite WAL plus filesystem permissions; admins can read but not edit; export to CSV preserves original timestamps. | B |
| T15 | Account takeover via stale session after Immich password change | Service revalidates the Immich token on a configurable interval (default 1h) by calling Immich `/users/me`; on 401 the MR session is invalidated. | B, G |
| T16 | Demo mode leaks into production | `MR_DEMO=1` requires the absence of `MR_MASTER_KEY` and a synthetic-data flag; the startup log prints a banner; CI rejects images with both demo and prod env signals. | B✅ (gate), E (fixtures) |
| T17 | Backup contains decryptable secrets | Backups exclude `master.key` by default; restore documents the operator-side rekey procedure; `VACUUM INTO` snapshots are encrypted at rest only if the operator wraps the volume. | G |
| T18 | Cross-site request forgery on state-changing routes | CSRF double-submit cookie pattern; SameSite cookies; OPTIONS preflight enforced on cross-origin requests. | B |
| T19 | Open redirect / SSRF via Immich URL field | Immich URL stored per-deployment, not per-user; admin-only setting; outbound requests from the service are restricted to the configured host plus the model-catalog origins. | C |
| T20 | Account deletion does not actually purge | `DELETE /api/v1/me` test asserts: zero rows in any user-scoped table, zero entries in audit_log for that user_id (or anonymized to `user_deleted`), zero session records, zero stored API keys. | E, G |

## Defenses always-on

- HTTPS-only (enforced by config flag when not behind a trusted proxy).
- Structured JSON logs with no media bytes, no PIN, no password, no
  decrypted token.
- One source of truth for each secret (env > `/data/master.key`; never
  duplicated).
- Privacy CI gate (existing for v1, extended to service in Phase B).
- Lint and type checks (Phase G adds `ruff` + `mypy`).

## Open items (resolved during Phase A)

- **✅ Locked Folder API surface (Immich v2.7.5):** the canonical move
  is `PUT /api/assets/{id} {visibility:"locked"}` with `x-api-key`;
  reverting requires a Bearer session token whose session has been
  PIN-unlocked via `POST /api/auth/session/unlock`. Bulk
  `PUT /api/assets {ids:[...], visibility:...}` is **not** supported on
  v2.7.5 (returned 400). Recorded in `immich-api-compat.md` row #11/#11b.
- **✅ PIN flow direction:** browser → MR backend → Immich. The PIN
  is proxied (not stored) by `POST /api/v1/me/locked-folder/unlock`
  because reaching Immich's `POST /api/auth/session/unlock` requires
  the user's Bearer token, which is encrypted at rest under
  `MR_MASTER_KEY` and must not leave the server (T04). The PIN is
  held only in the route handler's local frame, never logged, never
  audited, never persisted, and rebound to `None` in `finally`. T09
  above codifies the resulting contract.
- **✅ Session token TTL:** not surfaced by `/api/auth/login`. v2
  revalidates Immich tokens by polling `GET /api/users/me` on a
  configurable interval (default 1 h); on **401** the MR session is
  invalidated. Implements T15.
- **✅ Upstream logout:** `POST /api/auth/logout` is server-side
  invalidating on Immich v2.7.5 (subsequent `/users/me` with the same
  token returns 401). MR logout calls it.
- **✅ Outbound webhooks for new assets:** not exposed on Immich
  v2.7.5. v2 falls back to scheduled polling. T11 (webhook spoofing)
  remains forward-looking — it will apply if/when Immich gains
  outbound webhooks or when a deployer wires an external uploader.

## Review cadence

- **Phase A:** open items resolved, T09/T15 made concrete.
- **End of Phase B:** T01–T06, T12–T15, T18 verified by test or audit.
- **End of Phase D:** T09–T11 verified end to end.
- **End of Phase G:** all rows verified; threat model frozen for v2.0
  release.
- **Post-release:** revisit on every minor release that touches auth,
  audit, or Immich integration.
