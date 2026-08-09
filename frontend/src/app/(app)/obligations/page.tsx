"use client";

import { useState } from "react";
import { useApi } from "@/lib/use-api";
import { obligationsApi } from "@/lib/resources";
import { formatDate, humanize } from "@/lib/format";
import { Badge, Card, EmptyState, ErrorBlock, Field, Input, LoadingBlock, Select, Button } from "@/components/ui";
import type { Regulation } from "@/lib/types";

const REGULATIONS: Regulation[] = ["gst", "income_tax", "rbi", "sebi", "mca", "fema", "labor", "dpdp"];
const PAGE_SIZE = 25;

export default function ObligationsPage() {
  const [regulation, setRegulation] = useState("");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);

  const { data, loading, error } = useApi(
    () =>
      obligationsApi.list({
        regulation: (regulation || undefined) as Regulation | undefined,
        search: search || undefined,
        limit: PAGE_SIZE,
        offset,
      }),
    [regulation, search, offset]
  );

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold text-slate-900 dark:text-slate-50">Obligations catalogue</h1>
      <p className="max-w-2xl text-sm text-slate-500 dark:text-slate-400">
        Every statutory obligation CompliPilot knows about, across GST, Income Tax, RBI, SEBI, MCA, FEMA, labour law and the DPDP Act.
      </p>

      <div className="flex flex-wrap items-end gap-3">
        <Field label="Search">
          <Input
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setOffset(0);
            }}
            placeholder="Title or code…"
            className="w-56"
          />
        </Field>
        <Field label="Regulation">
          <Select
            value={regulation}
            onChange={(e) => {
              setRegulation(e.target.value);
              setOffset(0);
            }}
            className="w-40"
          >
            <option value="">All</option>
            {REGULATIONS.map((r) => (
              <option key={r} value={r}>
                {r.toUpperCase()}
              </option>
            ))}
          </Select>
        </Field>
      </div>

      {loading && <LoadingBlock label="Loading catalogue…" />}
      {error && <ErrorBlock message={error} />}

      {data && (
        <Card>
          {data.items.length === 0 ? (
            <EmptyState title="No obligations match these filters" />
          ) : (
            <ul className="divide-y divide-slate-100 dark:divide-slate-800">
              {data.items.map((o) => (
                <li key={o.id} className="py-3">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div>
                      <p className="font-medium text-slate-800 dark:text-slate-100">{o.title}</p>
                      <p className="text-xs text-slate-500 dark:text-slate-400">
                        {o.code} · {o.regulation.toUpperCase()} · {humanize(o.frequency)}
                        {o.authority ? ` · ${o.authority}` : ""}
                      </p>
                      {o.description && <p className="mt-1 max-w-2xl text-sm text-slate-600 dark:text-slate-300">{o.description}</p>}
                      {o.penalty_description && (
                        <p className="mt-1 text-xs text-amber-700 dark:text-amber-400">Penalty: {o.penalty_description}</p>
                      )}
                    </div>
                    <div className="flex flex-col items-end gap-1">
                      {!o.is_system && <Badge value="custom" label="Tenant-defined" />}
                      {o.effective_from && (
                        <span className="text-xs text-slate-400">Effective from {formatDate(o.effective_from)}</span>
                      )}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
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
