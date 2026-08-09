"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth-context";
import { useApi } from "@/lib/use-api";
import { clientsApi, authApi } from "@/lib/resources";
import { ApiError } from "@/lib/api";
import { formatDate, humanize } from "@/lib/format";
import { Badge, Button, Card, EmptyState, ErrorBlock, Field, Input, LoadingBlock, Select } from "@/components/ui";

const ENGAGEMENT_TYPES = ["full_compliance", "gst_only", "audit_only", "advisory", "custom"];
const ENTITY_TYPES = [
  "private_limited",
  "public_limited",
  "one_person_company",
  "llp",
  "partnership",
  "proprietorship",
  "trust",
  "society",
  "section_8",
  "foreign_company",
  "huf",
];

export default function ClientsPage() {
  const router = useRouter();
  const { setSession } = useAuth();
  const { data, loading, error, reload } = useApi(() => clientsApi.list({ limit: 100 }), []);
  const [showForm, setShowForm] = useState(false);
  const [switchingId, setSwitchingId] = useState<number | null>(null);

  async function actAs(orgId: number) {
    setSwitchingId(orgId);
    try {
      const tokens = await authApi.switchClient(orgId);
      setSession(tokens);
      router.push("/dashboard");
    } catch {
      setSwitchingId(null);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold text-slate-900 dark:text-slate-50">Clients</h1>
        <Button onClick={() => setShowForm((v) => !v)}>{showForm ? "Cancel" : "Add client"}</Button>
      </div>

      {showForm && (
        <NewClientForm
          onCreated={() => {
            setShowForm(false);
            reload();
          }}
        />
      )}

      {loading && <LoadingBlock label="Loading clients…" />}
      {error && <ErrorBlock message={error} />}

      {data && (
        <Card>
          {data.items.length === 0 ? (
            <EmptyState title="No clients yet" description="Add your first client engagement above." />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-slate-100 text-left text-xs uppercase tracking-wide text-slate-400 dark:border-slate-800">
                    <th className="py-2 pr-4">Client</th>
                    <th className="py-2 pr-4">Engagement</th>
                    <th className="py-2 pr-4">Status</th>
                    <th className="py-2 pr-4">Open filings</th>
                    <th className="py-2 pr-4">Overdue</th>
                    <th className="py-2 pr-4">Since</th>
                    <th className="py-2 pr-4" />
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((c) => (
                    <tr key={c.id} className="border-b border-slate-50 dark:border-slate-800/60">
                      <td className="py-2.5 pr-4 font-medium text-slate-800 dark:text-slate-100">
                        {c.client_organization?.name ?? `Org #${c.client_org_id}`}
                      </td>
                      <td className="py-2.5 pr-4 text-slate-600 dark:text-slate-300">{humanize(c.engagement_type)}</td>
                      <td className="py-2.5 pr-4">
                        <Badge value={c.status} />
                      </td>
                      <td className="py-2.5 pr-4 tabular-nums">{c.open_filings ?? "—"}</td>
                      <td className="py-2.5 pr-4 tabular-nums text-rose-600 dark:text-rose-400">{c.overdue_filings ?? "—"}</td>
                      <td className="py-2.5 pr-4 text-slate-500 dark:text-slate-400">{formatDate(c.start_date)}</td>
                      <td className="py-2.5 pr-4 text-right">
                        <Button variant="secondary" disabled={switchingId === c.client_org_id} onClick={() => actAs(c.client_org_id)}>
                          {switchingId === c.client_org_id ? "Switching…" : "Act as"}
                        </Button>
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

function NewClientForm({ onCreated }: { onCreated: () => void }) {
  const [name, setName] = useState("");
  const [entityType, setEntityType] = useState("");
  const [state, setState] = useState("");
  const [contactEmail, setContactEmail] = useState("");
  const [engagementType, setEngagementType] = useState("full_compliance");
  const [retainer, setRetainer] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await clientsApi.create({
        organization: {
          name,
          entity_type: entityType || null,
          state: state || null,
          contact_email: contactEmail || null,
        },
        engagement_type: engagementType,
        retainer_paise: retainer ? Math.round(Number(retainer) * 100) : null,
      });
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not add this client");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card title="New client">
      <form onSubmit={handleSubmit} className="space-y-4">
        {error && <ErrorBlock message={error} />}
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label="Company name">
            <Input required minLength={2} value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="Entity type">
            <Select value={entityType} onChange={(e) => setEntityType(e.target.value)}>
              <option value="">Select…</option>
              {ENTITY_TYPES.map((t) => (
                <option key={t} value={t}>
                  {humanize(t)}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="State">
            <Input value={state} onChange={(e) => setState(e.target.value)} />
          </Field>
          <Field label="Contact email">
            <Input type="email" value={contactEmail} onChange={(e) => setContactEmail(e.target.value)} />
          </Field>
          <Field label="Engagement type">
            <Select value={engagementType} onChange={(e) => setEngagementType(e.target.value)}>
              {ENGAGEMENT_TYPES.map((t) => (
                <option key={t} value={t}>
                  {humanize(t)}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Retainer (₹ / month, optional)">
            <Input value={retainer} onChange={(e) => setRetainer(e.target.value)} />
          </Field>
        </div>
        <div className="flex justify-end">
          <Button type="submit" disabled={submitting}>
            {submitting ? "Adding…" : "Add client"}
          </Button>
        </div>
      </form>
    </Card>
  );
}

