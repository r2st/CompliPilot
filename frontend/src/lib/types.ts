/** Mirrors backend/app/models/enums.py. Values are the wire format. */

export type OrgType = "ca_firm" | "company";

export type EntityType =
  | "private_limited"
  | "public_limited"
  | "one_person_company"
  | "llp"
  | "partnership"
  | "proprietorship"
  | "trust"
  | "society"
  | "section_8"
  | "foreign_company"
  | "huf";

export type Regulation =
  | "gst"
  | "income_tax"
  | "rbi"
  | "sebi"
  | "mca"
  | "fema"
  | "labor"
  | "dpdp";

export type Frequency =
  | "monthly"
  | "quarterly"
  | "half_yearly"
  | "annual"
  | "event_based"
  | "one_time";

export type FilingStatus =
  | "not_started"
  | "draft"
  | "in_review"
  | "approved"
  | "submitted"
  | "acknowledged"
  | "rejected"
  | "late_filed"
  | "not_applicable";

export type ImpactLevel = "critical" | "high" | "medium" | "low" | "none";

export type UserRole = "admin" | "compliance_manager" | "staff" | "read_only";

export const ROLE_LABEL: Record<UserRole, string> = {
  admin: "Admin",
  compliance_manager: "Compliance Manager",
  staff: "Staff",
  read_only: "Read only",
};

export const ROLE_RANK: Record<UserRole, number> = {
  admin: 3,
  compliance_manager: 2,
  staff: 1,
  read_only: 0,
};

export type NotificationChannel = "whatsapp" | "email" | "sms" | "in_app";
export type NotificationStatus = "pending" | "sent" | "failed" | "skipped";

export type DocumentType =
  | "regulatory_notice"
  | "circular"
  | "show_cause_notice"
  | "filing_attachment"
  | "generated_filing"
  | "consent_record"
  | "other";

export type ParseStatus = "pending" | "processing" | "parsed" | "failed";

export type AuditAction =
  | "create"
  | "update"
  | "soft_delete"
  | "read"
  | "login"
  | "login_failed"
  | "logout"
  | "submit"
  | "approve"
  | "reject"
  | "generate"
  | "export"
  | "role_change"
  | "notify";

export type EngagementType =
  | "full_compliance"
  | "gst_only"
  | "audit_only"
  | "advisory"
  | "custom";

export type EngagementStatus = "active" | "paused" | "terminated";

export type PlanTier = "smb" | "professional" | "ca_firm" | "enterprise";

// --- Common envelopes ------------------------------------------------------

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    details?: Record<string, unknown>;
  };
}

// --- Auth --------------------------------------------------------------

export interface UserResponse {
  id: number;
  organization_id: number;
  email: string;
  full_name: string;
  phone: string | null;
  role: UserRole;
  is_active: boolean;
  has_totp: boolean;
  last_login_at: string | null;
  created_at: string;
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  expires_in: number;
  user: UserResponse;
  totp_required?: undefined;
}

export interface TotpChallengeResponse {
  totp_required: true;
  challenge_token: string;
}

export type LoginResponse = TokenResponse | TotpChallengeResponse;

export function isTotpChallenge(r: LoginResponse): r is TotpChallengeResponse {
  return (r as TotpChallengeResponse).totp_required === true;
}

export interface ClientSummary {
  client_id: number;
  organization_id: number;
  name: string;
  entity_type: EntityType | null;
  status: EngagementStatus;
}

export interface SessionContextResponse {
  user: UserResponse;
  organization_id: number;
  organization_name: string;
  home_organization_id: number;
  home_organization_name: string;
  role: UserRole;
  is_delegated: boolean;
  available_clients: ClientSummary[];
}

export interface TotpSetupResponse {
  secret: string;
  provisioning_uri: string;
}

// --- Organization --------------------------------------------------------

export interface OrganizationSummary {
  id: number;
  name: string;
  type: OrgType;
  entity_type: EntityType | null;
  state: string | null;
  industry: string | null;
  is_active: boolean;
  plan_tier: PlanTier;
  created_at: string;
}

