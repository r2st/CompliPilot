import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import FilingDetailPage from "./page";
import { filingsApi } from "@/lib/resources";
import { ApiError } from "@/lib/api";
import type { FilingResponse, FilingStatus } from "@/lib/types";

const push = vi.fn();

vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "7" }),
  useRouter: () => ({ push }),
}));

vi.mock("@/lib/resources", () => ({
  filingsApi: {
    get: vi.fn(),
    update: vi.fn(),
    transition: vi.fn(),
  },
}));

function filing(overrides: Partial<FilingResponse> = {}): FilingResponse {
  return {
    id: 7,
    organization_id: 1,
    obligation_id: 3,
    regulation: "gst",
    filing_type: "GSTR-3B",
    period_key: "2026-07",
    period_start: "2026-07-01",
    period_end: "2026-07-31",
    due_date: "2026-08-20",
    extended_due_date: null,
    status: "draft",
    is_ai_generated: false,
    created_at: "2026-07-01T00:00:00Z",
    title: "GSTR-3B — July 2026",
    effective_due_date: "2026-08-20",
    days_until_due: 10,
    urgency: "normal",
    is_open: true,
    data_json: null,
    template_id: null,
    ai_model: null,
    ai_generated_at: null,
    ai_confidence: null,
    ai_notes: null,
    prepared_by_id: null,
    reviewed_by_id: null,
    reviewed_at: null,
    submitted_at: null,
    submitted_by_id: null,
    acknowledgement_no: null,
    rejection_reason: null,
    tax_payable_paise: null,
    tax_paid_paise: null,
    penalty_paise: null,
    late_fee_paise: null,
    notes: null,
    updated_at: "2026-07-01T00:00:00Z",
    allowed_transitions: ["in_review"] as FilingStatus[],
    obligation_code: "gst.gstr3b.monthly",
    obligation_title: "Monthly GST return",
    penalty_description: null,
    ...overrides,
  };
}

