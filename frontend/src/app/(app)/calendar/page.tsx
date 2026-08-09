"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { useApi } from "@/lib/use-api";
import { calendarApi } from "@/lib/resources";
import { formatDate, daysUntilLabel, humanize } from "@/lib/format";
import { Badge, Card, EmptyState, ErrorBlock, Field, LoadingBlock, Select, Stat } from "@/components/ui";
import type { Regulation } from "@/lib/types";

const REGULATIONS: Regulation[] = ["gst", "income_tax", "rbi", "sebi", "mca", "fema", "labor", "dpdp"];

export default function CalendarPage() {
  const { session } = useAuth();
  const isCaFirmMember = session ? session.is_delegated || session.available_clients.length > 0 : false;
  const [regulation, setRegulation] = useState<string>("");
  const [includeClosed, setIncludeClosed] = useState(false);
  const [allClients, setAllClients] = useState(false);

  const { data, loading, error } = useApi(
    () =>
      calendarApi.get({
        regulation: (regulation || undefined) as Regulation | undefined,
        include_closed: includeClosed,
        all_clients: allClients,
      }),
    [regulation, includeClosed, allClients]
  );

  const grouped = useMemo(() => {
    if (!data) return [];
    const map = new Map<string, typeof data.items>();
    for (const item of data.items) {
      const list = map.get(item.date) ?? [];
      list.push(item);
      map.set(item.date, list);
    }
    return Array.from(map.entries()).sort(([a], [b]) => a.localeCompare(b));
  }, [data]);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold text-slate-900 dark:text-slate-50">Compliance calendar</h1>
        <div className="flex flex-wrap items-end gap-3">
          <Field label="Regulation">
            <Select value={regulation} onChange={(e) => setRegulation(e.target.value)} className="w-40">
              <option value="">All</option>
              {REGULATIONS.map((r) => (
                <option key={r} value={r}>
                  {r.toUpperCase()}
                </option>
              ))}
            </Select>
          </Field>
          <label className="flex items-center gap-2 pb-2 text-sm text-slate-600 dark:text-slate-300">
            <input type="checkbox" checked={includeClosed} onChange={(e) => setIncludeClosed(e.target.checked)} />
            Include closed
          </label>
          {isCaFirmMember && (
            <label className="flex items-center gap-2 pb-2 text-sm text-slate-600 dark:text-slate-300">
              <input type="checkbox" checked={allClients} onChange={(e) => setAllClients(e.target.checked)} />
              All clients
            </label>
          )}
        </div>
      </div>

      {loading && <LoadingBlock label="Loading calendar…" />}
      {error && <ErrorBlock message={error} />}

      {data && (
        <>
          <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
            <Card>
              <Stat label="Total" value={data.total} />
            </Card>
            <Card>
              <Stat label="Overdue" value={data.overdue} tone={data.overdue > 0 ? "bad" : "good"} />
            </Card>
            <Card>
              <Stat label="Due this week" value={data.due_this_week} tone={data.due_this_week > 0 ? "warn" : "default"} />
            </Card>
            <Card>
              <Stat label="Window" value={`${formatDate(data.start)} – ${formatDate(data.end)}`} />
            </Card>
          </div>

          <Card>
            {grouped.length === 0 ? (
              <EmptyState title="Nothing on the calendar for this window" />
            ) : (
              <div className="space-y-6">
                {grouped.map(([date, items]) => (
                  <div key={date}>
                    <h3 className="mb-2 text-sm font-semibold text-slate-700 dark:text-slate-200">{formatDate(date)}</h3>
                    <ul className="space-y-2">
                      {items.map((item, idx) => (
                        <li
                          key={`${item.filing_id ?? item.source}-${idx}`}
                          className="flex items-center justify-between rounded-lg border border-slate-100 px-3 py-2.5 dark:border-slate-800"
                        >
                          <div className="min-w-0 flex-1 pr-3">
                            {item.filing_id ? (
                              <Link href={`/filings/${item.filing_id}`} className="truncate text-sm font-medium text-slate-800 hover:underline dark:text-slate-100">
                                {item.title}
                              </Link>
                            ) : (
                              <p className="truncate text-sm font-medium text-slate-800 dark:text-slate-100">{item.title}</p>
                            )}
                            <p className="text-xs text-slate-500 dark:text-slate-400">
                              {item.organization_name ? `${item.organization_name} · ` : ""}
                              {item.regulation ? `${item.regulation.toUpperCase()} · ` : ""}
                              {humanize(item.source)} · {daysUntilLabel(item.days_until_due)}
                            </p>
                          </div>
                          <div className="flex items-center gap-2">
                            <Badge value={item.urgency} />
                            {item.status && <Badge value={item.status} />}
                          </div>
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>
            )}
          </Card>
        </>
      )}
    </div>
  );
}
