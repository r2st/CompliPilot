import { apiRequest, apiUpload } from "./api";
import type {
  AlertSummary,
  AuditEntryResponse,
  BreachIncidentResponse,
  CalendarResponse,
  ClientResponse,
  ConsentRecordResponse,
  DashboardResponse,
  DataMapEntryResponse,
  DataSubjectRequestResponse,
  DocumentResponse,
  DocumentSummary,
  DocumentUploadResponse,
  DPDPReadinessResponse,
  FilingResponse,
  FilingStatus,
  FilingSummary,
  FilingTransitionResponse,
  FirmDashboardResponse,
  LoginResponse,
  ObligationResponse,
  OrganizationResponse,
  Page,
  PIAResponse,
  Regulation,
  RegulatoryImpactResponse,
  SessionContextResponse,
  TokenResponse,
  TotpSetupResponse,
  UserResponse,
} from "./types";

// --- Auth --------------------------------------------------------------

export const authApi = {
  login: (email: string, password: string, totp_code?: string) =>
    apiRequest<LoginResponse>("/auth/login", {
      method: "POST",
      auth: false,
      body: { email, password, totp_code },
    }),
  register: (payload: {
    organization_name: string;
    organization_type: "ca_firm" | "company";
    entity_type?: string | null;
    state?: string | null;
    email: string;
    full_name: string;
    phone?: string | null;
    password: string;
  }) => apiRequest<TokenResponse>("/auth/register", { method: "POST", auth: false, body: payload }),
  me: () => apiRequest<SessionContextResponse>("/auth/me"),
  switchClient: (client_org_id: number | null) =>
    apiRequest<TokenResponse>("/auth/switch-client", { method: "POST", body: { client_org_id } }),
  totpSetup: () => apiRequest<TotpSetupResponse>("/auth/totp/setup", { method: "POST" }),
  totpConfirm: (code: string) =>
    apiRequest<{ message: string }>("/auth/totp/confirm", { method: "POST", body: { code } }),
  totpDisable: (code: string) =>
    apiRequest<{ message: string }>("/auth/totp/disable", { method: "POST", body: { code } }),
  changePassword: (current_password: string, new_password: string) =>
    apiRequest<{ message: string }>("/auth/password", {
      method: "POST",
      body: { current_password, new_password },
    }),
  listUsers: () => apiRequest<UserResponse[]>("/auth/users"),
  createUser: (payload: {
    email: string;
    full_name: string;
    phone?: string | null;
    password: string;
    role: string;
  }) => apiRequest<UserResponse>("/auth/users", { method: "POST", body: payload }),
  updateUser: (id: number, payload: Partial<{ full_name: string; phone: string; role: string; is_active: boolean }>) =>
    apiRequest<UserResponse>(`/auth/users/${id}`, { method: "PATCH", body: payload }),
  deleteUser: (id: number) => apiRequest<{ message: string }>(`/auth/users/${id}`, { method: "DELETE" }),
};

// --- Dashboard ---------------------------------------------------------

export const dashboardApi = {
  get: (params?: { upcoming_days?: number; limit?: number }) =>
    apiRequest<DashboardResponse>("/dashboard", { query: params }),
  firm: (params?: { limit?: number }) =>
    apiRequest<FirmDashboardResponse>("/dashboard/firm", { query: params }),
};

// --- Calendar ------------------------------------------------------------

export const calendarApi = {
  get: (params?: {
    start?: string;
    end?: string;
    regulation?: Regulation;
    include_closed?: boolean;
    all_clients?: boolean;
  }) => apiRequest<CalendarResponse>("/calendar", { query: params }),
};

// --- Filings -------------------------------------------------------------

export const filingsApi = {
  list: (params?: {
    limit?: number;
    offset?: number;
    regulation?: Regulation;
    status?: FilingStatus;
    only_open?: boolean;
    search?: string;
  }) => apiRequest<Page<FilingSummary>>("/filings", { query: params }),
  get: (id: number) => apiRequest<FilingResponse>(`/filings/${id}`),
  update: (id: number, payload: Record<string, unknown>) =>
    apiRequest<FilingResponse>(`/filings/${id}`, { method: "PATCH", body: payload }),
  transition: (
    id: number,
    payload: { status: FilingStatus; acknowledgement_no?: string; rejection_reason?: string; notes?: string }
  ) => apiRequest<FilingTransitionResponse>(`/filings/${id}/transition`, { method: "POST", body: payload }),
  create: (payload: Record<string, unknown>) =>
    apiRequest<FilingResponse>("/filings", { method: "POST", body: payload }),
};

// --- Obligations -----------------------------------------------------------

export const obligationsApi = {
  list: (params?: {
    limit?: number;
    offset?: number;
    regulation?: Regulation;
    search?: string;
  }) => apiRequest<Page<ObligationResponse>>("/obligations", { query: params }),
  get: (id: number) => apiRequest<ObligationResponse>(`/obligations/${id}`),
};

// --- Clients ---------------------------------------------------------------

export const clientsApi = {
  list: (params?: { limit?: number; offset?: number; status?: string; search?: string }) =>
    apiRequest<Page<ClientResponse>>("/clients", { query: params }),
  get: (id: number) => apiRequest<ClientResponse>(`/clients/${id}`),
  create: (payload: Record<string, unknown>) =>
    apiRequest<ClientResponse>("/clients", { method: "POST", body: payload }),
  update: (id: number, payload: Record<string, unknown>) =>
    apiRequest<ClientResponse>(`/clients/${id}`, { method: "PATCH", body: payload }),
};

