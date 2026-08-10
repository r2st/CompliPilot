import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import SettingsPage from "./page";
import { useAuth } from "@/lib/auth-context";
import { authApi, organizationsApi } from "@/lib/resources";
import { ApiError } from "@/lib/api";
import type { UserRole } from "@/lib/types";

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn(),
}));

vi.mock("@/lib/resources", () => ({
  authApi: {
    changePassword: vi.fn(),
    totpSetup: vi.fn(),
    totpConfirm: vi.fn(),
    totpDisable: vi.fn(),
    listUsers: vi.fn().mockResolvedValue([]),
    createUser: vi.fn(),
  },
  organizationsApi: {
    me: vi.fn(),
    update: vi.fn(),
  },
}));

const ORG = {
  id: 1,
  name: "Acme Manufacturing",
  type: "company" as const,
  entity_type: "private_limited" as const,
  state: "Karnataka",
  industry: "Manufacturing",
  is_active: true,
  plan_tier: "smb" as const,
  created_at: "2026-01-01T00:00:00Z",
  legal_name: "Acme Manufacturing Pvt Ltd",
  gstin: null,
  pan: null,
  cin: null,
  llpin: null,
  tan: null,
  firm_registration_no: null,
  annual_turnover_paise: null,
  employee_count: null,
  incorporation_date: null,
  financial_year_end_month: 3,
  is_listed: false,
  has_foreign_investment: false,
  handles_personal_data: false,
  contact_email: null,
  contact_phone: null,
  address: null,
  updated_at: "2026-01-01T00:00:00Z",
};

function makeUser(overrides: Partial<{ has_totp: boolean; role: UserRole }> = {}) {
  return {
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
    ...overrides,
  };
}

const refreshSession = vi.fn();

function mockAuth(user: ReturnType<typeof makeUser> | null, session: unknown = null) {
  vi.mocked(useAuth).mockReturnValue({
    loading: false,
    isAuthenticated: !!user,
    user,
    session,
    setSession: vi.fn(),
    refreshSession,
    logout: vi.fn(),
  } as unknown as ReturnType<typeof useAuth>);
}

