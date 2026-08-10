import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import DocumentsPage from "./page";
import { documentsApi } from "@/lib/resources";
import { ApiError } from "@/lib/api";
import type { DocumentSummary } from "@/lib/types";

let searchParams = new URLSearchParams();

vi.mock("next/navigation", () => ({
  useSearchParams: () => searchParams,
}));

vi.mock("@/lib/resources", () => ({
  documentsApi: {
    list: vi.fn(),
    upload: vi.fn(),
  },
}));

function doc(overrides: Partial<DocumentSummary> = {}): DocumentSummary {
  return {
    id: 1,
    organization_id: 1,
    type: "other" as const,
    title: "GST show-cause notice",
    original_filename: "notice.pdf",
    mime_type: "application/pdf",
    size_bytes: 1024,
    content_hash: "abc123",
    filing_id: null,
    regulation: null,
    parse_status: "pending" as const,
    parsed_at: null,
    extraction_method: null,
    extracted_deadline: null,
    extracted_summary: null,
    uploaded_by_id: 1,
    created_at: "2026-07-01T00:00:00Z",
    ...overrides,
  };
}

function page(items: DocumentSummary[]) {
  return { items, total: items.length, limit: 50, offset: 0 };
}

describe("DocumentsPage", () => {
  afterEach(() => {
    vi.clearAllMocks();
    searchParams = new URLSearchParams();
  });

  it("shows an empty state when there are no documents", async () => {
    vi.mocked(documentsApi.list).mockResolvedValue(page([]));
    render(<DocumentsPage />);

    expect(await screen.findByText("No documents yet")).toBeInTheDocument();
  });

  it("lists documents with their parse status and extracted metadata", async () => {
    vi.mocked(documentsApi.list).mockResolvedValue(
      page([
        doc({
          extracted_deadline: "2026-08-20",
          extracted_summary: "GST return due for July 2026.",
          parse_status: "parsed",
        }),
      ])
    );
    render(<DocumentsPage />);

    expect(await screen.findByText("GST show-cause notice")).toBeInTheDocument();
    expect(screen.getByText("GST return due for July 2026.")).toBeInTheDocument();
    expect(screen.getByText(/deadline/)).toBeInTheDocument();
  });

  it("shows an error state when the list fails to load", async () => {
    vi.mocked(documentsApi.list).mockRejectedValue(new ApiError(500, "server_error", "Could not load documents"));
    render(<DocumentsPage />);

    expect(await screen.findByText("Could not load documents")).toBeInTheDocument();
  });

  it("scopes the list to a filing when filing_id is in the query string", async () => {
    searchParams = new URLSearchParams({ filing_id: "42" });
    vi.mocked(documentsApi.list).mockResolvedValue(page([]));
    render(<DocumentsPage />);

    await waitFor(() =>
      expect(documentsApi.list).toHaveBeenCalledWith({ limit: 50, filing_id: 42 })
    );
  });

  it("uploads the selected file with the chosen type and reloads the list", async () => {
    vi.mocked(documentsApi.list).mockResolvedValue(page([]));
    vi.mocked(documentsApi.upload).mockResolvedValue({ id: 2, parse_status: "pending" } as never);
    render(<DocumentsPage />);
    await screen.findByText("No documents yet");

    const file = new File(["contents"], "notice.pdf", { type: "application/pdf" });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;

    await act(async () => {
      fireEvent.change(input, { target: { files: [file] } });
    });

    expect(documentsApi.upload).toHaveBeenCalledWith(file, { type: "other" });
    await waitFor(() => expect(documentsApi.list).toHaveBeenCalledTimes(2));
    // The input is cleared so the same file can be re-selected.
    expect(input.value).toBe("");
  });

  it("passes the filing_id from the query string through to the upload", async () => {
    searchParams = new URLSearchParams({ filing_id: "42" });
    vi.mocked(documentsApi.list).mockResolvedValue(page([]));
    vi.mocked(documentsApi.upload).mockResolvedValue({ id: 2, parse_status: "pending" } as never);
    render(<DocumentsPage />);
    await waitFor(() => expect(documentsApi.list).toHaveBeenCalled());

    const file = new File(["contents"], "notice.pdf", { type: "application/pdf" });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;

    await act(async () => {
      fireEvent.change(input, { target: { files: [file] } });
    });

    expect(documentsApi.upload).toHaveBeenCalledWith(file, { type: "other", filing_id: "42" });
  });

  it("shows the server's error message when an upload fails, and still clears the input", async () => {
    vi.mocked(documentsApi.list).mockResolvedValue(page([]));
    vi.mocked(documentsApi.upload).mockRejectedValue(
      new ApiError(413, "too_large", "File exceeds the 20MB limit")
    );
    render(<DocumentsPage />);
    await screen.findByText("No documents yet");

    const file = new File(["contents"], "huge.pdf", { type: "application/pdf" });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;

    await act(async () => {
      fireEvent.change(input, { target: { files: [file] } });
    });

    expect(await screen.findByText("File exceeds the 20MB limit")).toBeInTheDocument();
    expect(input.value).toBe("");
  });
});
