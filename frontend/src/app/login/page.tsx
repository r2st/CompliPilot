"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { authApi } from "@/lib/resources";
import { isTotpChallenge } from "@/lib/types";
import { Button, ErrorBlock, Field, Input } from "@/components/ui";

export default function LoginPage() {
  const router = useRouter();
  const { setSession } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [totpCode, setTotpCode] = useState("");
  const [needsTotp, setNeedsTotp] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const result = await authApi.login(email, password, needsTotp ? totpCode : undefined);
      if (isTotpChallenge(result)) {
        setNeedsTotp(true);
        return;
      }
      setSession(result);
      router.push("/dashboard");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not sign in");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 px-4 dark:bg-slate-950">
      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <h1 className="text-2xl font-semibold tracking-tight text-slate-900 dark:text-slate-50">CompliPilot</h1>
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
            Sign in to your compliance workspace
          </p>
        </div>
        <form
          onSubmit={handleSubmit}
          className="space-y-4 rounded-xl border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-800 dark:bg-slate-900"
        >
          {error && <ErrorBlock message={error} />}

          {!needsTotp && (
            <>
              <Field label="Email">
                <Input
                  type="email"
                  required
                  autoFocus
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@company.com"
                />
              </Field>
              <Field label="Password">
                <Input
                  type="password"
                  required
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                />
              </Field>
            </>
          )}

          {needsTotp && (
            <Field label="Authenticator code">
              <Input
                type="text"
                inputMode="numeric"
                autoFocus
                required
                maxLength={10}
                value={totpCode}
                onChange={(e) => setTotpCode(e.target.value)}
                placeholder="123456"
              />
              <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                Enter the 6-digit code from your authenticator app.
              </p>
            </Field>
          )}

          <Button type="submit" disabled={submitting} className="w-full">
            {submitting ? "Signing in…" : needsTotp ? "Verify" : "Sign in"}
          </Button>
        </form>
        <p className="mt-6 text-center text-sm text-slate-500 dark:text-slate-400">
          New to CompliPilot?{" "}
          <Link href="/register" className="font-medium text-slate-900 underline dark:text-slate-100">
            Create an organization
          </Link>
        </p>
      </div>
    </div>
  );
}