describe("SettingsPage", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  describe("OrganizationCard", () => {
    it("loads and displays the organization's profile", async () => {
      mockAuth(makeUser());
      vi.mocked(organizationsApi.me).mockResolvedValue(ORG);
      render(<SettingsPage />);

      expect(await screen.findByLabelText("Name")).toHaveValue("Acme Manufacturing");
      expect(screen.getByLabelText("Legal name")).toHaveValue("Acme Manufacturing Pvt Ltd");
    });

    it("shows an error if the profile fails to load", async () => {
      mockAuth(makeUser());
      vi.mocked(organizationsApi.me).mockRejectedValue(new ApiError(500, "server_error", "Could not load"));
      render(<SettingsPage />);

      expect(await screen.findByText("Could not load")).toBeInTheDocument();
    });

    it("saves edited fields, sending blanks as null and dropping unedited identifiers", async () => {
      mockAuth(makeUser());
      vi.mocked(organizationsApi.me).mockResolvedValue(ORG);
      vi.mocked(organizationsApi.update).mockResolvedValue(ORG);
      render(<SettingsPage />);

      await screen.findByLabelText("Name");
      fireEvent.change(screen.getByLabelText("Industry"), { target: { value: "" } });
      fireEvent.change(screen.getByLabelText("GSTIN"), { target: { value: "29ABCDE1234F1Z5" } });

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
      });

      await waitFor(() => expect(organizationsApi.update).toHaveBeenCalled());
      const payload = vi.mocked(organizationsApi.update).mock.calls[0][0];
      expect(payload.industry).toBeNull();
      expect(payload.gstin).toBe("29ABCDE1234F1Z5");
      expect(payload).not.toHaveProperty("pan");
      expect(await screen.findByText("Saved.")).toBeInTheDocument();
      expect(refreshSession).toHaveBeenCalled();
    });

    it("shows the server's error message when saving fails", async () => {
      mockAuth(makeUser());
      vi.mocked(organizationsApi.me).mockResolvedValue(ORG);
      vi.mocked(organizationsApi.update).mockRejectedValue(
        new ApiError(422, "validation_error", "Invalid GSTIN")
      );
      render(<SettingsPage />);

      await screen.findByLabelText("Name");
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
      });

      expect(await screen.findByText("Invalid GSTIN")).toBeInTheDocument();
    });

    it("notes when a delegated user is editing a client's profile", async () => {
      mockAuth(makeUser(), {
        is_delegated: true,
        organization_name: "Client Co",
        home_organization_name: "Sharma & Associates",
      });
      vi.mocked(organizationsApi.me).mockResolvedValue(ORG);
      render(<SettingsPage />);

      expect(
        await screen.findByText("Editing Client Co on behalf of Sharma & Associates.")
      ).toBeInTheDocument();
    });
  });

  describe("SecurityCard — password", () => {
    it("changes the password and clears the fields", async () => {
      mockAuth(makeUser());
      vi.mocked(organizationsApi.me).mockResolvedValue(ORG);
      vi.mocked(authApi.changePassword).mockResolvedValue({ message: "ok" });
      render(<SettingsPage />);

      await screen.findByLabelText("Name");
      fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "old-pw-12345" } });
      fireEvent.change(screen.getByLabelText("New password"), { target: { value: "new-pw-67890" } });

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Update password" }));
      });

      expect(authApi.changePassword).toHaveBeenCalledWith("old-pw-12345", "new-pw-67890");
      expect(await screen.findByText("Updated.")).toBeInTheDocument();
      expect(screen.getByLabelText("Current password")).toHaveValue("");
      expect(screen.getByLabelText("New password")).toHaveValue("");
    });

    it("shows the server's error message when the current password is wrong", async () => {
      mockAuth(makeUser());
      vi.mocked(organizationsApi.me).mockResolvedValue(ORG);
      vi.mocked(authApi.changePassword).mockRejectedValue(
        new ApiError(401, "invalid_credentials", "Current password is incorrect")
      );
      render(<SettingsPage />);

      await screen.findByLabelText("Name");
      fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "wrong" } });
      fireEvent.change(screen.getByLabelText("New password"), { target: { value: "new-pw-67890" } });

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Update password" }));
      });

      expect(await screen.findByText("Current password is incorrect")).toBeInTheDocument();
    });
  });

  describe("SecurityCard — two-factor authentication", () => {
    it("shows a disable form when two-factor is already enabled", async () => {
      mockAuth(makeUser({ has_totp: true }));
      vi.mocked(organizationsApi.me).mockResolvedValue(ORG);
      render(<SettingsPage />);

      expect(await screen.findByText("Two-factor authentication is enabled.")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Disable" })).toBeDisabled();
    });

    it("disables two-factor once a code is entered", async () => {
      mockAuth(makeUser({ has_totp: true }));
      vi.mocked(organizationsApi.me).mockResolvedValue(ORG);
      vi.mocked(authApi.totpDisable).mockResolvedValue({ message: "ok" });
      render(<SettingsPage />);

      await screen.findByText("Two-factor authentication is enabled.");
      fireEvent.change(screen.getByLabelText("Enter a code to disable it"), { target: { value: "123456" } });

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Disable" }));
      });

      expect(authApi.totpDisable).toHaveBeenCalledWith("123456");
      expect(refreshSession).toHaveBeenCalled();
    });

    it("walks through enrolment: begin, show the secret, then confirm", async () => {
      mockAuth(makeUser({ has_totp: false }));
      vi.mocked(organizationsApi.me).mockResolvedValue(ORG);
      vi.mocked(authApi.totpSetup).mockResolvedValue({
        secret: "JBSWY3DPEHPK3PXP",
        provisioning_uri: "otpauth://totp/CompliPilot",
      });
      vi.mocked(authApi.totpConfirm).mockResolvedValue({ message: "ok" });
      render(<SettingsPage />);

      await act(async () => {
        fireEvent.click(await screen.findByRole("button", { name: "Set up two-factor authentication" }));
      });

      expect(await screen.findByText("JBSWY3DPEHPK3PXP")).toBeInTheDocument();

      fireEvent.change(screen.getByLabelText("Enter the 6-digit code to confirm"), {
        target: { value: "654321" },
      });
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
      });

      expect(authApi.totpConfirm).toHaveBeenCalledWith("654321");
      expect(refreshSession).toHaveBeenCalled();
      // Back to the "not enabled" state until has_totp flips via a real session refresh.
      expect(screen.queryByText("JBSWY3DPEHPK3PXP")).not.toBeInTheDocument();
    });

    it("shows an error for an incorrect confirmation code", async () => {
      mockAuth(makeUser({ has_totp: false }));
      vi.mocked(organizationsApi.me).mockResolvedValue(ORG);
      vi.mocked(authApi.totpSetup).mockResolvedValue({
        secret: "JBSWY3DPEHPK3PXP",
        provisioning_uri: "otpauth://totp/CompliPilot",
      });
      vi.mocked(authApi.totpConfirm).mockRejectedValue(new ApiError(400, "bad_code", "Incorrect code"));
      render(<SettingsPage />);

      await act(async () => {
        fireEvent.click(await screen.findByRole("button", { name: "Set up two-factor authentication" }));
      });
      fireEvent.change(await screen.findByLabelText("Enter the 6-digit code to confirm"), {
        target: { value: "000000" },
      });
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
      });

      expect(await screen.findByText("Incorrect code")).toBeInTheDocument();
      // Still on the confirm step, not knocked back to "not enabled".
      expect(screen.getByText("JBSWY3DPEHPK3PXP")).toBeInTheDocument();
    });
  });

  describe("UsersCard", () => {
    it("is hidden from non-admins", async () => {
      mockAuth(makeUser({ role: "staff" }));
      vi.mocked(organizationsApi.me).mockResolvedValue(ORG);
      render(<SettingsPage />);

      await screen.findByLabelText("Name");
      expect(screen.queryByText("Users")).not.toBeInTheDocument();
      expect(authApi.listUsers).not.toHaveBeenCalled();
    });

    it("lists users for an admin, marking the current user", async () => {
      mockAuth(makeUser({ role: "admin" }));
      vi.mocked(organizationsApi.me).mockResolvedValue(ORG);
      vi.mocked(authApi.listUsers).mockResolvedValue([
        makeUser({ role: "admin" }),
        { ...makeUser({ role: "staff" }), id: 2, full_name: "Rahul Verma", email: "rahul@example.com", is_active: false },
      ]);
      render(<SettingsPage />);

      expect(await screen.findByText("Rahul Verma")).toBeInTheDocument();
      expect(screen.getByText("(you)")).toBeInTheDocument();
      expect(screen.getByText("Inactive")).toBeInTheDocument();
    });

    it("adds a user through the inline form and reloads the list", async () => {
      mockAuth(makeUser({ role: "admin" }));
      vi.mocked(organizationsApi.me).mockResolvedValue(ORG);
      vi.mocked(authApi.listUsers).mockResolvedValue([makeUser({ role: "admin" })]);
      vi.mocked(authApi.createUser).mockResolvedValue(makeUser({ role: "staff" }));
      render(<SettingsPage />);

      await screen.findByText("Users");
      fireEvent.click(screen.getByRole("button", { name: "Add user" }));

      fireEvent.change(screen.getByLabelText("Full name"), { target: { value: "New Hire" } });
      fireEvent.change(screen.getByLabelText("Email"), { target: { value: "new@example.com" } });
      fireEvent.change(screen.getByLabelText("Temporary password"), { target: { value: "temp-pw-12345" } });
      fireEvent.change(screen.getByLabelText("Role"), { target: { value: "compliance_manager" } });

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Add user" }));
      });

      expect(authApi.createUser).toHaveBeenCalledWith({
        full_name: "New Hire",
        email: "new@example.com",
        password: "temp-pw-12345",
        role: "compliance_manager",
      });
      // The form collapses back to the "Add user" toggle once creation succeeds.
      await waitFor(() => expect(screen.queryByLabelText("Temporary password")).not.toBeInTheDocument());
      expect(authApi.listUsers).toHaveBeenCalledTimes(2);
    });

    it("shows the server's error message when adding a user fails", async () => {
      mockAuth(makeUser({ role: "admin" }));
      vi.mocked(organizationsApi.me).mockResolvedValue(ORG);
      vi.mocked(authApi.listUsers).mockResolvedValue([makeUser({ role: "admin" })]);
      vi.mocked(authApi.createUser).mockRejectedValue(
        new ApiError(409, "conflict", "A user with that email already exists")
      );
      render(<SettingsPage />);

      await screen.findByText("Users");
      fireEvent.click(screen.getByRole("button", { name: "Add user" }));
      fireEvent.change(screen.getByLabelText("Full name"), { target: { value: "New Hire" } });
      fireEvent.change(screen.getByLabelText("Email"), { target: { value: "new@example.com" } });
      fireEvent.change(screen.getByLabelText("Temporary password"), { target: { value: "temp-pw-12345" } });

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Add user" }));
      });

      expect(await screen.findByText("A user with that email already exists")).toBeInTheDocument();
    });
  });
});
