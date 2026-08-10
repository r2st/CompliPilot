import { Children, cloneElement, isValidElement, useId, type ReactElement, type ReactNode } from "react";
import { humanize } from "@/lib/format";

export function Card({
  children,
  className = "",
  title,
  action,
}: {
  children: ReactNode;
  className?: string;
  title?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className={`rounded-xl border border-slate-200 bg-white shadow-sm dark:border-slate-800 dark:bg-slate-900 ${className}`}>
      {(title || action) && (
        <div className="flex items-center justify-between border-b border-slate-100 px-5 py-3 dark:border-slate-800">
          {title && <h2 className="text-sm font-semibold text-slate-700 dark:text-slate-200">{title}</h2>}
          {action}
        </div>
      )}
      <div className="p-5">{children}</div>
    </div>
  );
}

export function Stat({
  label,
  value,
  sublabel,
  tone = "default",
}: {
  label: string;
  value: ReactNode;
  sublabel?: string;
  tone?: "default" | "good" | "warn" | "bad";
}) {
  const toneClass = {
    default: "text-slate-900 dark:text-slate-50",
    good: "text-emerald-600 dark:text-emerald-400",
    warn: "text-amber-600 dark:text-amber-400",
    bad: "text-rose-600 dark:text-rose-400",
  }[tone];
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">{label}</span>
      <span className={`text-2xl font-semibold tabular-nums ${toneClass}`}>{value}</span>
      {sublabel && <span className="text-xs text-slate-500 dark:text-slate-400">{sublabel}</span>}
    </div>
  );
}

const STATUS_TONE: Record<string, string> = {
  not_started: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300",
  draft: "bg-sky-100 text-sky-700 dark:bg-sky-950 dark:text-sky-300",
  in_review: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  approved: "bg-indigo-100 text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300",
  submitted: "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300",
  acknowledged: "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300",
  rejected: "bg-rose-100 text-rose-700 dark:bg-rose-950 dark:text-rose-300",
  late_filed: "bg-orange-100 text-orange-700 dark:bg-orange-950 dark:text-orange-300",
  not_applicable: "bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400",
  critical: "bg-rose-100 text-rose-700 dark:bg-rose-950 dark:text-rose-300",
  high: "bg-orange-100 text-orange-700 dark:bg-orange-950 dark:text-orange-300",
  medium: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  low: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300",
  none: "bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400",
  overdue: "bg-rose-100 text-rose-700 dark:bg-rose-950 dark:text-rose-300",
  urgent: "bg-orange-100 text-orange-700 dark:bg-orange-950 dark:text-orange-300",
  soon: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  normal: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300",
  active: "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300",
  paused: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  terminated: "bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400",
  pending: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300",
  processing: "bg-sky-100 text-sky-700 dark:bg-sky-950 dark:text-sky-300",
  parsed: "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300",
  failed: "bg-rose-100 text-rose-700 dark:bg-rose-950 dark:text-rose-300",
  sent: "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300",
  skipped: "bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400",
  open: "bg-rose-100 text-rose-700 dark:bg-rose-950 dark:text-rose-300",
  contained: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  notified: "bg-sky-100 text-sky-700 dark:bg-sky-950 dark:text-sky-300",
  closed: "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300",
  received: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300",
  verifying: "bg-sky-100 text-sky-700 dark:bg-sky-950 dark:text-sky-300",
  in_progress: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  completed: "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300",
  granted: "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300",
  withdrawn: "bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400",
};

export function Badge({ value, label }: { value: string | null | undefined; label?: string }) {
  const tone = STATUS_TONE[value ?? ""] ?? "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300";
  return (
    <span className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${tone}`}>
      {label ?? humanize(value)}
    </span>
  );
}

export function Spinner({ className = "" }: { className?: string }) {
  return (
    <span
      className={`inline-block h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent align-middle ${className}`}
      role="status"
      aria-label="Loading"
    />
  );
}

export function LoadingBlock({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-16 text-sm text-slate-500 dark:text-slate-400">
      <Spinner /> {label}
    </div>
  );
}

export function ErrorBlock({ message }: { message: string }) {
  return (
    <div className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700 dark:border-rose-900 dark:bg-rose-950 dark:text-rose-300">
      {message}
    </div>
  );
}

export function EmptyState({ title, description }: { title: string; description?: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-1 py-16 text-center">
      <p className="text-sm font-medium text-slate-700 dark:text-slate-200">{title}</p>
      {description && <p className="max-w-sm text-sm text-slate-500 dark:text-slate-400">{description}</p>}
    </div>
  );
}

export function Button({
  children,
  variant = "primary",
  className = "",
  ...rest
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "secondary" | "danger" | "ghost" }) {
  const base = "inline-flex items-center justify-center gap-2 rounded-lg px-3.5 py-2 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-50";
  const variants = {
    primary: "bg-slate-900 text-white hover:bg-slate-700 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white",
    secondary: "border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200 dark:hover:bg-slate-800",
    danger: "bg-rose-600 text-white hover:bg-rose-500",
    ghost: "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800",
  };
  return (
    <button className={`${base} ${variants[variant]} ${className}`} {...rest}>
      {children}
    </button>
  );
}

export function Input(props: React.InputHTMLAttributes<HTMLInputElement>) {
  const { className = "", ...rest } = props;
  return (
    <input
      className={`w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 focus:border-slate-500 focus:outline-none focus:ring-1 focus:ring-slate-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100 ${className}`}
      {...rest}
    />
  );
}

export function Select(props: React.SelectHTMLAttributes<HTMLSelectElement>) {
  const { className = "", children, ...rest } = props;
  return (
    <select
      className={`w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-slate-500 focus:outline-none focus:ring-1 focus:ring-slate-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100 ${className}`}
      {...rest}
    >
      {children}
    </select>
  );
}

export function Label({ children, htmlFor }: { children: ReactNode; htmlFor?: string }) {
  return (
    <label htmlFor={htmlFor} className="mb-1 block text-xs font-medium text-slate-600 dark:text-slate-400">
      {children}
    </label>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  const id = useId();
  // The control is always the first child (a helper <p> or similar may
  // follow, e.g. the TOTP hint on the login page) — cloning only that one
  // wires the label to the actual input without touching its siblings, and
  // leaves an id the caller already set alone.
  const [control, ...rest] = Children.toArray(children);
  const hasOwnId = isValidElement<{ id?: string }>(control) && !!control.props.id;
  const labelable =
    isValidElement<{ id?: string }>(control) && !hasOwnId
      ? cloneElement(control as ReactElement<{ id?: string }>, { id })
      : control;
  const forId = hasOwnId ? (control as ReactElement<{ id?: string }>).props.id : id;

  return (
    <div>
      <Label htmlFor={forId}>{label}</Label>
      {labelable}
      {rest}
    </div>
  );
}
