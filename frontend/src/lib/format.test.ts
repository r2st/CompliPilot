import { describe, expect, it } from "vitest";
import { daysUntilLabel, formatDate, formatDateTime, formatRupees, humanize } from "./format";

describe("formatRupees", () => {
  it("renders whole rupees without decimals", () => {
    expect(formatRupees(150000)).toBe("₹1,500");
  });

  it("renders fractional rupees with two decimals", () => {
    expect(formatRupees(150050)).toBe("₹1,500.50");
  });

  it("renders a dash for null or undefined", () => {
    expect(formatRupees(null)).toBe("—");
    expect(formatRupees(undefined)).toBe("—");
  });

  it("renders zero as ₹0, not a dash", () => {
    expect(formatRupees(0)).toBe("₹0");
  });
});

describe("formatDate", () => {
  it("renders a date-only string without a timezone shift", () => {
    // A bare "2026-01-31" must not roll back to the 30th in a negative-UTC-offset zone.
    expect(formatDate("2026-01-31")).toBe("31 Jan 2026");
  });

  it("renders a dash for null or undefined", () => {
    expect(formatDate(null)).toBe("—");
    expect(formatDate(undefined)).toBe("—");
  });

  it("passes through a value it cannot parse", () => {
    expect(formatDate("not-a-date")).toBe("not-a-date");
  });
});

describe("formatDateTime", () => {
  it("renders a dash for null or undefined", () => {
    expect(formatDateTime(null)).toBe("—");
  });

  it("formats an ISO datetime", () => {
    const result = formatDateTime("2026-08-09T14:30:00Z");
    expect(result).toMatch(/2026/);
  });
});

describe("humanize", () => {
  it("title-cases a snake_case value", () => {
    expect(humanize("not_started")).toBe("Not Started");
  });

  it("handles a single word", () => {
    expect(humanize("draft")).toBe("Draft");
  });

  it("renders a dash for null, undefined or empty", () => {
    expect(humanize(null)).toBe("—");
    expect(humanize(undefined)).toBe("—");
    expect(humanize("")).toBe("—");
  });
});

describe("daysUntilLabel", () => {
  it("labels a positive count as upcoming", () => {
    expect(daysUntilLabel(5)).toBe("in 5d");
  });

  it("labels zero as due today", () => {
    expect(daysUntilLabel(0)).toBe("Due today");
  });

  it("labels a negative count as overdue", () => {
    expect(daysUntilLabel(-3)).toBe("3d overdue");
  });

  it("renders a dash for null or undefined", () => {
    expect(daysUntilLabel(null)).toBe("—");
    expect(daysUntilLabel(undefined)).toBe("—");
  });
});
