import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "../App";
import Wizard from "../pages/Wizard";

// Catalog returned by the mocked /api/v1/models/catalog.
const CATALOG = {
  models: [
    {
      id: "demo-model",
      name: "Demo Model",
      kind: "image",
      status: "supported",
      license: "Apache-2.0",
      license_url: "https://example.invalid/license",
      size_bytes: 1234567,
      sha256: "abc".repeat(21) + "a", // 64 chars
      presets: [],
      installed: false,
      installable: true,
    },
  ],
};

interface FetchCall {
  url: string;
  init?: RequestInit;
}

function setupFetchMock() {
  const calls: FetchCall[] = [];
  let bootstrapReady = false;
  let authed = false;
  let installedCount = 0;

  const handler = async (url: RequestInfo | URL, init?: RequestInit) => {
    const u = typeof url === "string" ? url : url.toString();
    calls.push({ url: u, init });
    const method = (init?.method ?? "GET").toUpperCase();

    if (u.endsWith("/api/v1/setup/bootstrap") && method === "GET") {
      return new Response(
        JSON.stringify({
          terms_accepted: bootstrapReady,
          users_exist: bootstrapReady,
          admin_present: bootstrapReady,
          ready: bootstrapReady,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    if (u.endsWith("/api/v1/setup/bootstrap") && method === "POST") {
      bootstrapReady = true;
      return new Response(
        JSON.stringify({ terms_accepted: true, accepted_at: "now" }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    if (u.endsWith("/api/v1/me")) {
      if (!authed) return new Response(null, { status: 401 });
      return new Response(
        JSON.stringify({
          user_id: "u1",
          email: "alice@x.invalid",
          name: "Alice",
          is_admin: true,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    if (u.endsWith("/api/v1/models/catalog")) {
      return new Response(JSON.stringify(CATALOG), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (u.endsWith("/api/v1/models") && method === "GET") {
      const installed =
        installedCount === 0
          ? []
          : [
              {
                id: 1,
                name: "Demo Model",
                version: "demo-model",
                sha256: CATALOG.models[0]!.sha256,
                license: "Apache-2.0",
                active: true,
                present_on_disk: true,
              },
            ];
      return new Response(JSON.stringify({ installed }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (u.endsWith("/api/v1/models/install") && method === "POST") {
      installedCount += 1;
      return new Response(
        JSON.stringify({
          id: 1,
          model_id: "demo-model",
          name: "Demo Model",
          sha256: CATALOG.models[0]!.sha256,
          active: true,
        }),
        { status: 201, headers: { "Content-Type": "application/json" } },
      );
    }
    if (u.endsWith("/api/v1/scans") && method === "POST") {
      return new Response(
        JSON.stringify({ run_id: 42, status: "running" }),
        { status: 202, headers: { "Content-Type": "application/json" } },
      );
    }
    return new Response(null, { status: 404 });
  };

  const fetchMock = vi.fn(handler);
  vi.stubGlobal("fetch", fetchMock);
  return {
    calls,
    fetchMock,
    setBootstrap: (v: boolean) => {
      bootstrapReady = v;
    },
    setAuthed: (v: boolean) => {
      authed = v;
    },
  };
}

beforeEach(() => {
  document.cookie = "mr_csrf=test-csrf-token";
  localStorage.clear();
  sessionStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
  document.cookie = "mr_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT";
});

describe("Wizard — setup phase (App routing)", () => {
  it("renders welcome and bootstrap when bootstrap.ready=false", async () => {
    setupFetchMock();
    render(<App />);
    expect(await screen.findByRole("heading", { name: /Welcome/i })).toBeInTheDocument();
  });

  it("posts /setup/bootstrap with credentials and CSRF header", async () => {
    const { calls } = setupFetchMock();
    render(<App />);
    await screen.findByRole("heading", { name: /Welcome/i });

    // Step 1: consent + Continue
    await userEvent.click(screen.getByRole("checkbox"));
    await userEvent.click(screen.getByRole("button", { name: /Continue/i }));

    // Step 2: Record and continue
    fireEvent.click(screen.getByRole("button", { name: /Record and continue/i }));

    await waitFor(() => {
      const post = calls.find(
        (c) =>
          c.url.endsWith("/api/v1/setup/bootstrap") &&
          (c.init?.method ?? "GET") === "POST",
      );
      expect(post).toBeDefined();
      expect(post!.init!.credentials).toBe("include");
      const headers = new Headers(post!.init!.headers);
      expect(headers.get("X-CSRF-Token")).toBe("test-csrf-token");
      const body = JSON.parse(post!.init!.body as string);
      expect(body).toEqual({ accept_terms: true });
    });
  });
});

describe("Wizard — install phase (steps 3-7 happy path)", () => {
  it("walks catalog → license → install → scan → done", async () => {
    const fx = setupFetchMock();
    fx.setBootstrap(true);
    fx.setAuthed(true);

    render(<App />);

    // Step 3 — catalog: select the only model and continue.
    await screen.findByRole("heading", { name: /Choose a classifier model/i });
    await userEvent.click(screen.getByRole("radio"));
    await userEvent.click(screen.getByRole("button", { name: /Continue/i }));

    // Step 4 — license: install button should not exist yet (we're on
    // license step), but Continue is gated by the accept checkbox.
    await screen.findByRole("heading", { name: /Review licence/i });
    expect(screen.getByText(fx.calls[0]!.url ? CATALOG.models[0]!.sha256 : "")).toBeInTheDocument();
    const cont = screen.getByRole("button", { name: /Continue/i });
    expect(cont).toBeDisabled();
    await userEvent.click(screen.getByRole("checkbox"));
    expect(cont).not.toBeDisabled();
    await userEvent.click(cont);

    // Step 5 — install.
    await screen.findByRole("heading", { name: /Install model/i });
    await userEvent.click(screen.getByRole("button", { name: /^Install$/i }));
    await screen.findByText(/Model installed/i);
    await userEvent.click(screen.getByRole("button", { name: /Continue/i }));

    // Step 6 — scan.
    await screen.findByRole("heading", { name: /Run a first scan/i });
    await userEvent.click(screen.getByRole("button", { name: /Start dry-run scan/i }));
    await screen.findByText(/run #42/i);
    await userEvent.click(screen.getByRole("button", { name: /Continue/i }));

    // Step 7 — done.
    await screen.findByRole("heading", { name: /All set/i });

    // Every state-changing fetch sent the CSRF header + credentials.
    const mutations = fx.calls.filter(
      (c) =>
        (c.init?.method ?? "GET") !== "GET" &&
        !c.url.endsWith("/api/v1/setup/bootstrap"),
    );
    expect(mutations.length).toBeGreaterThanOrEqual(2);
    for (const c of mutations) {
      expect(c.init!.credentials).toBe("include");
      const headers = new Headers(c.init!.headers);
      expect(headers.get("X-CSRF-Token")).toBe("test-csrf-token");
    }
  });
});

describe("Wizard — direct mount", () => {
  it("license install is disabled until acceptance checkbox is ticked", async () => {
    setupFetchMock();
    const { rerender } = render(<Wizard phase="install" />);
    // Pick the model.
    await screen.findByRole("heading", { name: /Choose a classifier model/i });
    await userEvent.click(screen.getByRole("radio"));
    await userEvent.click(screen.getByRole("button", { name: /Continue/i }));

    await screen.findByRole("heading", { name: /Review licence/i });
    const cont = screen.getByRole("button", { name: /Continue/i });
    expect(cont).toBeDisabled();
    rerender(<Wizard phase="install" />);
  });
});

describe("Wizard — privacy", () => {
  it("never writes wizard state to localStorage or sessionStorage", async () => {
    const fx = setupFetchMock();
    fx.setBootstrap(true);
    fx.setAuthed(true);

    render(<App />);
    await screen.findByRole("heading", { name: /Choose a classifier model/i });
    await userEvent.click(screen.getByRole("radio"));
    await userEvent.click(screen.getByRole("button", { name: /Continue/i }));
    await screen.findByRole("heading", { name: /Review licence/i });
    await userEvent.click(screen.getByRole("checkbox"));
    await userEvent.click(screen.getByRole("button", { name: /Continue/i }));

    expect(JSON.stringify({ ...localStorage })).toBe("{}");
    expect(JSON.stringify({ ...sessionStorage })).toBe("{}");
  });
});