export interface OrganizationResponse extends OrganizationSummary {
  legal_name: string | null;
  gstin: string | null;
  pan: string | null;
  cin: string | null;
  llpin: string | null;
  tan: string | null;
  firm_registration_no: string | null;
  annual_turnover_paise: number | null;
  employee_count: number | null;
  incorporation_date: string | null;
  financial_year_end_month: number;
  is_listed: boolean;
  has_foreign_investment: boolean;
  handles_personal_data: boolean;
  contact_email: string | null;
  contact_phone: string | null;
  address: string | null;
  updated_at: string;
}

export interface ClientResponse {
  id: number;
  ca_firm_id: number;
  client_org_id: number;
  engagement_type: EngagementType;
  status: EngagementStatus;
  start_date: string;
  end_date: string | null;
  assigned_user_id: number | null;
  retainer_paise: number | null;
  notes: string | null;
  created_at: string;
  client_organization: OrganizationSummary | null;
  open_filings: number | null;
  overdue_filings: number | null;
}

// --- Filings ---------------------------------------------------------------

export interface FilingSummary {
  id: number;
  organization_id: number;
  obligation_id: number;
  regulation: Regulation;
  filing_type: string | null;
  period_key: string;
  period_start: string | null;
  period_end: string | null;
  due_date: string;
  extended_due_date: string | null;
  status: FilingStatus;
  is_ai_generated: boolean;
  created_at: string;
  title: string | null;
  effective_due_date: string | null;
  days_until_due: number | null;
  urgency: string | null;
  is_open: boolean | null;
}

export interface FilingResponse extends FilingSummary {
  data_json: Record<string, unknown> | null;
  template_id: number | null;
  ai_model: string | null;
  ai_generated_at: string | null;
  ai_confidence: number | null;
  ai_notes: string | null;
  prepared_by_id: number | null;
  reviewed_by_id: number | null;
  reviewed_at: string | null;
  submitted_at: string | null;
  submitted_by_id: number | null;
  acknowledgement_no: string | null;
  rejection_reason: string | null;
  tax_payable_paise: number | null;
  tax_paid_paise: number | null;
  penalty_paise: number | null;
  late_fee_paise: number | null;
  notes: string | null;
  updated_at: string;
  allowed_transitions: FilingStatus[];
  obligation_code: string | null;
  obligation_title: string | null;
  penalty_description: string | null;
}

export interface FilingTransitionResponse {
  filing: FilingResponse;
  previous_status: FilingStatus;
  recorded_as_late: boolean;
}

// --- Calendar ----------------------------------------------------------

export interface CalendarItem {
  date: string;
  source: string;
  filing_id: number | null;
  organization_id: number;
  organization_name: string | null;
  regulation: Regulation | null;
  filing_type: string | null;
  title: string;
  period_key: string | null;
  status: FilingStatus | null;
  days_until_due: number;
  urgency: string;
  penalty_per_day_paise: number | null;
  is_open: boolean;
}

export interface CalendarResponse {
  start: string;
  end: string;
  items: CalendarItem[];
  total: number;
  overdue: number;
  due_this_week: number;
  by_urgency: Record<string, number>;
  by_regulation: Record<string, number>;
}

// --- Obligations -----------------------------------------------------------

export interface ObligationResponse {
  id: number;
  organization_id: number | null;
  regulation: Regulation;
  code: string;
  title: string;
  filing_type: string | null;
  section: string | null;
  description: string | null;
  authority: string | null;
  frequency: Frequency;
  due_day: number | null;
  due_month: number | null;
  offset_days: number | null;
  period_offset: number;
  penalty_description: string | null;
  penalty_per_day_paise: number | null;
  penalty_max_paise: number | null;
  effective_from: string | null;
  effective_to: string | null;
  is_active: boolean;
  is_system: boolean;
  created_at: string;
}

// --- Documents ---------------------------------------------------------

export interface DocumentSummary {
  id: number;
  organization_id: number;
  type: DocumentType;
  title: string;
  original_filename: string | null;
  mime_type: string | null;
  size_bytes: number | null;
  content_hash: string | null;
  filing_id: number | null;
  regulation: Regulation | null;
  parse_status: ParseStatus;
  parsed_at: string | null;
  extraction_method: string | null;
  extracted_deadline: string | null;
  extracted_summary: string | null;
  uploaded_by_id: number | null;
  created_at: string;
}