describe("FilingDetailPage", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("shows an error state when the filing fails to load", async () => {
    vi.mocked(filingsApi.get).mockRejectedValue(new ApiError(404, "not_found", "No such filing"));
    render(<FilingDetailPage />);

    expect(await screen.findByText("No such filing")).toBeInTheDocument();
  });

  it("renders the filing's details", async () => {
    vi.mocked(filingsApi.get).mockResolvedValue(filing());
    render(<FilingDetailPage />);

    expect(await screen.findByText("GSTR-3B — July 2026")).toBeInTheDocument();
    expect(screen.getByText("GST · 2026-07 · gst.gstr3b.monthly")).toBeInTheDocument();
  });

  it("says the filing is in a final state when there are no allowed transitions", async () => {
    vi.mocked(filingsApi.get).mockResolvedValue(filing({ allowed_transitions: [] }));
    render(<FilingDetailPage />);

    expect(await screen.findByText("This filing is in a final state.")).toBeInTheDocument();
  });

  it("runs a plain transition (no acknowledgement or reason) immediately", async () => {
    vi.mocked(filingsApi.get).mockResolvedValue(filing({ allowed_transitions: ["in_review"] as FilingStatus[] }));
    vi.mocked(filingsApi.transition).mockResolvedValue({
      filing: filing({ status: "in_review" }),
      previous_status: "draft",
      recorded_as_late: false,
    });
    render(<FilingDetailPage />);

    const moveButton = await screen.findByRole("button", { name: "Move to In Review" });
    await act(async () => {
      fireEvent.click(moveButton);
    });

    expect(filingsApi.transition).toHaveBeenCalledWith(7, {
      status: "in_review",
      acknowledgement_no: undefined,
      rejection_reason: undefined,
    });
    await waitFor(() => expect(filingsApi.get).toHaveBeenCalledTimes(2));
  });

  it("requires a two-step confirm with an acknowledgement number before marking submitted", async () => {
    vi.mocked(filingsApi.get).mockResolvedValue(filing({ allowed_transitions: ["submitted"] as FilingStatus[] }));
    vi.mocked(filingsApi.transition).mockResolvedValue({
      filing: filing({ status: "submitted" }),
      previous_status: "draft",
      recorded_as_late: false,
    });
    render(<FilingDetailPage />);

    const moveButton = await screen.findByRole("button", { name: "Move to Submitted" });
    await act(async () => {
      fireEvent.click(moveButton);
    });

    // First click only opens the acknowledgement field — no call yet.
    expect(filingsApi.transition).not.toHaveBeenCalled();
    const ackField = await screen.findByLabelText("Acknowledgement number (optional)");
    fireEvent.change(ackField, { target: { value: "ACK-2026-001" } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    });

    expect(filingsApi.transition).toHaveBeenCalledWith(7, {
      status: "submitted",
      acknowledgement_no: "ACK-2026-001",
      rejection_reason: undefined,
    });
  });

  it("surfaces the late-filed notice when the server records a submission as late", async () => {
    vi.mocked(filingsApi.get).mockResolvedValue(filing({ allowed_transitions: ["submitted"] as FilingStatus[] }));
    vi.mocked(filingsApi.transition).mockResolvedValue({
      filing: filing({ status: "late_filed" }),
      previous_status: "draft",
      recorded_as_late: true,
    });
    render(<FilingDetailPage />);

    const moveButton = await screen.findByRole("button", { name: "Move to Submitted" });
    await act(async () => {
      fireEvent.click(moveButton);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    });

    expect(
      await screen.findByText("Recorded as late-filed: the due date had already passed.")
    ).toBeInTheDocument();
  });

  it("requires a non-empty rejection reason before the Confirm button is enabled", async () => {
    vi.mocked(filingsApi.get).mockResolvedValue(filing({ allowed_transitions: ["rejected"] as FilingStatus[] }));
    render(<FilingDetailPage />);

    const moveButton = await screen.findByRole("button", { name: "Move to Rejected" });
    await act(async () => {
      fireEvent.click(moveButton);
    });

    const confirm = screen.getByRole("button", { name: "Confirm" });
    expect(confirm).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Rejection reason"), {
      target: { value: "GSTIN mismatch with PAN records" },
    });
    expect(confirm).not.toBeDisabled();
  });

  it("shows the server's error message when a transition is rejected", async () => {
    vi.mocked(filingsApi.get).mockResolvedValue(filing({ allowed_transitions: ["in_review"] as FilingStatus[] }));
    vi.mocked(filingsApi.transition).mockRejectedValue(
      new ApiError(409, "invalid_transition", "Cannot move a filing to In Review from Draft")
    );
    render(<FilingDetailPage />);

    const moveButton = await screen.findByRole("button", { name: "Move to In Review" });
    await act(async () => {
      fireEvent.click(moveButton);
    });

    expect(
      await screen.findByText("Cannot move a filing to In Review from Draft")
    ).toBeInTheDocument();
  });

  it("saves edited figures, converting rupees to paise and omitting untouched fields", async () => {
    vi.mocked(filingsApi.get).mockResolvedValue(filing());
    vi.mocked(filingsApi.update).mockResolvedValue(filing());
    render(<FilingDetailPage />);

    fireEvent.change(await screen.findByLabelText("Tax payable (₹)"), { target: { value: "12500.50" } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });

    await waitFor(() => expect(filingsApi.update).toHaveBeenCalled());
    const payload = vi.mocked(filingsApi.update).mock.calls[0][1];
    expect(payload).toEqual({ tax_payable_paise: 1250050 });
  });

  it("shows the server's error message when saving figures fails", async () => {
    vi.mocked(filingsApi.get).mockResolvedValue(filing());
    vi.mocked(filingsApi.update).mockRejectedValue(new ApiError(422, "validation_error", "Penalty cannot be negative"));
    render(<FilingDetailPage />);

    fireEvent.change(await screen.findByLabelText("Penalty (₹)"), { target: { value: "-5" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });

    expect(await screen.findByText("Penalty cannot be negative")).toBeInTheDocument();
  });
});
