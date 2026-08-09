"use client";

import Link from "next/link";
import { useState } from "react";
import { useApi } from "@/lib/use-api";
import { filingsApi } from "@/lib/resources";
import { formatDate, daysUntilLabel } from "@/lib/format";
import { Badge, Card, EmptyState, ErrorBlock, Field, Input, LoadingBlock, Select, Button } from "@/components/ui";
import type { FilingStatus, Regulation } from "@/lib/types";

const REGULATIONS: Regulation[] = ["gst", "income_tax", "rbi", "sebi", "mca", "fema", "labor", "dpdp"];
const STATUSES: FilingStatus[] = [
  "not_started",
  "draft",
  "in_review",
  "approved",
  "submitted",
  "acknowledged",
  "rejected",
  "late_filed",
  "not_applicable",
];

const PAGE_SIZE = 25;

export default function FilingsPage() {
  const [regulation, setRegulation] = useState("");
  const [status, setStatus] = useState("");
  const [onlyOpen, setOnlyOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);

  const { data, loading, error } = useApi(
    () =>
      filingsApi.list({
        regulation: (regulation || undefined) as Regulation | undefined,
        status: (status || undefined) as FilingStatus | undefined,
        only_open: onlyOpen || undefined,
        search: search || undefined,
        limit: PAGE_SIZE,
        offset,
      }),
    [regulation, status, onlyOpen, search, offset]
  );

  function resetAnd(setter: () => void) {
    setter();
    setOffset(0);
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold text-slate-900 dark:text-slate-50">Filings</h1>
      </div>

      <div className="flex flex-wrap items-end gap-3">
        <Field label="Search">
          <Input
            value={search}
            onChange={(e) => resetAnd(() => setSearch(e.target.value))}
            placeholder="Title, period, ack. no…"
            className="w-56"
          />
        </Field>
        <Field label="Regulation">
          <Select value={regulation} onChange={(e) => resetAnd(() => setRegulation(e.target.value))} className="w-40">
            <option value="">All</option>
            {REGULATIONS.map((r) => (
              <option key={r} value={r}>
                {r.toUpperCase()}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Status">
          <Select value={status} onChange={(e) => resetAnd(() => setStatus(e.target.value))} className="w-44">
            <option value="">All</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </Select>
        </Field>
        <label className="flex items-center gap-2 pb-2 text-sm text-slate-600 dark:text-slate-300">
          <input type="checkbox" checked={onlyOpen} onChange={(e) => resetAnd(() => setOnlyOpen(e.target.checked))} />
          Only open
        </label>
      </div>

      {loading && <LoadingBlock label="Loading filings…" />}
      {error && <ErrorBlock message={error} />}

      {data && (
        <Card>
          {data.items.length === 0 ? (
            <EmptyState title="No filings match these filters" />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-slate-100 text-left text-xs uppercase tracking-wide text-slate-400 dark:border-slate-800">
                    <th className="py-2 pr-4">Filing</th>
                    <th className="py-2 pr-4">Regulation</th>
                    <th className="py-2 pr-4">Period</th>
                    <th className="py-2 pr-4">Due</th>
                    <th className="py-2 pr-4">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((f) => (
                    <tr key={f.id} className="border-b border-slate-50 hover:bg-slate-50 dark:border-slate-800/60 dark:hover:bg-slate-800/40">
                      <td className="py-2.5 pr-4">
                        <Link href={`/filings/${f.id}`} className="font-medium text-slate-800 hover:underline dark:text-slate-100">
                          {f.title ?? f.filing_type ?? "Filing"}
                        </Link>
                      </td>
                      <td className="py-2.5 pr-4 text-slate-600 dark:text-slate-300">{f.regulation.toUpperCase()}</td>
                      <td className="py-2.5 pr-4 text-slate-600 dark:text-slate-300">{f.period_key}</td>
                      <td className="py-2.5 pr-4 text-slate-600 dark:text-slate-300">
                        {formatDate(f.effective_due_date ?? f.due_date)}
                        <span className="ml-1.5 text-xs text-slate-400">{daysUntilLabel(f.days_until_due)}</span>
                      </td>
                      <td className="py-2.5 pr-4">
                        <Badge value={f.status} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="mt-4 flex items-center justify-between text-sm text-slate-500 dark:text-slate-400">
            <span>
              {data.total === 0 ? "0" : `${offset + 1}–${Math.min(offset + data.items.length, data.total)}`} of {data.total}
            </span>
            <div className="flex gap-2">
              <Button variant="secondary" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
                Previous
              </Button>
              <Button
                variant="secondary"
                disabled={offset + data.items.length >= data.total}
                onClick={() => setOffset(offset + PAGE_SIZE)}
              >
                Next
              </Button>
            </div>
          </div>
        </Card>
      )}
    </div>
  );
}
