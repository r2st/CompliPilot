import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { Badge, Field, Input, Stat } from "./ui";

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

describe("Field", () => {
  it("associates the label with its control, so getByLabelText finds it", () => {
    render(
      <Field label="Email">
        <Input value="" onChange={() => {}} />
      </Field>
    );
    expect(screen.getByLabelText("Email")).toBeInstanceOf(HTMLInputElement);
  });

  it("gives every field a distinct id, even with a duplicate label", () => {
    render(
      <>
        <Field label="Name">
          <Input value="" onChange={() => {}} />
        </Field>
        <Field label="Name">
          <Input value="" onChange={() => {}} />
        </Field>
      </>
    );
    const [first, second] = screen.getAllByLabelText("Name") as HTMLInputElement[];
    expect(first.id).not.toBe(second.id);
  });

  it("does not overwrite a control's own explicit id", () => {
    render(
      <Field label="Email">
        <Input id="custom-email-id" value="" onChange={() => {}} />
      </Field>
    );
    expect(screen.getByLabelText("Email").id).toBe("custom-email-id");
  });

  it("still labels the control when a helper node follows it", () => {
    render(
      <Field label="Authenticator code">
        <Input value="" onChange={() => {}} />
        <p>Enter the 6-digit code from your authenticator app.</p>
      </Field>
    );
    expect(screen.getByLabelText("Authenticator code")).toBeInstanceOf(HTMLInputElement);
    expect(screen.getByText("Enter the 6-digit code from your authenticator app.")).toBeInTheDocument();
  });
});
