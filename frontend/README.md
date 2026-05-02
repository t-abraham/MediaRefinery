# MediaRefinery frontend (Phase E)

React + Vite + TypeScript dashboard. Built statics are emitted into
`../src/mediarefinery/web/` and served by FastAPI in production. In
development, run the FastAPI service on `:8080` and `npm run dev` here
on `:5173` — Vite proxies `/api` to the service.

## Commands

```
npm install
npm run dev          # http://localhost:5173 (proxies /api to :8080)
npm run build        # writes static bundle into src/mediarefinery/web/
npm test             # vitest run
npm run typecheck
```

## Privacy notes

- No third-party CDNs, fonts, analytics, or trackers. CSP enforces this.
- Passwords are held in component state only long enough to POST to
  `/api/v1/auth/login`, then wiped. Nothing credential-shaped touches
  `localStorage` / `sessionStorage` / `IndexedDB`.
- PINs (Phase E PR 4) follow the same form-only contract and are sent
  exclusively to the backend `POST /api/v1/me/locked-folder/unlock`
  endpoint, which forwards to Immich without logging or persistence.
