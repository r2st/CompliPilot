"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
import { useApi } from "@/lib/use-api";
import { filingsApi } from "@/lib/resources";
import { ApiError } from "@/lib/api";
import { formatDate, formatDateTime, formatRupees, humanize } from "@/lib/format";
import { Badge, Button, Card, ErrorBlock, Field, Input, LoadingBlock } from "@/components/ui";
import type { FilingStatus } from "@/lib/types";

function toPaise(value: string): number | null {
  if (value.trim() === "") return null;
  const rupees = Number(value);
  if (Number.isNaN(rupees)) return null;
  return Math.round(rupees * 100);
}

function toRupeeInput(paise: number | null | undefined): string {
  if (paise === null || paise === undefined) return "";
  return String(paise / 100);
}

const NEEDS_ACK: FilingStatus[] = ["submitted", "late_filed", "acknowledged"];
const NEEDS_REASON: FilingStatus[] = ["rejected"];

export default function FilingDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const id = Number(params.id);
  const { data: filing, loading, error, reload } = useApi(() => filingsApi.get(id), [id]);

  const [pendingStatus, setPendingStatus] = useState<FilingStatus | null>(null);
  const [ackNo, setAckNo] = useState("");
  const [rejectionReason, setRejectionReason] = useState("");
  const [transitionError, setTransitionError] = useState<string | null>(null);
  const [transitioning, setTransitioning] = useState(false);

  const [notes, setNotes] = useState<string | null>(null);
  const [taxPayable, setTaxPayable] = useState<string | null>(null);
  const [taxPaid, setTaxPaid] = useState<string | null>(null);
  const [penalty, setPenalty] = useState<string | null>(null);
  const [lateFee, setLateFee] = useState<string | null>(null);
  const [savingEdit, setSavingEdit] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);

  if (loading) return <LoadingBlock label="Loading filing…" />;
  if (error) return <ErrorBlock message={error} />;
  if (!filing) return null;

  async function runTransition(status: FilingStatus) {
    setTransitionError(null);
    if (NEEDS_ACK.includes(status) && pendingStatus !== status) {
      setPendingStatus(status);
      return;
    }
    if (NEEDS_REASON.includes(status) && pendingStatus !== status) {
      setPendingStatus(status);
      return;
    }
    setTransitioning(true);
    try {
      const result = await filingsApi.transition(id, {
        status,
        acknowledgement_no: ackNo || undefined,
        rejection_reason: rejectionReason || undefined,
      });
      setPendingStatus(null);
      setAckNo("");
      setRejectionReason("");
      if (result.recorded_as_late) {
        setTransitionError("Recorded as late-filed: the due date had already passed.");
      }
      reload();
    } catch (err) {
      setTransitionError(err instanceof ApiError ? err.message : "Could not update the status");
    } finally {
      setTransitioning(false);
    }
  }

  async function saveEdits() {
    setSavingEdit(true);
    setEditError(null);
    try {
      const payload: Record<string, unknown> = {};
      if (notes !== null) payload.notes = notes;
      if (taxPayable !== null) payload.tax_payable_paise = toPaise(taxPayable);
      if (taxPaid !== null) payload.tax_paid_paise = toPaise(taxPaid);
      if (penalty !== null) payload.penalty_paise = toPaise(penalty);
      if (lateFee !== null) payload.late_fee_paise = toPaise(lateFee);
      await filingsApi.update(id, payload);
      reload();
    } catch (err) {
      setEditError(err instanceof ApiError ? err.message : "Could not save changes");
    } finally {
      setSavingEdit(false);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <button onClick={() => router.push("/filings")} className="mb-2 text-sm text-slate-500 hover:underline dark:text-slate-400">
          ← All filings
        </button>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold text-slate-900 dark:text-slate-50">
              {filing.title ?? filing.obligation_title ?? filing.filing_type ?? "Filing"}
            </h1>
            <p className="text-sm text-slate-500 dark:text-slate-400">
              {filing.regulation.toUpperCase()} · {filing.period_key} · {filing.obligation_code}
            </p>
          </div>
          <Badge value={filing.status} />
        </div>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-2">
          <Card title="Details">
            <dl className="grid grid-cols-2 gap-x-4 gap-y-3 text-sm sm:grid-cols-3">
              <Detail label="Due date" value={formatDate(filing.due_date)} />
              <Detail label="Effective due date" value={formatDate(filing.effective_due_date ?? filing.due_date)} />
              <Detail label="Extended due date" value={formatDate(filing.extended_due_date)} />
              <Detail label="Period" value={`${formatDate(filing.period_start)} – ${formatDate(filing.period_end)}`} />
              <Detail label="Acknowledgement no." value={filing.acknowledgement_no ?? "—"} />
              <Detail label="Submitted" value={formatDateTime(filing.submitted_at)} />
              <Detail label="Reviewed" value={formatDateTime(filing.reviewed_at)} />
              <Detail label="AI generated" value={filing.is_ai_generated ? `Yes (${filing.ai_model ?? "model"})` : "No"} />
              {filing.penalty_description && <Detail label="Penalty" value={filing.penalty_description} span />}
              {filing.rejection_reason && <Detail label="Rejection reason" value={filing.rejection_reason} span />}
            </dl>
          </Card>

          <Card title="Figures and notes">
            {editError && <ErrorBlock message={editError} />}
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <Field label="Tax payable (₹)">
                <Input value={taxPayable ?? toRupeeInput(filing.tax_payable_paise)} onChange={(e) => setTaxPayable(e.target.value)} />
              </Field>
              <Field label="Tax paid (₹)">
                <Input value={taxPaid ?? toRupeeInput(filing.tax_paid_paise)} onChange={(e) => setTaxPaid(e.target.value)} />
              </Field>
              <Field label="Penalty (₹)">
                <Input value={penalty ?? toRupeeInput(filing.penalty_paise)} onChange={(e) => setPenalty(e.target.value)} />
              </Field>
              <Field label="Late fee (₹)">
                <Input value={lateFee ?? toRupeeInput(filing.late_fee_paise)} onChange={(e) => setLateFee(e.target.value)} />
              </Field>
            </div>
            <div className="mt-4">
              <Field label="Notes">
                <textarea
                  className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-slate-500 focus:outline-none focus:ring-1 focus:ring-slate-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
                  rows={3}
                  value={notes ?? filing.notes ?? ""}
                  onChange={(e) => setNotes(e.target.value)}
                />
              </Field>
            </div>
            <div className="mt-4 flex justify-end">
              <Button onClick={saveEdits} disabled={savingEdit}>
                {savingEdit ? "Saving…" : "Save"}
              </Button>
            </div>
          </Card>
        </div>

        <div className="space-y-6">
          <Card title="Move status">
            {transitionError && <div className="mb-3"><ErrorBlock message={transitionError} /></div>}
            {filing.allowed_transitions.length === 0 ? (
              <p className="text-sm text-slate-500 dark:text-slate-400">This filing is in a final state.</p>
            ) : (
              <div className="space-y-3">
                {filing.allowed_transitions.map((status) => (
                  <div key={status}>
                    <Button
                      variant="secondary"
                      className="w-full justify-start"
                      disabled={transitioning}
                      onClick={() => runTransition(status)}
                    >
                      Move to {humanize(status)}
                    </Button>
                    {pendingStatus === status && NEEDS_ACK.includes(status) && (
                      <div className="mt-2 space-y-2 rounded-lg border border-slate-200 p-3 dark:border-slate-800">
                        <Field label="Acknowledgement number (optional)">
                          <Input value={ackNo} onChange={(e) => setAckNo(e.target.value)} />
                        </Field>
                        <Button className="w-full" disabled={transitioning} onClick={() => runTransition(status)}>
                          Confirm
                        </Button>
                      </div>
                    )}
                    {pendingStatus === status && NEEDS_REASON.includes(status) && (
                      <div className="mt-2 space-y-2 rounded-lg border border-slate-200 p-3 dark:border-slate-800">
                        <Field label="Rejection reason">
                          <Input value={rejectionReason} onChange={(e) => setRejectionReason(e.target.value)} required />
                        </Field>
                        <Button className="w-full" disabled={transitioning || !rejectionReason} onClick={() => runTransition(status)}>
                          Confirm
                        </Button>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </Card>

          <Card title="Cost summary">
            <dl className="space-y-2 text-sm">
              <Detail label="Tax payable" value={formatRupees(filing.tax_payable_paise)} />
              <Detail label="Tax paid" value={formatRupees(filing.tax_paid_paise)} />
              <Detail label="Penalty" value={formatRupees(filing.penalty_paise)} />
              <Detail label="Late fee" value={formatRupees(filing.late_fee_paise)} />
            </dl>
          </Card>

          <Link href={`/documents?filing_id=${filing.id}`} className="block text-center text-sm font-medium text-slate-600 hover:underline dark:text-slate-300">
            View attached documents →
          </Link>
        </div>
      </div>
    </div>
  );
}

function Detail({ label, value, span = false }: { label: string; value: string; span?: boolean }) {
  return (
    <div className={span ? "col-span-full" : undefined}>
      <dt className="text-xs font-medium uppercase tracking-wide text-slate-400">{label}</dt>
      <dd className="mt-0.5 text-slate-700 dark:text-slate-200">{value}</dd>
    </div>
  );
}
