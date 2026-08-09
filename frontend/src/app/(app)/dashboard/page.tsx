"use client";

import Link from "next/link";
import { useAuth } from "@/lib/auth-context";
import { useApi } from "@/lib/use-api";
import { dashboardApi, organizationsApi } from "@/lib/resources";
import { formatDate, formatRupees, humanize } from "@/lib/format";
import { Badge, Card, EmptyState, ErrorBlock, LoadingBlock, Stat } from "@/components/ui";
import type { FilingCard } from "@/lib/types";

export default function DashboardPage() {
  const { session } = useAuth();
  const { data: org } = useApi(() => organizationsApi.me(), []);

  const isFirmHome = !!org && org.type === "ca_firm" && !session?.is_delegated;

  if (!org) return <LoadingBlock label="Loading dashboard…" />;

  return isFirmHome ? <FirmDashboard /> : <OrgDashboard />;
}

function scoreTone(band: string): "good" | "warn" | "bad" | "default" {
  if (band === "excellent" || band === "good") return "good";
  if (band === "at_risk") return "warn";
  if (band === "critical") return "bad";
  return "default";
}

function OrgDashboard() {
  const { data, loading, error } = useApi(() => dashboardApi.get({ upcoming_days: 30, limit: 10 }), []);

  if (loading) return <LoadingBlock label="Loading dashboard…" />;
  if (error) return <ErrorBlock message={error} />;
  if (!data) return null;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-900 dark:text-slate-50">{data.organization_name}</h1>
        <p className="text-sm text-slate-500 dark:text-slate-400">Compliance position as of {formatDate(data.as_of)}</p>
      </div>

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <Card>
          <Stat label="Compliance score" value={`${data.score.score}`} tone={scoreTone(data.score.band)} sublabel={humanize(data.score.band)} />
        </Card>
        <Card>
          <Stat label="Overdue filings" value={data.score.overdue_filings} tone={data.score.overdue_filings > 0 ? "bad" : "good"} />
        </Card>
        <Card>
          <Stat label="Open filings" value={data.score.open_filings} />
        </Card>
        <Card>
          <Stat label="Penalty exposure" value={formatRupees(data.penalty_exposure_paise)} tone={data.penalty_exposure_paise > 0 ? "bad" : "good"} />
        </Card>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card title="Overdue" action={<Link href="/filings?only_open=true" className="text-xs font-medium text-slate-500 hover:underline">View all</Link>}>
          <FilingCardList items={data.overdue} emptyLabel="Nothing overdue. Good work." />
        </Card>
        <Card title="Upcoming (30 days)" action={<Link href="/filings" className="text-xs font-medium text-slate-500 hover:underline">View all</Link>}>
          <FilingCardList items={data.upcoming} emptyLabel="Nothing due in the next 30 days." />
        </Card>
      </div>

      <Card title="By regulation">
        {data.by_regulation.length === 0 ? (
          <EmptyState title="No filings tracked yet" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-100 text-left text-xs uppercase tracking-wide text-slate-400 dark:border-slate-800">
                  <th className="py-2 pr-4">Regulation</th>
                  <th className="py-2 pr-4">Total</th>
                  <th className="py-2 pr-4">Open</th>
                  <th className="py-2 pr-4">Overdue</th>
                  <th className="py-2 pr-4">Submitted</th>
                </tr>
              </thead>
              <tbody>
                {data.by_regulation.map((row) => (
                  <tr key={row.regulation} className="border-b border-slate-50 dark:border-slate-800/60">
                    <td className="py-2 pr-4 font-medium text-slate-700 dark:text-slate-200">{row.regulation.toUpperCase()}</td>
                    <td className="py-2 pr-4 tabular-nums">{row.total}</td>
                    <td className="py-2 pr-4 tabular-nums">{row.open}</td>
                    <td className="py-2 pr-4 tabular-nums text-rose-600 dark:text-rose-400">{row.overdue}</td>
                    <td className="py-2 pr-4 tabular-nums text-emerald-600 dark:text-emerald-400">{row.submitted}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <Card>
          <Stat label="Documents pending parse" value={data.documents_pending_parse} />
        </Card>
        <Card>
          <Stat label="Open DPDP data requests" value={data.open_data_requests} />
        </Card>
        <Card>
          <Stat label="Open breach incidents" value={data.open_breaches} tone={data.open_breaches > 0 ? "bad" : "good"} />
        </Card>
      </div>
    </div>
  );
}

function FilingCardList({ items, emptyLabel }: { items: FilingCard[]; emptyLabel: string }) {
  if (items.length === 0) return <EmptyState title={emptyLabel} />;
  return (
    <ul className="divide-y divide-slate-100 dark:divide-slate-800">
      {items.map((f) => (
        <li key={f.filing_id} className="flex items-center justify-between py-2.5">
          <Link href={`/filings/${f.filing_id}`} className="min-w-0 flex-1 pr-3">
            <p className="truncate text-sm font-medium text-slate-800 hover:underline dark:text-slate-100">{f.title}</p>
            <p className="text-xs text-slate-500 dark:text-slate-400">
              {f.regulation.toUpperCase()} · {f.period_key} · due {formatDate(f.due_date)}
            </p>
          </Link>
          <Badge value={f.status} />
        </li>
      ))}
    </ul>
  );
}

function FirmDashboard() {
  const { data, loading, error } = useApi(() => dashboardApi.firm({ limit: 20 }), []);

  if (loading) return <LoadingBlock label="Loading firm dashboard…" />;
  if (error) return <ErrorBlock message={error} />;
  if (!data) return null;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-900 dark:text-slate-50">{data.ca_firm_name}</h1>
        <p className="text-sm text-slate-500 dark:text-slate-400">Consolidated view across {data.client_count} clients, as of {formatDate(data.as_of)}</p>
      </div>

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <Card>
          <Stat label="Average score" value={data.average_score} tone={scoreTone(data.average_score >= 75 ? "good" : data.average_score >= 50 ? "at_risk" : "critical")} />
        </Card>
        <Card>
          <Stat label="Open filings" value={data.total_open_filings} />
        </Card>
        <Card>
          <Stat label="Overdue filings" value={data.total_overdue_filings} tone={data.total_overdue_filings > 0 ? "bad" : "good"} />
        </Card>
        <Card>
          <Stat label="Penalty exposure" value={formatRupees(data.total_penalty_exposure_paise)} tone={data.total_penalty_exposure_paise > 0 ? "bad" : "good"} />
        </Card>
      </div>

      <Card title="Needs attention" action={<Link href="/clients" className="text-xs font-medium text-slate-500 hover:underline">All clients</Link>}>
        {data.attention_required.length === 0 ? (
          <EmptyState title="No client has an overdue filing right now." />
        ) : (
          <ClientTable rows={data.attention_required} />
        )}
      </Card>

      <Card title="All clients">
        {data.clients.length === 0 ? <EmptyState title="No clients engaged yet" description="Add your first client from the Clients page." /> : <ClientTable rows={data.clients} />}
      </Card>
    </div>
  );
}

function ClientTable({ rows }: { rows: { client_id: number; name: string; score: number; band: string; open_filings: number; overdue_filings: number; next_due_date: string | null; next_filing_type: string | null }[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-slate-100 text-left text-xs uppercase tracking-wide text-slate-400 dark:border-slate-800">
            <th className="py-2 pr-4">Client</th>
            <th className="py-2 pr-4">Score</th>
            <th className="py-2 pr-4">Open</th>
            <th className="py-2 pr-4">Overdue</th>
            <th className="py-2 pr-4">Next due</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.client_id} className="border-b border-slate-50 dark:border-slate-800/60">
              <td className="py-2 pr-4 font-medium text-slate-700 dark:text-slate-200">{row.name}</td>
              <td className="py-2 pr-4 tabular-nums">
                <Badge value={row.band} label={String(row.score)} />
              </td>
              <td className="py-2 pr-4 tabular-nums">{row.open_filings}</td>
              <td className="py-2 pr-4 tabular-nums text-rose-600 dark:text-rose-400">{row.overdue_filings}</td>
              <td className="py-2 pr-4 text-slate-600 dark:text-slate-300">
                {row.next_due_date ? `${row.next_filing_type ?? ""} · ${formatDate(row.next_due_date)}` : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
