import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { Badge, Stat } from "./ui";

describe("Badge", () => {
  it("renders a humanized label by default", () => {
    render(<Badge value="in_review" />);
    expect(screen.getByText("In Review")).toBeInTheDocument();
  });

  it("renders an explicit label when given one", () => {
    render(<Badge value="high" label="Sensitive" />);
    expect(screen.getByText("Sensitive")).toBeInTheDocument();
  });

  it("falls back to the neutral tone for an unrecognized value", () => {
    render(<Badge value="some_unmapped_status" />);
    const badge = screen.getByText("Some Unmapped Status");
    expect(badge.className).toContain("bg-slate-100");
  });

  it("renders a dash for a null value", () => {
    render(<Badge value={null} />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });
});

describe("Stat", () => {
  it("renders the label, value and sublabel", () => {
    render(<Stat label="Open requests" value={4} sublabel="1 overdue" />);
    expect(screen.getByText("Open requests")).toBeInTheDocument();
    expect(screen.getByText("4")).toBeInTheDocument();
    expect(screen.getByText("1 overdue")).toBeInTheDocument();
  });
});
