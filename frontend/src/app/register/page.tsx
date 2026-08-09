"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { authApi } from "@/lib/resources";
import { Button, ErrorBlock, Field, Input, Select } from "@/components/ui";

const ENTITY_TYPES = [
  ["private_limited", "Private Limited Company"],
  ["public_limited", "Public Limited Company"],
  ["one_person_company", "One Person Company"],
  ["llp", "LLP"],
  ["partnership", "Partnership"],
  ["proprietorship", "Proprietorship"],
  ["trust", "Trust"],
  ["society", "Society"],
  ["section_8", "Section 8 Company"],
  ["foreign_company", "Foreign Company"],
  ["huf", "HUF"],
];

export default function RegisterPage() {
  const router = useRouter();
  const { setSession } = useAuth();
  const [organizationType, setOrganizationType] = useState<"company" | "ca_firm">("company");
  const [organizationName, setOrganizationName] = useState("");
  const [entityType, setEntityType] = useState("");
  const [state, setState] = useState("");
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const tokens = await authApi.register({
        organization_name: organizationName,
        organization_type: organizationType,
        entity_type: organizationType === "company" ? entityType || null : null,
        state: state || null,
        email,
        full_name: fullName,
        phone: phone || null,
        password,
      });
      setSession(tokens);
      router.push("/dashboard");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not create your organization");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 px-4 py-10 dark:bg-slate-950">
      <div className="w-full max-w-md">
        <div className="mb-8 text-center">
          <h1 className="text-2xl font-semibold tracking-tight text-slate-900 dark:text-slate-50">CompliPilot</h1>
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
            Register your organization and its first Admin
          </p>
        </div>
        <form
          onSubmit={handleSubmit}
          className="space-y-4 rounded-xl border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-800 dark:bg-slate-900"
        >
          {error && <ErrorBlock message={error} />}

          <Field label="We are a">
            <Select value={organizationType} onChange={(e) => setOrganizationType(e.target.value as "company" | "ca_firm")}>
              <option value="company">Company / SMB filing directly</option>
              <option value="ca_firm">CA firm managing clients</option>
            </Select>
          </Field>

          <Field label="Organization name">
            <Input required minLength={2} value={organizationName} onChange={(e) => setOrganizationName(e.target.value)} />
          </Field>

          {organizationType === "company" && (
            <Field label="Entity type">
              <Select value={entityType} onChange={(e) => setEntityType(e.target.value)}>
                <option value="">Select…</option>
                {ENTITY_TYPES.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </Select>
            </Field>
          )}

          <Field label="State">
            <Input value={state} onChange={(e) => setState(e.target.value)} placeholder="Maharashtra" />
          </Field>

          <div className="border-t border-slate-100 pt-4 dark:border-slate-800">
            <p className="mb-3 text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Your account
            </p>
            <div className="space-y-4">
              <Field label="Full name">
                <Input required minLength={2} value={fullName} onChange={(e) => setFullName(e.target.value)} />
              </Field>
              <Field label="Email">
                <Input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
              </Field>
              <Field label="Phone (optional)">
                <Input value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="9876543210" />
              </Field>
              <Field label="Password">
                <Input
                  type="password"
                  required
                  minLength={8}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="At least 8 characters"
                />
              </Field>
            </div>
          </div>

          <Button type="submit" disabled={submitting} className="w-full">
            {submitting ? "Creating…" : "Create organization"}
          </Button>
        </form>
        <p className="mt-6 text-center text-sm text-slate-500 dark:text-slate-400">
          Already have an account?{" "}
          <Link href="/login" className="font-medium text-slate-900 underline dark:text-slate-100">
            Sign in
          </Link>
        </p>
      </div>
    </div>
  );
}
