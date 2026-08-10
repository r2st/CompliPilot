import { afterEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import { ClientSwitcher } from "./client-switcher";
import { useAuth } from "@/lib/auth-context";
import { authApi } from "@/lib/resources";
import { ApiError } from "@/lib/api";

const push = vi.fn();
const refresh = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, refresh }),
}));

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn(),
}));

vi.mock("@/lib/resources", () => ({
  authApi: { switchClient: vi.fn() },
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

const setSession = vi.fn();

function mockSession(session: unknown) {
  vi.mocked(useAuth).mockReturnValue({
    loading: false,
    isAuthenticated: true,
    user: USER,
    session,
    setSession,
    refreshSession: vi.fn(),
    logout: vi.fn(),
  } as unknown as ReturnType<typeof useAuth>);
}

describe("ClientSwitcher", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("renders nothing meaningful without a session", () => {
    mockSession(null);
    const { container } = render(<ClientSwitcher />);
    expect(container.querySelector("select")).toBeNull();
  });

  it("shows just the org name for a user with no client access", () => {
    mockSession({
      is_delegated: false,
      available_clients: [],
      organization_name: "Acme Manufacturing",
      home_organization_name: "Acme Manufacturing",
      organization_id: 1,
    });
    render(<ClientSwitcher />);
    expect(screen.getByText("Acme Manufacturing")).toBeInTheDocument();
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("shows a select for a CA firm member, defaulting to their own firm", () => {
    mockSession({
      is_delegated: false,
      available_clients: [{ client_id: 1, organization_id: 5, name: "Client Co", entity_type: null, status: "active" }],
      organization_name: "Sharma & Associates",
      home_organization_name: "Sharma & Associates",
      organization_id: 2,
    });
    render(<ClientSwitcher />);
    const select = screen.getByRole("combobox") as HTMLSelectElement;
    expect(select.value).toBe("home");
    expect(screen.getByText("Client Co")).toBeInTheDocument();
  });

  it("reflects the client currently acted for when delegated", () => {
    mockSession({
      is_delegated: true,
      available_clients: [{ client_id: 1, organization_id: 5, name: "Client Co", entity_type: null, status: "active" }],
      organization_name: "Client Co",
      home_organization_name: "Sharma & Associates",
      organization_id: 5,
    });
    render(<ClientSwitcher />);
    const select = screen.getByRole("combobox") as HTMLSelectElement;
    expect(select.value).toBe("5");
  });

  it("switches context, updates the session and navigates to the dashboard", async () => {
    mockSession({
      is_delegated: false,
      available_clients: [{ client_id: 1, organization_id: 5, name: "Client Co", entity_type: null, status: "active" }],
      organization_name: "Sharma & Associates",
      home_organization_name: "Sharma & Associates",
      organization_id: 2,
    });
    vi.mocked(authApi.switchClient).mockResolvedValue({
      access_token: "new-access",
      refresh_token: "new-refresh",
      expires_in: 3600,
      user: USER,
    });

    render(<ClientSwitcher />);
    const select = screen.getByRole("combobox");

    await act(async () => {
      (select as HTMLSelectElement).value = "5";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });

    await waitFor(() => expect(authApi.switchClient).toHaveBeenCalledWith(5));
    expect(setSession).toHaveBeenCalled();
    expect(push).toHaveBeenCalledWith("/dashboard");
    expect(refresh).toHaveBeenCalled();
  });

  it("shows an error message when the switch fails", async () => {
    mockSession({
      is_delegated: false,
      available_clients: [{ client_id: 1, organization_id: 5, name: "Client Co", entity_type: null, status: "active" }],
      organization_name: "Sharma & Associates",
      home_organization_name: "Sharma & Associates",
      organization_id: 2,
    });
    vi.mocked(authApi.switchClient).mockRejectedValue(
      new ApiError(403, "forbidden", "Engagement has ended")
    );

    render(<ClientSwitcher />);
    const select = screen.getByRole("combobox");

    await act(async () => {
      (select as HTMLSelectElement).value = "5";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });

    await waitFor(() => expect(screen.getByText("Engagement has ended")).toBeInTheDocument());
    expect(push).not.toHaveBeenCalled();
  });
});
