import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import RegisterPage from "./page";
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
  authApi: { register: vi.fn() },
}));

const USER = {
  id: 1,
  organization_id: 1,
  email: "priya@example.com",
  full_name: "Priya Sharma",
  phone: null,
  role: "admin" as const,
  is_active: true,
  has_totp: false,
  last_login_at: null,
  created_at: "2026-01-01T00:00:00Z",
};

const TOKENS = { access_token: "access-1", refresh_token: "refresh-1", expires_in: 3600, user: USER };

const setSession = vi.fn();

function fillCommonFields() {
  fireEvent.change(screen.getByLabelText("Organization name"), { target: { value: "Acme Pvt Ltd" } });
  fireEvent.change(screen.getByLabelText("Full name"), { target: { value: "Priya Sharma" } });
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: "priya@example.com" } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "correct-horse-battery" } });
}

describe("RegisterPage", () => {
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
    return render(<RegisterPage />);
  }

  it("registers a company, defaulting a blank entity type to null, and redirects on success", async () => {
    vi.mocked(authApi.register).mockResolvedValue(TOKENS);
    setup();

    fillCommonFields();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Create organization" }));
    });

    await waitFor(() => expect(setSession).toHaveBeenCalledWith(TOKENS));
    expect(authApi.register).toHaveBeenCalledWith({
      organization_name: "Acme Pvt Ltd",
      organization_type: "company",
      entity_type: null,
      state: null,
      email: "priya@example.com",
      full_name: "Priya Sharma",
      phone: null,
      password: "correct-horse-battery",
    });
    expect(push).toHaveBeenCalledWith("/dashboard");
  });

  it("sends the chosen entity type for a company", async () => {
    vi.mocked(authApi.register).mockResolvedValue(TOKENS);
    setup();

    fillCommonFields();
    fireEvent.change(screen.getByLabelText("Entity type"), { target: { value: "llp" } });
    fireEvent.change(screen.getByLabelText("State"), { target: { value: "Maharashtra" } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Create organization" }));
    });

    await waitFor(() => expect(authApi.register).toHaveBeenCalled());
    expect(authApi.register).toHaveBeenCalledWith(
      expect.objectContaining({ entity_type: "llp", state: "Maharashtra" })
    );
  });

  it("hides the entity type field for a CA firm and always sends null for it", async () => {
    vi.mocked(authApi.register).mockResolvedValue(TOKENS);
    setup();

    fireEvent.change(screen.getByLabelText("We are a"), { target: { value: "ca_firm" } });
    expect(screen.queryByLabelText("Entity type")).not.toBeInTheDocument();

    fillCommonFields();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Create organization" }));
    });

    await waitFor(() => expect(authApi.register).toHaveBeenCalled());
    expect(authApi.register).toHaveBeenCalledWith(
      expect.objectContaining({ organization_type: "ca_firm", entity_type: null })
    );
  });

  it("shows the server's error message on a rejected registration", async () => {
    vi.mocked(authApi.register).mockRejectedValue(
      new ApiError(409, "conflict", "An account with that email already exists")
    );
    setup();

    fillCommonFields();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Create organization" }));
    });

    expect(await screen.findByText("An account with that email already exists")).toBeInTheDocument();
    expect(setSession).not.toHaveBeenCalled();
    expect(push).not.toHaveBeenCalled();
  });

  it("falls back to a generic message for a non-ApiError failure", async () => {
    vi.mocked(authApi.register).mockRejectedValue(new Error("network down"));
    setup();

    fillCommonFields();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Create organization" }));
    });

    expect(await screen.findByText("Could not create your organization")).toBeInTheDocument();
  });

  it("disables the submit button while submitting", async () => {
    let resolveRegister: (v: typeof TOKENS) => void = () => {};
    vi.mocked(authApi.register).mockReturnValue(
      new Promise((resolve) => {
        resolveRegister = resolve;
      })
    );
    setup();

    fillCommonFields();
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "Create organization" }));
    });

    const button = screen.getByRole("button", { name: "Creating…" });
    expect(button).toBeDisabled();

    await act(async () => resolveRegister(TOKENS));
    await waitFor(() => expect(setSession).toHaveBeenCalled());
  });
});
