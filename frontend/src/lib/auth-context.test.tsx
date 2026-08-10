import { afterEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import { AuthProvider, useAuth } from "./auth-context";
import { tokenStore } from "./api";
import { authApi } from "./resources";

vi.mock("./resources", () => ({
  authApi: { me: vi.fn() },
}));

const USER = {
  id: 1,
  organization_id: 1,
  email: "a@example.com",
  full_name: "A B",
  phone: null,
  role: "admin" as const,
  is_active: true,
  has_totp: false,
  last_login_at: null,
  created_at: "2026-01-01T00:00:00Z",
};

const TOKENS = { access_token: "access-1", refresh_token: "refresh-1", expires_in: 3600, user: USER };

const SESSION = {
  user: USER,
  organization_id: 1,
  organization_name: "Acme",
  home_organization_id: 1,
  home_organization_name: "Acme",
  role: "admin" as const,
  is_delegated: false,
  available_clients: [],
};

function Probe() {
  const { loading, isAuthenticated, user, session, setSession, logout } = useAuth();
  return (
    <div>
      <div data-testid="loading">{String(loading)}</div>
      <div data-testid="authed">{String(isAuthenticated)}</div>
      <div data-testid="user">{user?.full_name ?? "none"}</div>
      <div data-testid="session">{session?.organization_name ?? "none"}</div>
      <button onClick={() => setSession(TOKENS)}>sign in</button>
      <button onClick={logout}>log out</button>
    </div>
  );
}

describe("AuthProvider", () => {
  afterEach(() => {
    tokenStore.clear();
    vi.restoreAllMocks();
  });

  it("starts unauthenticated with no cached token", async () => {
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>
    );

    await waitFor(() => expect(screen.getByTestId("loading").textContent).toBe("false"));
    expect(screen.getByTestId("authed").textContent).toBe("false");
    expect(screen.getByTestId("user").textContent).toBe("none");
    expect(authApi.me).not.toHaveBeenCalled();
  });

  it("resolves the session for a cached token on load", async () => {
    tokenStore.setSession(TOKENS);
    vi.mocked(authApi.me).mockResolvedValue(SESSION);

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>
    );

    await waitFor(() => expect(screen.getByTestId("loading").textContent).toBe("false"));
    expect(screen.getByTestId("authed").textContent).toBe("true");
    expect(screen.getByTestId("session").textContent).toBe("Acme");
  });

  it("clears the session when the cached token is rejected", async () => {
    tokenStore.setSession(TOKENS);
    vi.mocked(authApi.me).mockRejectedValue(new Error("expired"));

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>
    );

    await waitFor(() => expect(screen.getByTestId("loading").textContent).toBe("false"));
    expect(screen.getByTestId("authed").textContent).toBe("false");
    expect(tokenStore.getAccess()).toBeNull();
  });

  it("setSession authenticates immediately and stores the tokens", async () => {
    vi.mocked(authApi.me).mockResolvedValue(SESSION);

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>
    );
    await waitFor(() => expect(screen.getByTestId("loading").textContent).toBe("false"));

    await act(async () => {
      screen.getByText("sign in").click();
    });

    expect(screen.getByTestId("authed").textContent).toBe("true");
    expect(screen.getByTestId("user").textContent).toBe("A B");
    expect(tokenStore.getAccess()).toBe("access-1");
  });

  it("logout clears the stored session and user state", async () => {
    tokenStore.setSession(TOKENS);
    vi.mocked(authApi.me).mockResolvedValue(SESSION);
    const originalLocation = window.location;
    // jsdom throws on a bare assignment to window.location.href in some
    // configurations; replacing the whole object sidesteps that rather than
    // asserting on a navigation jsdom does not actually perform.
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...originalLocation, href: "" },
    });

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>
    );
    await waitFor(() => expect(screen.getByTestId("authed").textContent).toBe("true"));

    act(() => {
      screen.getByText("log out").click();
    });

    expect(screen.getByTestId("authed").textContent).toBe("false");
    expect(tokenStore.getAccess()).toBeNull();
    expect(window.location.href).toBe("/login");

    Object.defineProperty(window, "location", { configurable: true, value: originalLocation });
  });

  it("useAuth throws outside a provider", () => {
    function Bare() {
      useAuth();
      return null;
    }
    // React logs its own error boundary noise for a thrown render; suppress it
    // so the expected failure doesn't read as a broken test run.
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => render(<Bare />)).toThrow("useAuth must be used within AuthProvider");
    spy.mockRestore();
  });
});
