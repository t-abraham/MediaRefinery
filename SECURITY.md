# Security policy

## Supported versions

Security updates are best-effort and align with the latest **default branch** and the most recent **tagged** release, once the project ships releases. This section will be updated with a version table when publishable versions exist.

Current pre-v1 status: GitHub private vulnerability reporting is enabled for
this public repository as of 2026-04-29. No public stable release has been
tagged yet.

Sprint 014 historical status: no public stable release had been tagged yet, and
a public security contact or host-platform private reporting path was recorded
as a release-manager blocker. This blocker was resolved before Sprint 023 by
enabling GitHub private vulnerability reporting.

Sprint 017 historical status: no real public reporting contact or
host-platform private vulnerability reporting path had been provided. The
project kept that blocker open rather than inventing a placeholder contact for
release notes or public docs.

Sprint 018 status: the public v1 classifier stance is `noop`-only and adds no
model artifacts or production backend dependencies. The public vulnerability
reporting path was still unresolved at that time and was later resolved before
Sprint 023.

Sprint 019 status: the public v1 real HTTP action stance is review-album only.
No production tag/archive mutation client, live smoke detail, log, state,
secret, media byte, thumbnail, or frame capture was added. The public
vulnerability reporting path was still unresolved at that time and was later
resolved before Sprint 023.

Sprint 020 status: the first production local classifier backend is
operator-provided ONNX. The repository still ships no model weights, datasets,
private media, thumbnails, or frame captures. Model files must be mounted or
provided by the operator, and backend failures are tested to avoid logging or
persisting model bytes, media bytes, full private paths, or secrets. The public
vulnerability reporting path was still unresolved at that time and was later
resolved before Sprint 023.

Sprint 021 status: production HTTP tag writes are implemented for Immich
through tag find/create/add endpoints with dry-run mutation barriers, mocked
HTTP tests, and sanitized action errors. The repository still ships no smoke
credentials, server details, logs, state, private media, thumbnails, frame
captures, datasets, model weights, delete/trash support, or archive expansion.
Live tag smoke was completed during Sprint 022 after maintainers granted the
disposable smoke key tag creation permission. The public vulnerability
reporting path was later resolved before Sprint 023.

Sprint 022 status: full local verification, mock/default smoke, live doctor,
real HTTP dry-run, live review-album writes, live tag writes, and archive
fail-closed checks passed with local-only ignored smoke files. No API key value,
smoke server detail, media bytes, thumbnails, frame captures, private paths,
model weights, datasets, delete/trash support, archive expansion, release tag,
or official image was added. The public vulnerability reporting path is resolved
through GitHub private vulnerability reporting.

## Reporting a vulnerability

**Please do not** file a public GitHub issue for **undisclosed** security vulnerabilities in this repository.

1. Use GitHub's **Report a vulnerability** flow for this repository, which opens a private vulnerability report to the maintainers.
2. Include: short description, affected component (if known), reproduction steps, impact assessment, and your preferred disclosure timeline.
3. We aim to **acknowledge** within a few business days and work toward a **coordinated disclosure** (fix + advisory + release) before public details.

## Scope (intended)

- The **application** and **default** **Docker/Compose** examples in this repository.
- **Out of scope (typically):** third-party services (e.g. Immich itself), operator misconfiguration, stolen credentials at the user’s site—still report if our **documentation** or **defaults** are dangerously wrong.

## Secure defaults and expectations

- **No secrets in git**; use environment variables and secret files.
- **No user media in logs** (see [docs/v2/threat-model.md](docs/v2/threat-model.md)).
- **Local-first** processing for classification in default designs; cloud APIs not required by default.

## Coordination

After a fix is available, we may request **CVE** assignment and publish a **security advisory** on the host platform, plus notes in the changelog.

## Non-security issues

Bugs that are not security-sensitive can be reported as **public issues** with label `type:bug`.
