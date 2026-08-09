"use client";

import { Suspense, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useApi } from "@/lib/use-api";
import { documentsApi } from "@/lib/resources";
import { ApiError } from "@/lib/api";
import { formatDate, humanize } from "@/lib/format";
import { Badge, Button, Card, EmptyState, ErrorBlock, Field, LoadingBlock, Select } from "@/components/ui";

const DOCUMENT_TYPES = [
  "regulatory_notice",
  "circular",
  "show_cause_notice",
  "filing_attachment",
  "generated_filing",
  "consent_record",
  "other",
];

function DocumentsPageInner() {
  const searchParams = useSearchParams();
  const filingId = searchParams.get("filing_id");
  const fileInput = useRef<HTMLInputElement>(null);
  const [docType, setDocType] = useState("other");
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  const { data, loading, error, reload } = useApi(
    () => documentsApi.list({ limit: 50, filing_id: filingId ? Number(filingId) : undefined }),
    [filingId]
  );

  async function handleUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    setUploadError(null);
    try {
      await documentsApi.upload(file, { type: docType, ...(filingId ? { filing_id: filingId } : {}) });
      reload();
    } catch (err) {
      setUploadError(err instanceof ApiError ? err.message : "Upload failed");
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold text-slate-900 dark:text-slate-50">Documents</h1>
      </div>

      <Card title="Upload a document">
        {uploadError && <div className="mb-3"><ErrorBlock message={uploadError} /></div>}
        <div className="flex flex-wrap items-end gap-3">
          <Field label="Type">
            <Select value={docType} onChange={(e) => setDocType(e.target.value)} className="w-52">
              {DOCUMENT_TYPES.map((t) => (
                <option key={t} value={t}>
                  {humanize(t)}
                </option>
              ))}
            </Select>
          </Field>
          <div>
            <input
              ref={fileInput}
              type="file"
              accept=".pdf,.doc,.docx,.png,.jpg,.jpeg,.html,.txt"
              disabled={uploading}
              onChange={handleUpload}
              className="text-sm text-slate-600 file:mr-3 file:rounded-lg file:border-0 file:bg-slate-900 file:px-3.5 file:py-2 file:text-sm file:font-medium file:text-white hover:file:bg-slate-700 dark:text-slate-300 dark:file:bg-slate-100 dark:file:text-slate-900"
            />
          </div>
          {uploading && <Button disabled>Uploading…</Button>}
        </div>
        <p className="mt-2 text-xs text-slate-400">PDF, Word, HTML, plain text, PNG or JPEG. Parsing runs in the background.</p>
      </Card>

      {loading && <LoadingBlock label="Loading documents…" />}
      {error && <ErrorBlock message={error} />}

      {data && (
        <Card>
          {data.items.length === 0 ? (
            <EmptyState title="No documents yet" description="Upload a regulatory notice or filing attachment above." />
          ) : (
            <ul className="divide-y divide-slate-100 dark:divide-slate-800">
              {data.items.map((doc) => (
                <li key={doc.id} className="flex flex-wrap items-center justify-between gap-2 py-3">
                  <div className="min-w-0">
                    <p className="font-medium text-slate-800 dark:text-slate-100">{doc.title}</p>
                    <p className="text-xs text-slate-500 dark:text-slate-400">
                      {humanize(doc.type)} · {doc.original_filename ?? "—"} · {formatDate(doc.created_at)}
                      {doc.extracted_deadline ? ` · deadline ${formatDate(doc.extracted_deadline)}` : ""}
                    </p>
                    {doc.extracted_summary && (
                      <p className="mt-1 max-w-2xl text-sm text-slate-600 dark:text-slate-300">{doc.extracted_summary}</p>
                    )}
                  </div>
                  <Badge value={doc.parse_status} />
                </li>
              ))}
            </ul>
          )}
        </Card>
      )}
    </div>
  );
}

export default function DocumentsPage() {
  return (
    <Suspense fallback={<LoadingBlock label="Loading documents…" />}>
      <DocumentsPageInner />
    </Suspense>
  );
}
