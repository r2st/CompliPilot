"use client";

import { useState } from "react";
import { useApi } from "@/lib/use-api";
import { auditApi } from "@/lib/resources";
import { formatDateTime, humanize } from "@/lib/format";
import { Badge, Button, Card, EmptyState, ErrorBlock, Field, Input, LoadingBlock } from "@/components/ui";

const PAGE_SIZE = 30;

export default function AuditPage() {
  const [entityType, setEntityType] = useState("");
  const [offset, setOffset] = useState(0);
  const { data, loading, error } = useApi(
    () => auditApi.list({ limit: PAGE_SIZE, offset, entity_type: entityType || undefined }),
    [entityType, offset]
  );

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold text-slate-900 dark:text-slate-50">Audit trail</h1>
      <p className="max-w-2xl text-sm text-slate-500 dark:text-slate-400">
        An append-only, hash-chained record of every action taken in this organization&apos;s context.
      </p>

      <Field label="Filter by entity type">
        <Input
          value={entityType}
          onChange={(e) => {
            setEntityType(e.target.value);
            setOffset(0);
          }}
          placeholder="e.g. filing, document, user"
          className="w-64"
        />
      </Field>

      {loading && <LoadingBlock label="Loading audit trail…" />}
      {error && <ErrorBlock message={error} />}

      {data && (
        <Card>
          {data.items.length === 0 ? (
            <EmptyState title="No entries match this filter" />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-slate-100 text-left text-xs uppercase tracking-wide text-slate-400 dark:border-slate-800">
                    <th className="py-2 pr-4">When</th>
                    <th className="py-2 pr-4">Actor</th>
                    <th className="py-2 pr-4">Action</th>
                    <th className="py-2 pr-4">Entity</th>
                    <th className="py-2 pr-4">Summary</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((entry) => (
                    <tr key={entry.id} className="border-b border-slate-50 align-top dark:border-slate-800/60">
                      <td className="py-2.5 pr-4 whitespace-nowrap text-slate-500 dark:text-slate-400">{formatDateTime(entry.timestamp)}</td>
                      <td className="py-2.5 pr-4 text-slate-700 dark:text-slate-200">{entry.actor_label}</td>
                      <td className="py-2.5 pr-4">
                        <Badge value={entry.action} />
                      </td>
                      <td className="py-2.5 pr-4 text-slate-600 dark:text-slate-300">
                        {humanize(entry.entity_type)}
                        {entry.entity_id ? ` #${entry.entity_id}` : ""}
                      </td>
                      <td className="py-2.5 pr-4 text-slate-600 dark:text-slate-300">{entry.summary ?? "—"}</td>
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
