import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import LoginPage from "../pages/LoginPage";

describe("LoginPage", () => {
  beforeEach(() => {
    document.cookie = "";
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the form fields and a labelled submit button", () => {
    render(<LoginPage />);
    expect(screen.getByLabelText(/Immich URL/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Username/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Password/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Sign in/i })).toBeInTheDocument();
  });

  it("posts credentials to /api/v1/auth/login with credentials:include", async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => new Response(null, { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    render(<LoginPage />);
    await userEvent.type(screen.getByLabelText(/Immich URL/i), "https://immich.example.com");
    await userEvent.type(screen.getByLabelText(/Username/i), "alice");
    await userEvent.type(screen.getByLabelText(/Password/i), "hunter2");
    fireEvent.click(screen.getByRole("button", { name: /Sign in/i }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const call = fetchMock.mock.calls[0]!;
    const url = call[0];
    const init = call[1]!;
    expect(url).toBe("/api/v1/auth/login");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("include");
    const body = JSON.parse(init.body as string);
    expect(body).toEqual({
      immich_url: "https://immich.example.com",
      username: "alice",
      password: "hunter2",
    });
    expect(await screen.findByRole("status")).toHaveTextContent(/Signed in/);
  });

  it("does not persist password to localStorage or sessionStorage", async () => {
    const fetchMock = vi.fn(async () => new Response(null, { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    render(<LoginPage />);
    await userEvent.type(screen.getByLabelText(/Immich URL/i), "https://immich.example.com");
    await userEvent.type(screen.getByLabelText(/Username/i), "alice");
    await userEvent.type(screen.getByLabelText(/Password/i), "hunter2");
    fireEvent.click(screen.getByRole("button", { name: /Sign in/i }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    const ls = JSON.stringify({ ...localStorage });
    const ss = JSON.stringify({ ...sessionStorage });
    expect(ls).not.toContain("hunter2");
    expect(ss).not.toContain("hunter2");
  });

  it("shows the invalid-credentials error on 401", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(null, { status: 401 })),
    );

    render(<LoginPage />);
    await userEvent.type(screen.getByLabelText(/Immich URL/i), "https://immich.example.com");
    await userEvent.type(screen.getByLabelText(/Username/i), "alice");
    await userEvent.type(screen.getByLabelText(/Password/i), "wrong");
    fireEvent.click(screen.getByRole("button", { name: /Sign in/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/Invalid/i);
  });

  it("shows the unreachable error when fetch throws", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("network down");
      }),
    );

    render(<LoginPage />);
    await userEvent.type(screen.getByLabelText(/Immich URL/i), "https://immich.example.com");
    await userEvent.type(screen.getByLabelText(/Username/i), "alice");
    await userEvent.type(screen.getByLabelText(/Password/i), "hunter2");
    fireEvent.click(screen.getByRole("button", { name: /Sign in/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/Could not reach/i);
  });
});
