"use client";

import { useState } from "react";
import { useApi } from "@/lib/use-api";
import { alertsApi } from "@/lib/resources";
import { formatDate } from "@/lib/format";
import { Badge, Button, Card, EmptyState, ErrorBlock, LoadingBlock, Stat } from "@/components/ui";

export default function AlertsPage() {
  const { data: summary, error: summaryError } = useApi(() => alertsApi.summary(), []);
  const [unacknowledgedOnly, setUnacknowledgedOnly] = useState(true);
  const { data, loading, error, reload } = useApi(
    () => alertsApi.impacts({ limit: 50, unacknowledged_only: unacknowledgedOnly }),
    [unacknowledgedOnly]
  );
  const [acking, setAcking] = useState<number | null>(null);

  async function acknowledge(id: number) {
    setAcking(id);
    try {
      await alertsApi.acknowledge(id);
      reload();
    } catch {
      // surfaced inline per-row would be nicer; the reload failing silently is acceptable here
    } finally {
      setAcking(null);
    }
  }

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold text-slate-900 dark:text-slate-50">Alerts</h1>

      {summaryError && <ErrorBlock message={summaryError} />}
      {summary && (
        <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
          <Card>
            <Stat label="Unacknowledged impacts" value={summary.unacknowledged_impacts} tone={summary.unacknowledged_impacts > 0 ? "warn" : "good"} />
          </Card>
          <Card>
            <Stat label="Critical impacts" value={summary.critical_impacts} tone={summary.critical_impacts > 0 ? "bad" : "good"} />
          </Card>
          <Card>
            <Stat label="Overdue filings" value={summary.overdue_filings} tone={summary.overdue_filings > 0 ? "bad" : "good"} />
          </Card>
          <Card>
            <Stat label="Due within 7 days" value={summary.due_within_7_days} />
          </Card>
          <Card>
            <Stat label="Open breach incidents" value={summary.open_breach_incidents} tone={summary.open_breach_incidents > 0 ? "bad" : "good"} />
          </Card>
          <Card>
            <Stat label="Overdue data requests" value={summary.overdue_data_requests} tone={summary.overdue_data_requests > 0 ? "bad" : "good"} />
          </Card>
          <Card>
            <Stat label="Failed notifications" value={summary.failed_notifications} tone={summary.failed_notifications > 0 ? "warn" : "good"} />
          </Card>
        </div>
      )}

      <Card
        title="Regulatory impacts"
        action={
          <label className="flex items-center gap-2 text-xs font-medium text-slate-500 dark:text-slate-400">
            <input type="checkbox" checked={unacknowledgedOnly} onChange={(e) => setUnacknowledgedOnly(e.target.checked)} />
            Unacknowledged only
          </label>
        }
      >
        {loading && <LoadingBlock label="Loading alerts…" />}
        {error && <ErrorBlock message={error} />}
        {data && data.items.length === 0 && <EmptyState title="Nothing here" description="No regulatory impacts match this filter." />}
        {data && data.items.length > 0 && (
          <ul className="divide-y divide-slate-100 dark:divide-slate-800">
            {data.items.map((impact) => (
              <li key={impact.id} className="flex flex-wrap items-start justify-between gap-3 py-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <Badge value={impact.impact_level} />
                    {impact.update && <span className="text-xs text-slate-400">{formatDate(impact.update.published_date)}</span>}
                  </div>
                  <p className="mt-1 font-medium text-slate-800 dark:text-slate-100">{impact.update?.title ?? "Regulatory update"}</p>
                  {impact.rationale && <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">{impact.rationale}</p>}
                  {impact.action_required && (
                    <p className="mt-1 text-sm text-amber-700 dark:text-amber-400">
                      Action required: {impact.action_required}
                      {impact.action_deadline ? ` by ${formatDate(impact.action_deadline)}` : ""}
                    </p>
                  )}
                </div>
                {!impact.is_acknowledged ? (
                  <Button variant="secondary" disabled={acking === impact.id} onClick={() => acknowledge(impact.id)}>
                    {acking === impact.id ? "Acknowledging…" : "Acknowledge"}
                  </Button>
                ) : (
                  <Badge value="acknowledged" />
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
