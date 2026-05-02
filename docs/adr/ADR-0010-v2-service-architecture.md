# ADR-0010: v2 service architecture and skip of v1 CLI tag

## Status

Accepted (supersedes ADR-0009)

## Context

ADR-0009 set the public v1 target as a real-product CLI: operator-supplied
ONNX classifier plus real Immich tag writes, packaged as a versioned Python
CLI. By Sprint 023 that target was implementation-ready and verified end to
end. The maintainer has now rejected shipping a CLI-only public release on
the grounds that:

- The v1 CLI audience overlaps the intended v2 audience (self-hosted Immich
  operators), so two consecutive public debuts split attention.
- The CLI's UX assumes operators willing to write YAML, source ONNX models,
  and run scans by hand. That excludes the broader self-hosting audience the
  project ultimately targets.
- Burning the `1.x` version line on a CLI release would force later versioning
  contortions when the dashboard product lands.

The desired public release is a multi-user web service with a dashboard,
distributed exclusively as a Docker image, that mirrors Immich logins and
moves flagged media into Immich's native Locked Folder.

The existing pipeline modules (`scanner`, `extractor`, `classifier`,
`decision`, `actions`, `state`, `reporter`) are well tested (146 tests,
explicit privacy gate in CI) and will be reused as a library by the new
service layer rather than rewritten.

## Decision

The public v1 CLI tag is **not** released. The next public release is **v2**,
delivered as a multi-user web service in a single Docker image. Concretely:

1. **No `1.x.x` tag is created.** The current CLI code remains in the
   repository as a contributor and power-user entry point but is not the
   primary release artifact. Version planning resumes with the v2 release.

2. **v2 architecture** is a FastAPI service plus a React/Vite single-page
   dashboard, both shipped inside one Docker image. The service runs
   APScheduler in-process for background scans. The existing pipeline
   modules are imported as a library and are not rewritten.

3. **Authentication** is login-via-Immich proxy. The dashboard accepts a
   user's Immich URL, username, and password, forwards the credentials to
   Immich `POST /auth/login`, persists the returned session token encrypted
   at rest, and discards the password. MediaRefinery does not store, hash,
   or otherwise retain user passwords. Optional long-lived Immich API keys
   may be stored (encrypted) per user to authorize unattended background
   scans.

4. **Hide semantics** are Immich's native Locked Folder. The
   `move_to_locked_folder` action sets the asset visibility to `locked` via
   the Immich API. The PIN required to view Locked Folder content flows
   browser-to-Immich and is never sent to or stored by MediaRefinery. Every
   move is reversible from the dashboard via Undo.

5. **No bundled model weights.** The Docker image ships no classifier model.
   On first run, the operator selects a model from a curated catalog
   (`docs/models/catalog.json`) pinning name, URL, SHA256, license text, and
   recommended thresholds. The wizard downloads, verifies the hash, and
   records license acceptance in the audit log. This preserves the legal
   posture established by ADR-0007 while removing the operator-side ONNX
   sourcing burden.

6. **Distribution is Docker-only** for end users. No PyPI publication. The
   `pip install -e .` workflow remains documented for contributors.

7. **Multi-tenancy** is enforced at the persistence layer. Every existing
   table gains a `user_id` column; new tables (`users`, `sessions`,
   `audit_log`, `model_registry`, `user_api_keys`) are added. v2 starts on a
   fresh `state-v2.db`; no in-place migration from v1's `state.db` is
   provided.

8. **Audit logging** is required for every action that mutates Immich state
   or user-visible MediaRefinery state. Locked Folder moves, Undo
   invocations, model installs, license acceptances, and admin actions are
   first-class audit entries with actor, target, before/after, and reason.

9. **Threat model is a release-blocker artifact**, drafted before service
   code lands (see `docs/v2/threat-model.md`). It covers cookie theft,
   credential interception, model-download MITM, multi-tenant data leakage,
   Locked Folder PIN exposure, and webhook spoofing. Each threat names its
   mitigation and the phase that implements it.

10. **Minimum supported Immich version is pinned** before service code
    lands, after concrete verification of the endpoints v2 depends on
    (`/auth/login`, `/users`, `/users/me`, `/api-keys`, asset
    visibility/Locked Folder, optional asset webhooks). Findings are
    recorded in `docs/v2/immich-api-compat.md`.

## Consequences

- ADR-0009 is superseded. Its acceptance criteria no longer gate a release.
- ADR-0007 (no bundled models) and ADR-0008 (action stance) remain in force
  and are reinforced by this ADR's catalog-download model and Locked Folder
  semantics.
- ADR-0005 (no auto-delete) remains in force. `move_to_locked_folder` is
  not a delete; it is a reversible visibility change.
- The Sprint 023 release-execution checklist is retired. Phase A through
  Phase H of the v2 plan replace it.
- `planning/dependency-map.md` is updated to include the service layer,
  frontend, scheduler, audit log, and model lifecycle as new modules.
- `README.md` is held in its current pre-release posture until v2 is
  implemented; no claims about a dashboard or multi-user support are made
  publicly until the corresponding code lands.
- The current CLI continues to work for contributors and power users but
  is not advertised as a public artifact.
- A new module layout is introduced: `src/mediarefinery/service/` for
  FastAPI code, `src/mediarefinery/web/` for built frontend assets shipped
  in the wheel, and a top-level `frontend/` directory for Vite source.

## Acceptance

- This ADR is merged on `main` before any v2 service code lands.
- ADR-0009's status is updated in the same change set to record it as
  superseded by ADR-0010.
- `planning/dependency-map.md` is updated in the same change set or in a
  follow-up PR explicitly tracked in `planning/progress-log.md`.
- `docs/v2/threat-model.md` and `docs/v2/immich-api-compat.md` exist as at
  least skeletons before Phase B (service skeleton) work begins.
- No `1.x.x` tag is created on the repository.
