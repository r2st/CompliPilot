import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import LoginPage from "./page";
import { useAuth } from "@/lib/auth-context";
import { authApi } from "@/lib/resources";
import { ApiError } from "@/lib/api";

const push = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
}));

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn(),
}));

vi.mock("@/lib/resources", () => ({
  authApi: { login: vi.fn() },
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

const setSession = vi.fn();

function fillAndSubmit(email: string, password: string, submitLabel = "Sign in") {
  fireEvent.change(screen.getByPlaceholderText("you@company.com"), { target: { value: email } });
  fireEvent.change(screen.getByPlaceholderText("••••••••"), { target: { value: password } });
  fireEvent.click(screen.getByRole("button", { name: submitLabel }));
}

describe("LoginPage", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  function setup() {
    vi.mocked(useAuth).mockReturnValue({
      loading: false,
      isAuthenticated: false,
      user: null,
      session: null,
      setSession,
      refreshSession: vi.fn(),
      logout: vi.fn(),
    } as unknown as ReturnType<typeof useAuth>);
    return render(<LoginPage />);
  }

  it("signs in and redirects to the dashboard on success", async () => {
    vi.mocked(authApi.login).mockResolvedValue(TOKENS);
    setup();

    await act(async () => fillAndSubmit("a@example.com", "correct-horse"));

    await waitFor(() => expect(setSession).toHaveBeenCalledWith(TOKENS));
    expect(authApi.login).toHaveBeenCalledWith("a@example.com", "correct-horse", undefined);
    expect(push).toHaveBeenCalledWith("/dashboard");
  });

  it("shows the server's error message on a rejected login", async () => {
    vi.mocked(authApi.login).mockRejectedValue(
      new ApiError(401, "invalid_credentials", "Incorrect email or password")
    );
    setup();

    await act(async () => fillAndSubmit("a@example.com", "wrong"));

    expect(await screen.findByText("Incorrect email or password")).toBeInTheDocument();
    expect(setSession).not.toHaveBeenCalled();
    expect(push).not.toHaveBeenCalled();
  });

  it("falls back to a generic message for a non-ApiError failure", async () => {
    vi.mocked(authApi.login).mockRejectedValue(new Error("network down"));
    setup();

    await act(async () => fillAndSubmit("a@example.com", "correct-horse"));

    expect(await screen.findByText("Could not sign in")).toBeInTheDocument();
  });

  it("switches to a TOTP prompt instead of signing in, and submits the code on the next attempt", async () => {
    vi.mocked(authApi.login).mockResolvedValueOnce({ totp_required: true, challenge_token: "chal-1" });
    setup();

    await act(async () => fillAndSubmit("a@example.com", "correct-horse"));

    expect(await screen.findByLabelText("Authenticator code")).toBeInTheDocument();
    expect(setSession).not.toHaveBeenCalled();
    // The email/password fields are gone; only the TOTP field remains.
    expect(screen.queryByPlaceholderText("you@company.com")).not.toBeInTheDocument();

    vi.mocked(authApi.login).mockResolvedValueOnce(TOKENS);
    await act(async () => {
      fireEvent.change(screen.getByLabelText("Authenticator code"), { target: { value: "123456" } });
      fireEvent.click(screen.getByRole("button", { name: "Verify" }));
    });

    await waitFor(() => expect(setSession).toHaveBeenCalledWith(TOKENS));
    expect(authApi.login).toHaveBeenLastCalledWith("a@example.com", "correct-horse", "123456");
  });

  it("disables the submit button and shows progress text while submitting", async () => {
    let resolveLogin: (v: typeof TOKENS) => void = () => {};
    vi.mocked(authApi.login).mockReturnValue(
      new Promise((resolve) => {
        resolveLogin = resolve;
      })
    );
    setup();

    act(() => fillAndSubmit("a@example.com", "correct-horse"));

    const button = screen.getByRole("button", { name: "Signing in…" });
    expect(button).toBeDisabled();

    await act(async () => resolveLogin(TOKENS));
    await waitFor(() => expect(setSession).toHaveBeenCalled());
  });
});
