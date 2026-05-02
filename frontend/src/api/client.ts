// Thin fetch wrapper for the MediaRefinery service API.
//
// Privacy contract:
//   - We never persist the password or any other credential to
//     localStorage / sessionStorage / IndexedDB. The only state the
//     browser keeps after login is the cookies the server sets
//     (signed session + CSRF), which is correct for HttpOnly auth.
//   - Every request sends `credentials: "include"` so cookies travel
//     on same-origin and the dev-proxy origin.
//   - State-changing requests echo the CSRF cookie value into the
//     X-CSRF-Token header (double-submit pattern matched by the
//     backend require_csrf dependency).

export interface LoginPayload {
  immich_url: string;
  username: string;
  password: string;
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: "invalid" | "unreachable" | "generic",
    message: string,
  ) {
    super(message);
  }
}

function readCookie(name: string): string | null {
  const prefix = `${name}=`;
  for (const part of document.cookie.split(";")) {
    const trimmed = part.trim();
    if (trimmed.startsWith(prefix)) {
      return decodeURIComponent(trimmed.slice(prefix.length));
    }
  }
  return null;
}

export async function login(payload: LoginPayload): Promise<void> {
  let resp: Response;
  try {
    resp = await fetch("/api/v1/auth/login", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (err) {
    throw new ApiError(0, "unreachable", (err as Error).message);
  }
  if (resp.ok) return;
  if (resp.status === 401 || resp.status === 400) {
    throw new ApiError(resp.status, "invalid", "invalid credentials");
  }
  throw new ApiError(resp.status, "generic", `unexpected status ${resp.status}`);
}

export function csrfHeader(): Record<string, string> {
  const token = readCookie("mr_csrf");
  return token ? { "X-CSRF-Token": token } : {};
}

// Single state-changing fetch wrapper. Always credentials:include, always
// echoes the mr_csrf cookie via X-CSRF-Token (double-submit pattern).
// Reused by every wizard mutation so the CSRF rule has one home.
export async function csrfFetch(
  url: string,
  init: RequestInit = {},
): Promise<Response> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  for (const [k, v] of Object.entries(csrfHeader())) {
    headers.set(k, v);
  }
  return fetch(url, { ...init, credentials: "include", headers });
}

export interface BootstrapStatus {
  terms_accepted: boolean;
  users_exist: boolean;
  admin_present: boolean;
  ready: boolean;
}

export async function getBootstrap(): Promise<BootstrapStatus> {
  const r = await fetch("/api/v1/setup/bootstrap", {
    credentials: "include",
  });
  if (!r.ok) throw new ApiError(r.status, "generic", "bootstrap status failed");
  return (await r.json()) as BootstrapStatus;
}

export async function postBootstrap(): Promise<void> {
  const r = await csrfFetch("/api/v1/setup/bootstrap", {
    method: "POST",
    body: JSON.stringify({ accept_terms: true }),
  });
  if (!r.ok && r.status !== 409) {
    throw new ApiError(r.status, "generic", `bootstrap failed (${r.status})`);
  }
}

export interface MeResponse {
  user_id: string;
  email: string;
  name: string | null;
  is_admin: boolean;
}

export async function getMe(): Promise<MeResponse | null> {
  const r = await fetch("/api/v1/me", { credentials: "include" });
  if (r.status === 401) return null;
  if (!r.ok) throw new ApiError(r.status, "generic", `me failed (${r.status})`);
  return (await r.json()) as MeResponse;
}

export interface CatalogModel {
  id: string;
  name: string;
  kind: string;
  status: string;
  license: string;
  license_url: string | null;
  size_bytes: number;
  sha256: string;
  presets: string[];
  installed: boolean;
  installable: boolean;
}

export async function getCatalog(): Promise<CatalogModel[]> {
  const r = await fetch("/api/v1/models/catalog", { credentials: "include" });
  if (!r.ok) throw new ApiError(r.status, "generic", `catalog failed (${r.status})`);
  const data = (await r.json()) as { models: CatalogModel[] };
  return data.models;
}

export interface InstalledModel {
  id: number;
  name: string;
  version: string;
  sha256: string;
  license: string | null;
  active: boolean;
  present_on_disk: boolean;
}

export async function getInstalledModels(): Promise<InstalledModel[]> {
  const r = await fetch("/api/v1/models", { credentials: "include" });
  if (!r.ok) throw new ApiError(r.status, "generic", `models failed (${r.status})`);
  const data = (await r.json()) as { installed: InstalledModel[] };
  return data.installed;
}

export async function installModel(modelId: string): Promise<void> {
  const r = await csrfFetch("/api/v1/models/install", {
    method: "POST",
    body: JSON.stringify({ model_id: modelId, license_accepted: true }),
  });
  if (!r.ok) {
    throw new ApiError(r.status, "generic", `install failed (${r.status})`);
  }
}

export interface ScanResponse {
  run_id: number;
  status: string;
}

export async function startScan(): Promise<ScanResponse> {
  const r = await csrfFetch("/api/v1/scans", { method: "POST" });
  if (!r.ok) {
    throw new ApiError(r.status, "generic", `scan failed (${r.status})`);
  }
  return (await r.json()) as ScanResponse;
}
