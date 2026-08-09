"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth-context";
import { authApi } from "@/lib/resources";
import { ApiError } from "@/lib/api";

export function ClientSwitcher() {
  const router = useRouter();
  const { session, setSession } = useAuth();
  const [switching, setSwitching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!session) return <div />;

  const isCaFirmMember = session.is_delegated || session.available_clients.length > 0;
  if (!isCaFirmMember) {
    return (
      <div className="text-sm font-medium text-slate-700 dark:text-slate-200">{session.organization_name}</div>
    );
  }

  const currentValue = session.is_delegated ? String(session.organization_id) : "home";

  async function handleChange(value: string) {
    setError(null);
    setSwitching(true);
    try {
      const clientOrgId = value === "home" ? null : Number(value);
      const tokens = await authApi.switchClient(clientOrgId);
      setSession(tokens);
      router.push("/dashboard");
      router.refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not switch context");
    } finally {
      setSwitching(false);
    }
  }

  return (
    <div className="flex items-center gap-2">
      <span className="hidden text-xs font-medium uppercase tracking-wide text-slate-400 sm:inline">
        Acting as
      </span>
      <select
        value={currentValue}
        disabled={switching}
        onChange={(e) => handleChange(e.target.value)}
        className="rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-sm font-medium text-slate-800 focus:outline-none focus:ring-1 focus:ring-slate-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
      >
        <option value="home">{session.home_organization_name} (your firm)</option>
        {session.available_clients.map((c) => (
          <option key={c.client_id} value={c.organization_id}>
            {c.name}
          </option>
        ))}
      </select>
      {error && <span className="text-xs text-rose-600 dark:text-rose-400">{error}</span>}
    </div>
  );
}