// --- Documents ---------------------------------------------------------

export const documentsApi = {
  list: (params?: {
    limit?: number;
    offset?: number;
    type?: string;
    parse_status?: string;
    filing_id?: number;
  }) => apiRequest<Page<DocumentSummary>>("/documents", { query: params }),
  get: (id: number) => apiRequest<DocumentResponse>(`/documents/${id}`),
  upload: (file: File, extra?: { type?: string; title?: string; regulation?: string }) =>
    apiUpload<DocumentUploadResponse>("/documents", file, extra),
  update: (id: number, payload: Record<string, unknown>) =>
    apiRequest<DocumentResponse>(`/documents/${id}`, { method: "PATCH", body: payload }),
  remove: (id: number) => apiRequest<void>(`/documents/${id}`, { method: "DELETE" }),
};

// --- Alerts ------------------------------------------------------------

export const alertsApi = {
  summary: () => apiRequest<AlertSummary>("/alerts/summary"),
  impacts: (params?: { limit?: number; offset?: number; impact_level?: string; unacknowledged_only?: boolean }) =>
    apiRequest<Page<RegulatoryImpactResponse>>("/alerts/regulatory", { query: params }),
  acknowledge: (id: number, notes?: string) =>
    apiRequest<RegulatoryImpactResponse>(`/alerts/regulatory/${id}/acknowledge`, {
      method: "POST",
      body: { notes },
    }),
};

// --- Audit ---------------------------------------------------------------

export const auditApi = {
  list: (params?: { limit?: number; offset?: number; entity_type?: string; action?: string }) =>
    apiRequest<Page<AuditEntryResponse>>("/audit", { query: params }),
};

// --- DPDP toolkit ----------------------------------------------------------

export const dpdpApi = {
  readiness: () => apiRequest<DPDPReadinessResponse>("/dpdp/readiness"),

  consents: {
    list: (params?: { limit?: number; offset?: number; purpose?: string; granted_only?: boolean; principal_ref?: string }) =>
      apiRequest<Page<ConsentRecordResponse>>("/dpdp/consents", { query: params }),
    create: (payload: Record<string, unknown>) =>
      apiRequest<ConsentRecordResponse>("/dpdp/consents", { method: "POST", body: payload }),
    withdraw: (id: number, reason?: string) =>
      apiRequest<ConsentRecordResponse>(`/dpdp/consents/${id}/withdraw`, { method: "POST", body: { reason } }),
  },

  dataMap: {
    list: (params?: { limit?: number; offset?: number; sensitive_only?: boolean; cross_border_only?: boolean }) =>
      apiRequest<Page<DataMapEntryResponse>>("/dpdp/data-map", { query: params }),
    create: (payload: Record<string, unknown>) =>
      apiRequest<DataMapEntryResponse>("/dpdp/data-map", { method: "POST", body: payload }),
    update: (id: number, payload: Record<string, unknown>) =>
      apiRequest<DataMapEntryResponse>(`/dpdp/data-map/${id}`, { method: "PATCH", body: payload }),
    remove: (id: number) => apiRequest<{ message: string }>(`/dpdp/data-map/${id}`, { method: "DELETE" }),
  },

  breaches: {
    list: (params?: { limit?: number; offset?: number; open_only?: boolean; unnotified_only?: boolean }) =>
      apiRequest<Page<BreachIncidentResponse>>("/dpdp/breaches", { query: params }),
    create: (payload: Record<string, unknown>) =>
      apiRequest<BreachIncidentResponse>("/dpdp/breaches", { method: "POST", body: payload }),
    update: (id: number, payload: Record<string, unknown>) =>
      apiRequest<BreachIncidentResponse>(`/dpdp/breaches/${id}`, { method: "PATCH", body: payload }),
    notify: (id: number, payload: { dpb_reference?: string; notification_text?: string; notify_principals?: boolean }) =>
      apiRequest<BreachIncidentResponse>(`/dpdp/breaches/${id}/notify`, { method: "POST", body: payload }),
  },

  requests: {
    list: (params?: { limit?: number; offset?: number; request_type?: string; open_only?: boolean; overdue_only?: boolean }) =>
      apiRequest<Page<DataSubjectRequestResponse>>("/dpdp/requests", { query: params }),
    create: (payload: Record<string, unknown>) =>
      apiRequest<DataSubjectRequestResponse>("/dpdp/requests", { method: "POST", body: payload }),
    update: (id: number, payload: Record<string, unknown>) =>
      apiRequest<DataSubjectRequestResponse>(`/dpdp/requests/${id}`, { method: "PATCH", body: payload }),
  },

  assessments: {
    list: (params?: { limit?: number; offset?: number; status?: string }) =>
      apiRequest<Page<PIAResponse>>("/dpdp/assessments", { query: params }),
    create: (payload: Record<string, unknown>) =>
      apiRequest<PIAResponse>("/dpdp/assessments", { method: "POST", body: payload }),
    review: (id: number, nextReviewMonths?: number) =>
      apiRequest<PIAResponse>(`/dpdp/assessments/${id}/review`, {
        method: "POST",
        query: nextReviewMonths ? { next_review_months: nextReviewMonths } : undefined,
      }),
  },
};

// --- Organizations -------------------------------------------------------

export const organizationsApi = {
  me: () => apiRequest<OrganizationResponse>("/organizations/me"),
  update: (payload: Record<string, unknown>) =>
    apiRequest<OrganizationResponse>("/organizations/me", { method: "PATCH", body: payload }),
};
