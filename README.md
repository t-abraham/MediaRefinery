# MediaRefinery

**MediaRefinery** is a self-hosted, multi-user companion service for [Immich](https://immich.app/). It runs alongside your Immich instance, classifies media against user-defined categories, and applies reviewable policies (review albums, tags, Immich Locked Folder) — all on hardware you control. No media bytes leave your network for inference.

> **Warning:** This project handles **private** personal media. Treat API keys, logs, and reports as **sensitive**. The default design does **not** send media to third-party inference services. **No** architecture here is a substitute for legal or compliance review for your jurisdiction.

## Status

Pre-release. The v2 service is under active development. Authoritative scope, decisions, and threat model are in [docs/v2/](docs/v2/) and [docs/adr/ADR-0010-v2-service-architecture.md](docs/adr/ADR-0010-v2-service-architecture.md). No public release tag exists yet.

## Architecture

- **Backend:** FastAPI service with an in-process APScheduler worker. Login is proxied to Immich (passwords are never stored). Per-user encrypted Immich session tokens and optional API keys live in a SQLite state store; every persisted row carries a `user_id` for multi-tenant isolation.
- **Frontend:** React + Vite + TypeScript single-page app. Built statics ship in the same Docker image and are served by FastAPI under a strict CSP.
- **Hide semantics:** "Hide" maps to Immich's native Locked Folder via the `move_to_locked_folder` action. The Locked Folder PIN flows browser → Immich and never reaches the MediaRefinery backend.
- **Models:** No bundled weights. Models are downloaded at first run from a curated catalog ([docs/models/catalog.json](docs/models/catalog.json)) with explicit per-model SHA256 verification and license acceptance audit-logged.
- **Distribution:** Docker image only.

## Development quickstart

Backend:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,service,onnx]"
.\.venv\Scripts\python.exe -m pytest tests/service
```

Frontend:

```powershell
cd frontend
npm install
npm run typecheck
npm test
npm run build   # emits the static bundle into src/mediarefinery/web/
```

Run the service locally (requires `MR_IMMICH_BASE_URL`):

```powershell
$env:MR_IMMICH_BASE_URL = "https://immich.example.com"
.\.venv\Scripts\python.exe -m uvicorn --factory mediarefinery.service.app:create_app --host 0.0.0.0 --port 8080
```

Demo mode (synthetic data, no real Immich required):

```powershell
$env:MR_IMMICH_BASE_URL = "http://demo.invalid"
$env:MR_DEMO = "1"
.\.venv\Scripts\python.exe -m uvicorn --factory mediarefinery.service.app:create_app --host 0.0.0.0 --port 8080
```

## Documentation

| Area | Document |
|------|----------|
| Architecture decision | [docs/adr/ADR-0010-v2-service-architecture.md](docs/adr/ADR-0010-v2-service-architecture.md) |
| Threat model | [docs/v2/threat-model.md](docs/v2/threat-model.md) |
| Immich API compatibility | [docs/v2/immich-api-compat.md](docs/v2/immich-api-compat.md) |
| Operations | [docs/v2/operations.md](docs/v2/operations.md) |
| Model catalog | [docs/models/](docs/models/) |
| Module dependency map | [planning/dependency-map.md](planning/dependency-map.md) |
| Contributing | [CONTRIBUTING.md](CONTRIBUTING.md) |
| Security disclosure | [SECURITY.md](SECURITY.md) |

## What this tool will not do

- **No automatic deletion** of library assets.
- **No upload of media** to third-party cloud inference.
- **No bypass of Immich access control.**
- **No bundled classifier weights.**
- **No claim of perfect classifier accuracy** — results are probabilistic.

## License

See [LICENSE](LICENSE).
