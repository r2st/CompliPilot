"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { useApi } from "@/lib/use-api";
import { dpdpApi } from "@/lib/resources";
import { ApiError } from "@/lib/api";
import { formatDate, formatDateTime, humanize } from "@/lib/format";
import { ROLE_RANK } from "@/lib/types";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorBlock,
  Field,
  Input,
  Label,
  LoadingBlock,
  Select,
  Stat,
} from "@/components/ui";

const TABS = [
  { key: "consents", label: "Consent register" },
  { key: "data-map", label: "Data map" },
  { key: "breaches", label: "Breach register" },
  { key: "requests", label: "Data principal requests" },
  { key: "assessments", label: "Impact assessments" },
] as const;

type TabKey = (typeof TABS)[number]["key"];

function splitList(value: string): string[] | undefined {
  const items = value
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);
  return items.length > 0 ? items : undefined;
}

function scoreTone(score: number): "good" | "warn" | "bad" {
  if (score >= 80) return "good";
  if (score >= 50) return "warn";
  return "bad";
}

export default function DPDPPage() {
  const { user } = useAuth();
  const roleRank = user ? ROLE_RANK[user.role] : 0;
  const canWrite = roleRank >= ROLE_RANK.staff;
  const canManage = roleRank >= ROLE_RANK.compliance_manager;

  const [tab, setTab] = useState<TabKey>("consents");
  const { data: readiness, loading, error, reload } = useApi(() => dpdpApi.readiness(), []);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-900 dark:text-slate-50">DPDP toolkit</h1>
        <p className="text-sm text-slate-500 dark:text-slate-400">
          Consent, the personal-data map, breach notifications, data principal requests and privacy
          impact assessments under the Digital Personal Data Protection Act.
        </p>
      </div>

      {loading && <LoadingBlock label="Loading readiness…" />}
      {error && <ErrorBlock message={error} />}
      {readiness && (
        <Card title="Readiness">
          <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
            <Stat label="Readiness score" value={`${readiness.score}/100`} tone={scoreTone(readiness.score)} />
            <Stat label="Data map entries" value={readiness.data_map_entries} />
            <Stat
              label="Active consents"
              value={readiness.active_consents}
              sublabel={`${readiness.withdrawn_consents} withdrawn`}
            />
            <Stat
              label="Open breaches"
              value={readiness.open_breaches}
              tone={readiness.open_breaches > 0 ? "bad" : "good"}
            />
            <Stat
              label="Overdue DPB notifications"
              value={readiness.overdue_dpb_notifications}
              tone={readiness.overdue_dpb_notifications > 0 ? "bad" : "good"}
            />
            <Stat
              label="Open requests"
              value={readiness.open_data_requests}
              sublabel={`${readiness.overdue_data_requests} overdue`}
              tone={readiness.overdue_data_requests > 0 ? "bad" : "default"}
            />
            <Stat label="Approved assessments" value={readiness.completed_assessments} />
            <Stat label="Cross-border transfers" value={readiness.cross_border_transfers} />
          </div>
          {readiness.gaps.length > 0 && (
            <ul className="mt-4 space-y-1.5 border-t border-slate-100 pt-4 text-sm text-amber-700 dark:border-slate-800 dark:text-amber-400">
              {readiness.gaps.map((gap) => (
                <li key={gap} className="flex gap-2">
                  <span aria-hidden>•</span>
                  <span>{gap}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      )}

      <div className="flex flex-wrap gap-1 border-b border-slate-200 dark:border-slate-800">
        {TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium transition ${
              tab === t.key
                ? "border-slate-900 text-slate-900 dark:border-slate-100 dark:text-slate-50"
                : "border-transparent text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "consents" && <ConsentsTab canWrite={canWrite} onChange={reload} />}
      {tab === "data-map" && <DataMapTab canWrite={canWrite} canManage={canManage} onChange={reload} />}
      {tab === "breaches" && <BreachesTab canWrite={canWrite} canManage={canManage} onChange={reload} />}
      {tab === "requests" && <RequestsTab canWrite={canWrite} onChange={reload} />}
      {tab === "assessments" && <AssessmentsTab canWrite={canWrite} canManage={canManage} onChange={reload} />}
    </div>
  );
}

// --- Consent register --------------------------------------------------

function ConsentsTab({ canWrite, onChange }: { canWrite: boolean; onChange: () => void }) {
  const [grantedOnly, setGrantedOnly] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const { data, loading, error, reload } = useApi(
    () => dpdpApi.consents.list({ limit: 100, granted_only: grantedOnly }),
    [grantedOnly]
  );
  const [withdrawingId, setWithdrawingId] = useState<number | null>(null);

  async function withdraw(id: number) {
    setWithdrawingId(id);
    try {
      await dpdpApi.consents.withdraw(id);
      reload();
      onChange();
    } catch {
      // the row stays "granted"; a retry is the recovery path
    } finally {
      setWithdrawingId(null);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <label className="flex items-center gap-2 text-xs font-medium text-slate-500 dark:text-slate-400">
          <input type="checkbox" checked={grantedOnly} onChange={(e) => setGrantedOnly(e.target.checked)} />
          Granted only
        </label>
        {canWrite && <Button onClick={() => setShowForm((v) => !v)}>{showForm ? "Cancel" : "Record consent"}</Button>}
      </div>

      {showForm && (
        <NewConsentForm
          onCreated={() => {
            setShowForm(false);
            reload();
            onChange();
          }}
        />
      )}

      {loading && <LoadingBlock label="Loading the consent register…" />}
      {error && <ErrorBlock message={error} />}
      {data && (
        <Card>
          {data.items.length === 0 ? (
            <EmptyState title="No consent records" description="Record a data principal's consent above." />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-slate-100 text-left text-xs uppercase tracking-wide text-slate-400 dark:border-slate-800">
                    <th className="py-2 pr-4">Purpose</th>
                    <th className="py-2 pr-4">Type</th>
                    <th className="py-2 pr-4">Notice</th>
                    <th className="py-2 pr-4">Status</th>
                    <th className="py-2 pr-4">Granted</th>
                    <th className="py-2 pr-4">Expires</th>
                    <th className="py-2 pr-4" />
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((c) => (
                    <tr key={c.id} className="border-b border-slate-50 dark:border-slate-800/60">
                      <td className="py-2.5 pr-4 font-medium text-slate-800 dark:text-slate-100">
                        {c.purpose}
                        {c.purpose_description && (
                          <p className="text-xs font-normal text-slate-500 dark:text-slate-400">{c.purpose_description}</p>
                        )}
                      </td>
                      <td className="py-2.5 pr-4 text-slate-600 dark:text-slate-300">{humanize(c.principal_type)}</td>
                      <td className="py-2.5 pr-4 text-slate-600 dark:text-slate-300">
                        v{c.notice_version} · {c.notice_language}
                      </td>
                      <td className="py-2.5 pr-4">
                        <Badge value={c.withdrawn_at ? "withdrawn" : "granted"} />
                      </td>
                      <td className="py-2.5 pr-4 text-slate-500 dark:text-slate-400">{formatDateTime(c.granted_at)}</td>
                      <td className="py-2.5 pr-4 text-slate-500 dark:text-slate-400">{formatDateTime(c.expires_at)}</td>
                      <td className="py-2.5 pr-4 text-right">
                        {canWrite && !c.withdrawn_at && (
                          <Button variant="secondary" disabled={withdrawingId === c.id} onClick={() => withdraw(c.id)}>
                            {withdrawingId === c.id ? "Withdrawing…" : "Withdraw"}
                          </Button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}
    </div>
  );
}

function NewConsentForm({ onCreated }: { onCreated: () => void }) {
  const [principalRef, setPrincipalRef] = useState("");
  const [principalType, setPrincipalType] = useState("customer");
  const [purpose, setPurpose] = useState("");
  const [purposeDescription, setPurposeDescription] = useState("");
  const [noticeVersion, setNoticeVersion] = useState("1");
  const [noticeLanguage, setNoticeLanguage] = useState("en");
  const [collectionMethod, setCollectionMethod] = useState("");
  const [expiresAt, setExpiresAt] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await dpdpApi.consents.create({
        principal_ref: principalRef,
        principal_type: principalType,
        purpose,
        purpose_description: purposeDescription || null,
        notice_version: noticeVersion,
        notice_language: noticeLanguage,
        collection_method: collectionMethod || null,
        expires_at: expiresAt ? new Date(expiresAt).toISOString() : null,
      });
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not record this consent");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card title="Record consent">
      <form onSubmit={handleSubmit} className="space-y-4">
        {error && <ErrorBlock message={error} />}
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label="Data principal (email or phone)">
            <Input required minLength={1} value={principalRef} onChange={(e) => setPrincipalRef(e.target.value)} />
          </Field>
          <Field label="Principal type">
            <Select value={principalType} onChange={(e) => setPrincipalType(e.target.value)}>
              <option value="customer">Customer</option>
              <option value="employee">Employee</option>
              <option value="vendor">Vendor</option>
              <option value="other">Other</option>
            </Select>
          </Field>
          <Field label="Purpose">
            <Input required minLength={2} value={purpose} onChange={(e) => setPurpose(e.target.value)} />
          </Field>
          <Field label="Purpose description (optional)">
            <Input value={purposeDescription} onChange={(e) => setPurposeDescription(e.target.value)} />
          </Field>
          <Field label="Notice version">
            <Input required value={noticeVersion} onChange={(e) => setNoticeVersion(e.target.value)} />
          </Field>
          <Field label="Notice language">
            <Input required value={noticeLanguage} onChange={(e) => setNoticeLanguage(e.target.value)} />
          </Field>
          <Field label="Collection method (optional)">
            <Input value={collectionMethod} onChange={(e) => setCollectionMethod(e.target.value)} placeholder="web_form, app, paper…" />
          </Field>
          <Field label="Expires (optional)">
            <Input type="date" value={expiresAt} onChange={(e) => setExpiresAt(e.target.value)} />
          </Field>
        </div>
        <div className="flex justify-end">
          <Button type="submit" disabled={submitting}>
            {submitting ? "Recording…" : "Record consent"}
          </Button>
        </div>
      </form>
    </Card>
  );
}

// --- Data map ------------------------------------------------------------

const LEGAL_BASES = ["consent", "legitimate_use", "legal_obligation", "contract", "vital_interest"];

function DataMapTab({
  canWrite,
  canManage,
  onChange,
}: {
  canWrite: boolean;
  canManage: boolean;
  onChange: () => void;
}) {
  const [showForm, setShowForm] = useState(false);
  const { data, loading, error, reload } = useApi(() => dpdpApi.dataMap.list({ limit: 100 }), []);
  const [removingId, setRemovingId] = useState<number | null>(null);

  async function remove(id: number) {
    setRemovingId(id);
    try {
      await dpdpApi.dataMap.remove(id);
      reload();
      onChange();
    } catch {
      // leave the entry; the button re-enables for a retry
    } finally {
      setRemovingId(null);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        {canWrite && <Button onClick={() => setShowForm((v) => !v)}>{showForm ? "Cancel" : "Add data-map entry"}</Button>}
      </div>

      {showForm && (
        <NewDataMapForm
          onCreated={() => {
            setShowForm(false);
            reload();
            onChange();
          }}
        />
      )}

      {loading && <LoadingBlock label="Loading the data map…" />}
      {error && <ErrorBlock message={error} />}
      {data && (
        <Card>
          {data.items.length === 0 ? (
            <EmptyState title="No data-map entries" description="Inventory what personal data each system holds, above." />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-slate-100 text-left text-xs uppercase tracking-wide text-slate-400 dark:border-slate-800">
                    <th className="py-2 pr-4">System</th>
                    <th className="py-2 pr-4">Category</th>
                    <th className="py-2 pr-4">Purpose</th>
                    <th className="py-2 pr-4">Legal basis</th>
                    <th className="py-2 pr-4">Sensitive</th>
                    <th className="py-2 pr-4">Cross-border</th>
                    <th className="py-2 pr-4">Reviewed</th>
                    <th className="py-2 pr-4" />
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((entry) => (
                    <tr key={entry.id} className="border-b border-slate-50 dark:border-slate-800/60">
                      <td className="py-2.5 pr-4 font-medium text-slate-800 dark:text-slate-100">
                        {entry.system_name}
                        {entry.owner_team && (
                          <p className="text-xs font-normal text-slate-500 dark:text-slate-400">{entry.owner_team}</p>
                        )}
                      </td>
                      <td className="py-2.5 pr-4 text-slate-600 dark:text-slate-300">{entry.data_category}</td>
                      <td className="py-2.5 pr-4 text-slate-600 dark:text-slate-300">{entry.purpose}</td>
                      <td className="py-2.5 pr-4 text-slate-600 dark:text-slate-300">{humanize(entry.legal_basis)}</td>
                      <td className="py-2.5 pr-4">{entry.is_sensitive ? <Badge value="high" label="Sensitive" /> : "—"}</td>
                      <td className="py-2.5 pr-4">
                        {entry.is_transferred_abroad ? (
                          <Badge value="medium" label={(entry.transfer_countries_json ?? []).join(", ") || "Yes"} />
                        ) : (
                          "—"
                        )}
                      </td>
                      <td className="py-2.5 pr-4 text-slate-500 dark:text-slate-400">{formatDate(entry.last_reviewed_at)}</td>
                      <td className="py-2.5 pr-4 text-right">
                        {canManage && (
                          <Button variant="ghost" disabled={removingId === entry.id} onClick={() => remove(entry.id)}>
                            {removingId === entry.id ? "Removing…" : "Remove"}
                          </Button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}
    </div>
  );
}

function NewDataMapForm({ onCreated }: { onCreated: () => void }) {
  const [systemName, setSystemName] = useState("");
  const [systemType, setSystemType] = useState("");
  const [ownerTeam, setOwnerTeam] = useState("");
  const [dataCategory, setDataCategory] = useState("");
  const [purpose, setPurpose] = useState("");
  const [legalBasis, setLegalBasis] = useState("consent");
  const [isSensitive, setIsSensitive] = useState(false);
  const [isTransferredAbroad, setIsTransferredAbroad] = useState(false);
  const [transferCountries, setTransferCountries] = useState("");
  const [retentionDays, setRetentionDays] = useState("");
  const [recordCount, setRecordCount] = useState("");
  const [notes, setNotes] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await dpdpApi.dataMap.create({
        system_name: systemName,
        system_type: systemType || null,
        owner_team: ownerTeam || null,
        data_category: dataCategory,
        purpose,
        legal_basis: legalBasis,
        is_sensitive: isSensitive,
        is_transferred_abroad: isTransferredAbroad,
        transfer_countries_json: isTransferredAbroad ? splitList(transferCountries) : null,
        retention_period_days: retentionDays ? Number(retentionDays) : null,
        estimated_record_count: recordCount ? Number(recordCount) : null,
        notes: notes || null,
      });
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not add this entry");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card title="Add a data-map entry">
      <form onSubmit={handleSubmit} className="space-y-4">
        {error && <ErrorBlock message={error} />}
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label="System name">
            <Input required minLength={1} value={systemName} onChange={(e) => setSystemName(e.target.value)} />
          </Field>
          <Field label="System type (optional)">
            <Input value={systemType} onChange={(e) => setSystemType(e.target.value)} placeholder="CRM, HRMS, database…" />
          </Field>
          <Field label="Owner team (optional)">
            <Input value={ownerTeam} onChange={(e) => setOwnerTeam(e.target.value)} />
          </Field>
          <Field label="Data category">
            <Input required minLength={1} value={dataCategory} onChange={(e) => setDataCategory(e.target.value)} placeholder="contact_details, financial…" />
          </Field>
          <Field label="Purpose">
            <Input required minLength={2} value={purpose} onChange={(e) => setPurpose(e.target.value)} />
          </Field>
          <Field label="Legal basis">
            <Select value={legalBasis} onChange={(e) => setLegalBasis(e.target.value)}>
              {LEGAL_BASES.map((b) => (
                <option key={b} value={b}>
                  {humanize(b)}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Retention period, days (optional)">
            <Input type="number" min={0} value={retentionDays} onChange={(e) => setRetentionDays(e.target.value)} />
          </Field>
          <Field label="Estimated record count (optional)">
            <Input type="number" min={0} value={recordCount} onChange={(e) => setRecordCount(e.target.value)} />
          </Field>
        </div>
        <div className="flex flex-wrap gap-6">
          <label className="flex items-center gap-2 text-sm text-slate-700 dark:text-slate-200">
            <input type="checkbox" checked={isSensitive} onChange={(e) => setIsSensitive(e.target.checked)} />
            Sensitive personal data
          </label>
          <label className="flex items-center gap-2 text-sm text-slate-700 dark:text-slate-200">
            <input type="checkbox" checked={isTransferredAbroad} onChange={(e) => setIsTransferredAbroad(e.target.checked)} />
            Transferred abroad
          </label>
        </div>
        {isTransferredAbroad && (
          <Field label="Destination countries (comma-separated)">
            <Input required value={transferCountries} onChange={(e) => setTransferCountries(e.target.value)} placeholder="Singapore, Ireland" />
          </Field>
        )}
        <Field label="Notes (optional)">
          <Input value={notes} onChange={(e) => setNotes(e.target.value)} />
        </Field>
        <div className="flex justify-end">
          <Button type="submit" disabled={submitting}>
            {submitting ? "Adding…" : "Add entry"}
          </Button>
        </div>
      </form>
    </Card>
  );
}

// --- Breach register -------------------------------------------------------

const BREACH_STATUSES = ["open", "contained", "notified", "closed"];

function BreachesTab({
  canWrite,
  canManage,
  onChange,
}: {
  canWrite: boolean;
  canManage: boolean;
  onChange: () => void;
}) {
  const [openOnly, setOpenOnly] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const { data, loading, error, reload } = useApi(() => dpdpApi.breaches.list({ limit: 100, open_only: openOnly }), [openOnly]);
  const [notifyingId, setNotifyingId] = useState<number | null>(null);
  const [statusUpdating, setStatusUpdating] = useState<number | null>(null);
  const [rowError, setRowError] = useState<string | null>(null);

  async function updateStatus(id: number, status: string) {
    setStatusUpdating(id);
    setRowError(null);
    try {
      await dpdpApi.breaches.update(id, { status });
      reload();
      onChange();
    } catch (err) {
      setRowError(err instanceof ApiError ? err.message : "Could not update that breach");
    } finally {
      setStatusUpdating(null);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <label className="flex items-center gap-2 text-xs font-medium text-slate-500 dark:text-slate-400">
          <input type="checkbox" checked={openOnly} onChange={(e) => setOpenOnly(e.target.checked)} />
          Open only
        </label>
        {canWrite && <Button onClick={() => setShowForm((v) => !v)}>{showForm ? "Cancel" : "Report a breach"}</Button>}
      </div>

      {showForm && (
        <NewBreachForm
          onCreated={() => {
            setShowForm(false);
            reload();
            onChange();
          }}
        />
      )}

      {rowError && <ErrorBlock message={rowError} />}
      {loading && <LoadingBlock label="Loading the breach register…" />}
      {error && <ErrorBlock message={error} />}
      {data && (
        <Card>
          {data.items.length === 0 ? (
            <EmptyState title="No breach incidents" description="Nothing has been reported. That is a good sign." />
          ) : (
            <div className="space-y-3">
              {data.items.map((b) => (
                <div key={b.id} className="rounded-lg border border-slate-100 p-4 dark:border-slate-800">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-xs text-slate-400">{b.reference}</span>
                        <Badge value={b.severity} />
                        <Badge value={b.status} />
                        {b.dpb_notification_overdue && <Badge value="bad" label="DPB notification overdue" />}
                      </div>
                      <p className="mt-1 font-medium text-slate-800 dark:text-slate-100">{b.title}</p>
                      {b.description && <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">{b.description}</p>}
                      <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                        Detected {formatDateTime(b.detected_at)}
                        {b.affected_principals_count != null && ` · ${b.affected_principals_count} principals affected`}
                        {b.dpb_notified_at
                          ? ` · Board notified ${formatDateTime(b.dpb_notified_at)}`
                          : b.hours_until_dpb_deadline != null
                            ? b.hours_until_dpb_deadline >= 0
                              ? ` · ${b.hours_until_dpb_deadline.toFixed(1)}h left to notify the Board`
                              : ` · ${Math.abs(b.hours_until_dpb_deadline).toFixed(1)}h past the Board notification window`
                            : ""}
                      </p>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      {canWrite && b.status !== "closed" && (
                        <Select
                          className="w-36"
                          value={b.status}
                          disabled={statusUpdating === b.id}
                          onChange={(e) => updateStatus(b.id, e.target.value)}
                        >
                          {BREACH_STATUSES.map((s) => (
                            <option key={s} value={s}>
                              {humanize(s)}
                            </option>
                          ))}
                        </Select>
                      )}
                      {canManage && !b.dpb_notified_at && (
                        <Button
                          variant="secondary"
                          onClick={() => setNotifyingId(notifyingId === b.id ? null : b.id)}
                        >
                          {notifyingId === b.id ? "Cancel" : "Notify DPB"}
                        </Button>
                      )}
                    </div>
                  </div>
                  {notifyingId === b.id && (
                    <NotifyBreachForm
                      breachId={b.id}
                      onDone={() => {
                        setNotifyingId(null);
                        reload();
                        onChange();
                      }}
                    />
                  )}
                </div>
              ))}
            </div>
          )}
        </Card>
      )}
    </div>
  );
}

function NotifyBreachForm({ breachId, onDone }: { breachId: number; onDone: () => void }) {
  const [dpbReference, setDpbReference] = useState("");
  const [notificationText, setNotificationText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await dpdpApi.breaches.notify(breachId, {
        dpb_reference: dpbReference || undefined,
        notification_text: notificationText || undefined,
        notify_principals: false,
      });
      onDone();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not record the notification");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="mt-3 space-y-3 border-t border-slate-100 pt-3 dark:border-slate-800">
      {error && <ErrorBlock message={error} />}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label="Data Protection Board reference (optional)">
          <Input value={dpbReference} onChange={(e) => setDpbReference(e.target.value)} />
        </Field>
        <Field label="Notification text (optional)">
          <Input value={notificationText} onChange={(e) => setNotificationText(e.target.value)} />
        </Field>
      </div>
      <div className="flex justify-end">
        <Button type="submit" disabled={submitting}>
          {submitting ? "Recording…" : "Record Board notification"}
        </Button>
      </div>
    </form>
  );
}

function NewBreachForm({ onCreated }: { onCreated: () => void }) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [severity, setSeverity] = useState("medium");
  const [affectedCount, setAffectedCount] = useState("");
  const [affectedCategories, setAffectedCategories] = useState("");
  const [detectedAt, setDetectedAt] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await dpdpApi.breaches.create({
        title,
        description: description || null,
        severity,
        affected_principals_count: affectedCount ? Number(affectedCount) : null,
        affected_data_categories_json: splitList(affectedCategories) ?? null,
        detected_at: detectedAt ? new Date(detectedAt).toISOString() : null,
      });
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not report this breach");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card title="Report a breach">
      <form onSubmit={handleSubmit} className="space-y-4">
        {error && <ErrorBlock message={error} />}
        <p className="text-xs text-slate-500 dark:text-slate-400">
          The Data Protection Board must be notified within 72 hours of detection. Backdate the detection
          time if this is being logged after the fact — that is what makes the deadline correct.
        </p>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label="Title">
            <Input required minLength={2} value={title} onChange={(e) => setTitle(e.target.value)} />
          </Field>
          <Field label="Severity">
            <Select value={severity} onChange={(e) => setSeverity(e.target.value)}>
              {["low", "medium", "high", "critical"].map((s) => (
                <option key={s} value={s}>
                  {humanize(s)}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Detected at (defaults to now)">
            <Input type="datetime-local" value={detectedAt} onChange={(e) => setDetectedAt(e.target.value)} />
          </Field>
          <Field label="Affected principals (optional)">
            <Input type="number" min={0} value={affectedCount} onChange={(e) => setAffectedCount(e.target.value)} />
          </Field>
          <Field label="Affected data categories (comma-separated, optional)">
            <Input value={affectedCategories} onChange={(e) => setAffectedCategories(e.target.value)} placeholder="email, phone, financial" />
          </Field>
        </div>
        <Field label="Description (optional)">
          <Input value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>
        <div className="flex justify-end">
          <Button type="submit" disabled={submitting}>
            {submitting ? "Reporting…" : "Report breach"}
          </Button>
        </div>
      </form>
    </Card>
  );
}

// --- Data principal requests ------------------------------------------------

const DSR_TYPES = ["access", "correction", "erasure", "nomination", "grievance", "withdraw_consent"];

function RequestsTab({ canWrite, onChange }: { canWrite: boolean; onChange: () => void }) {
  const [openOnly, setOpenOnly] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const { data, loading, error, reload } = useApi(() => dpdpApi.requests.list({ limit: 100, open_only: openOnly }), [openOnly]);
  const [updatingId, setUpdatingId] = useState<number | null>(null);
  const [rejectingId, setRejectingId] = useState<number | null>(null);
  const [rejectionReason, setRejectionReason] = useState("");
  const [rowError, setRowError] = useState<string | null>(null);

  async function progress(id: number, payload: Record<string, unknown>) {
    setUpdatingId(id);
    setRowError(null);
    try {
      await dpdpApi.requests.update(id, payload);
      reload();
      onChange();
    } catch (err) {
      setRowError(err instanceof ApiError ? err.message : "Could not update this request");
    } finally {
      setUpdatingId(null);
    }
  }

  async function reject(id: number) {
    if (!rejectionReason.trim()) return;
    setUpdatingId(id);
    setRowError(null);
    try {
      await dpdpApi.requests.update(id, { status: "rejected", rejection_reason: rejectionReason });
      setRejectingId(null);
      setRejectionReason("");
      reload();
      onChange();
    } catch (err) {
      setRowError(err instanceof ApiError ? err.message : "Could not reject this request");
    } finally {
      setUpdatingId(null);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <label className="flex items-center gap-2 text-xs font-medium text-slate-500 dark:text-slate-400">
          <input type="checkbox" checked={openOnly} onChange={(e) => setOpenOnly(e.target.checked)} />
          Open only
        </label>
        {canWrite && <Button onClick={() => setShowForm((v) => !v)}>{showForm ? "Cancel" : "Log a request"}</Button>}
      </div>

      {showForm && (
        <NewRequestForm
          onCreated={() => {
            setShowForm(false);
            reload();
            onChange();
          }}
        />
      )}

      {rowError && <ErrorBlock message={rowError} />}
      {loading && <LoadingBlock label="Loading data principal requests…" />}
      {error && <ErrorBlock message={error} />}
      {data && (
        <Card>
          {data.items.length === 0 ? (
            <EmptyState title="No requests logged" description="Access, correction, erasure and other requests appear here." />
          ) : (
            <div className="space-y-3">
              {data.items.map((r) => (
                <div key={r.id} className="rounded-lg border border-slate-100 p-4 dark:border-slate-800">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-xs text-slate-400">{r.reference}</span>
                        <Badge value={r.request_type} label={humanize(r.request_type)} />
                        <Badge value={r.status} />
                        {r.is_overdue && <Badge value="bad" label="Overdue" />}
                      </div>
                      <p className="mt-1 font-medium text-slate-800 dark:text-slate-100">{r.principal_name ?? "Unnamed principal"}</p>
                      {r.details && <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">{r.details}</p>}
                      <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                        Received {formatDate(r.received_at)} · Due {formatDate(r.due_date)}
                        {r.days_until_due != null &&
                          (r.days_until_due < 0
                            ? ` · ${Math.abs(r.days_until_due)}d overdue`
                            : ` · ${r.days_until_due}d left`)}
                      </p>
                    </div>
                    {canWrite && r.status !== "completed" && r.status !== "rejected" && (
                      <div className="flex flex-wrap items-center gap-2">
                        {!r.verified_at && (
                          <Button
                            variant="secondary"
                            disabled={updatingId === r.id}
                            onClick={() => progress(r.id, { mark_verified: true, status: "verifying" })}
                          >
                            Mark verified
                          </Button>
                        )}
                        {r.status !== "in_progress" && (
                          <Button
                            variant="secondary"
                            disabled={updatingId === r.id}
                            onClick={() => progress(r.id, { status: "in_progress" })}
                          >
                            In progress
                          </Button>
                        )}
                        <Button
                          disabled={updatingId === r.id}
                          onClick={() => progress(r.id, { status: "completed" })}
                        >
                          Complete
                        </Button>
                        <Button
                          variant="ghost"
                          disabled={updatingId === r.id}
                          onClick={() => setRejectingId(rejectingId === r.id ? null : r.id)}
                        >
                          Reject
                        </Button>
                      </div>
                    )}
                  </div>
                  {rejectingId === r.id && (
                    <div className="mt-3 flex flex-wrap items-end gap-2 border-t border-slate-100 pt-3 dark:border-slate-800">
                      <div className="min-w-64 flex-1">
                        <Label>Rejection reason</Label>
                        <Input value={rejectionReason} onChange={(e) => setRejectionReason(e.target.value)} />
                      </div>
                      <Button variant="danger" disabled={updatingId === r.id || !rejectionReason.trim()} onClick={() => reject(r.id)}>
                        Confirm reject
                      </Button>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </Card>
      )}
    </div>
  );
}

function NewRequestForm({ onCreated }: { onCreated: () => void }) {
  const [requestType, setRequestType] = useState("access");
  const [principalRef, setPrincipalRef] = useState("");
  const [principalName, setPrincipalName] = useState("");
  const [details, setDetails] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await dpdpApi.requests.create({
        request_type: requestType,
        principal_ref: principalRef,
        principal_name: principalName || null,
        details: details || null,
      });
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not log this request");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card title="Log a data principal request">
      <form onSubmit={handleSubmit} className="space-y-4">
        {error && <ErrorBlock message={error} />}
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label="Request type">
            <Select value={requestType} onChange={(e) => setRequestType(e.target.value)}>
              {DSR_TYPES.map((t) => (
                <option key={t} value={t}>
                  {humanize(t)}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Data principal (email or phone)">
            <Input required minLength={1} value={principalRef} onChange={(e) => setPrincipalRef(e.target.value)} />
          </Field>
          <Field label="Name (optional)">
            <Input value={principalName} onChange={(e) => setPrincipalName(e.target.value)} />
          </Field>
        </div>
        <Field label="Details (optional)">
          <Input value={details} onChange={(e) => setDetails(e.target.value)} />
        </Field>
        <div className="flex justify-end">
          <Button type="submit" disabled={submitting}>
            {submitting ? "Logging…" : "Log request"}
          </Button>
        </div>
      </form>
    </Card>
  );
}

// --- Privacy impact assessments --------------------------------------------

function AssessmentsTab({
  canWrite,
  canManage,
  onChange,
}: {
  canWrite: boolean;
  canManage: boolean;
  onChange: () => void;
}) {
  const [showForm, setShowForm] = useState(false);
  const { data, loading, error, reload } = useApi(() => dpdpApi.assessments.list({ limit: 100 }), []);
  const [reviewingId, setReviewingId] = useState<number | null>(null);

  async function approve(id: number) {
    setReviewingId(id);
    try {
      await dpdpApi.assessments.review(id);
      reload();
      onChange();
    } catch {
      // the row stays "draft"; a retry is the recovery path
    } finally {
      setReviewingId(null);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        {canWrite && <Button onClick={() => setShowForm((v) => !v)}>{showForm ? "Cancel" : "Start an assessment"}</Button>}
      </div>

      {showForm && (
        <NewAssessmentForm
          onCreated={() => {
            setShowForm(false);
            reload();
            onChange();
          }}
        />
      )}

      {loading && <LoadingBlock label="Loading assessments…" />}
      {error && <ErrorBlock message={error} />}
      {data && (
        <Card>
          {data.items.length === 0 ? (
            <EmptyState title="No assessments yet" description="Start a privacy impact assessment for a new processing activity above." />
          ) : (
            <ul className="divide-y divide-slate-100 dark:divide-slate-800">
              {data.items.map((pia) => (
                <li key={pia.id} className="flex flex-wrap items-start justify-between gap-3 py-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge value={pia.status} />
                      {pia.risk_level && <Badge value={pia.risk_level} label={`Risk: ${humanize(pia.risk_level)}`} />}
                    </div>
                    <p className="mt-1 font-medium text-slate-800 dark:text-slate-100">{pia.title}</p>
                    <p className="text-sm text-slate-600 dark:text-slate-300">{pia.processing_activity}</p>
                    <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                      {pia.reviewed_at
                        ? `Reviewed ${formatDate(pia.reviewed_at)} · next review ${formatDate(pia.next_review_date)}`
                        : "Not yet reviewed"}
                    </p>
                  </div>
                  {canManage && pia.status !== "approved" && (
                    <Button variant="secondary" disabled={reviewingId === pia.id} onClick={() => approve(pia.id)}>
                      {reviewingId === pia.id ? "Approving…" : "Approve"}
                    </Button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Card>
      )}
    </div>
  );
}

function NewAssessmentForm({ onCreated }: { onCreated: () => void }) {
  const [title, setTitle] = useState("");
  const [processingActivity, setProcessingActivity] = useState("");
  const [residualRisk, setResidualRisk] = useState("");
  const [nextReviewDate, setNextReviewDate] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await dpdpApi.assessments.create({
        title,
        processing_activity: processingActivity,
        residual_risk: residualRisk || null,
        next_review_date: nextReviewDate || null,
      });
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not start this assessment");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card title="Start a privacy impact assessment">
      <form onSubmit={handleSubmit} className="space-y-4">
        {error && <ErrorBlock message={error} />}
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label="Title">
            <Input required minLength={2} value={title} onChange={(e) => setTitle(e.target.value)} />
          </Field>
          <Field label="Residual risk (optional)">
            <Select value={residualRisk} onChange={(e) => setResidualRisk(e.target.value)}>
              <option value="">Not assessed yet</option>
              {["low", "medium", "high", "critical"].map((s) => (
                <option key={s} value={s}>
                  {humanize(s)}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Next review date (optional)">
            <Input type="date" value={nextReviewDate} onChange={(e) => setNextReviewDate(e.target.value)} />
          </Field>
        </div>
        <Field label="Processing activity">
          <Input required minLength={2} value={processingActivity} onChange={(e) => setProcessingActivity(e.target.value)} />
        </Field>
        <div className="flex justify-end">
          <Button type="submit" disabled={submitting}>
            {submitting ? "Starting…" : "Start assessment"}
          </Button>
        </div>
      </form>
    </Card>
  );
}