export interface DocumentResponse extends DocumentSummary {
  parsed_content: string | null;
  parse_error: string | null;
  extracted_json: Record<string, unknown> | null;
  metadata_json: Record<string, unknown> | null;
  updated_at: string;
}

export interface DocumentUploadResponse {
  document: DocumentResponse;
  deduplicated: boolean;
  parse_queued: boolean;
}

// --- Alerts / notifications ----------------------------------------------

export interface AlertSummary {
  unacknowledged_impacts: number;
  critical_impacts: number;
  overdue_filings: number;
  due_within_7_days: number;
  open_breach_incidents: number;
  overdue_data_requests: number;
  failed_notifications: number;
}

export interface RegulatoryUpdateResponse {
  id: number;
  source: string;
  source_url: string | null;
  reference_no: string | null;
  published_date: string;
  effective_date: string | null;
  title: string;
  summary: string | null;
  regulation: Regulation | null;
  impact_level: ImpactLevel;
  action_required: string | null;
  compliance_deadline: string | null;
  is_analysed: boolean;
  is_published: boolean;
  created_at: string;
}

export interface RegulatoryImpactResponse {
  id: number;
  organization_id: number;
  update_id: number;
  impact_level: ImpactLevel;
  rationale: string | null;
  action_required: string | null;
  action_deadline: string | null;
  is_acknowledged: boolean;
  acknowledged_at: string | null;
  acknowledged_by_id: number | null;
  created_at: string;
  update: RegulatoryUpdateResponse | null;
}

// --- Audit ---------------------------------------------------------------

export interface AuditEntryResponse {
  id: number;
  organization_id: number;
  sequence: number;
  user_id: number | null;
  actor_label: string;
  action: AuditAction;
  entity_type: string;
  entity_id: string | null;
  timestamp: string;
  ip_address: string | null;
  request_id: string | null;
  before_json: Record<string, unknown> | null;
  after_json: Record<string, unknown> | null;
  summary: string | null;
  prev_checksum: string;
  checksum: string;
}

// --- Dashboard -------------------------------------------------------------

export interface ComplianceScore {
  score: number;
  band: string;
  on_time_filings: number;
  late_filings: number;
  overdue_filings: number;
  open_filings: number;
  total_filings: number;
  unacknowledged_impacts: number;
  overdue_dpb_notifications: number;
}

export interface RegulationBreakdown {
  regulation: Regulation;
  total: number;
  open: number;
  overdue: number;
  submitted: number;
}

export interface FilingCard {
  filing_id: number;
  title: string;
  filing_type: string | null;
  regulation: Regulation;
  period_key: string;
  due_date: string;
  days_until_due: number;
  status: FilingStatus;
  penalty_per_day_paise: number | null;
}

export interface DashboardResponse {
  organization_id: number;
  organization_name: string;
  as_of: string;
  score: ComplianceScore;
  upcoming: FilingCard[];
  overdue: FilingCard[];
  by_regulation: RegulationBreakdown[];
  by_status: Record<string, number>;
  penalty_exposure_paise: number;
  documents_pending_parse: number;
  open_data_requests: number;
  open_breaches: number;
}

export interface ClientDashboardRow {
  client_id: number;
  organization_id: number;
  name: string;
  status: string;
  assigned_user_id: number | null;
  score: number;
  band: string;
  open_filings: number;
  overdue_filings: number;
  due_within_7_days: number;
  penalty_exposure_paise: number;
  next_due_date: string | null;
  next_filing_type: string | null;
}

export interface FirmDashboardResponse {
  ca_firm_id: number;
  ca_firm_name: string;
  as_of: string;
  client_count: number;
  clients: ClientDashboardRow[];
  total_open_filings: number;
  total_overdue_filings: number;
  total_penalty_exposure_paise: number;
  average_score: number;
  attention_required: ClientDashboardRow[];
}
