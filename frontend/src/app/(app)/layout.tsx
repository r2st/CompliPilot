"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { alertsApi } from "@/lib/resources";
import { ROLE_RANK } from "@/lib/types";
import { LoadingBlock } from "@/components/ui";
import { ClientSwitcher } from "@/components/client-switcher";

const NAV: { href: string; label: string; minRole: number; caFirmOnly?: boolean }[] = [
  { href: "/dashboard", label: "Dashboard", minRole: 0 },
  { href: "/calendar", label: "Calendar", minRole: 0 },
  { href: "/filings", label: "Filings", minRole: 0 },
  { href: "/obligations", label: "Obligations", minRole: 0 },
  { href: "/clients", label: "Clients", minRole: 0, caFirmOnly: true },
  { href: "/documents", label: "Documents", minRole: 0 },
  { href: "/dpdp", label: "DPDP toolkit", minRole: 0 },
  { href: "/alerts", label: "Alerts", minRole: 0 },
  { href: "/audit", label: "Audit trail", minRole: 2 },
  { href: "/settings", label: "Settings", minRole: 0 },
];

export default function AppLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const { loading, isAuthenticated, user, session, logout } = useAuth();
  const [alertCount, setAlertCount] = useState<number | null>(null);

  useEffect(() => {
    if (loading || !isAuthenticated) return;
    alertsApi
      .summary()
      .then((s) =>
        setAlertCount(
          s.unacknowledged_impacts + s.overdue_filings + s.open_breach_incidents + s.overdue_data_requests
        )
      )
      .catch(() => setAlertCount(null));
  }, [loading, isAuthenticated, pathname]);

  useEffect(() => {
    if (!loading && !isAuthenticated) router.replace("/login");
  }, [loading, isAuthenticated, router]);

  if (loading || !isAuthenticated || !user || !session) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <LoadingBlock label="Loading your workspace…" />
      </div>
    );
  }

  const roleRank = ROLE_RANK[user.role];
  const isCaFirmMember = session.is_delegated || session.available_clients.length > 0;

  return (
    <div className="flex min-h-screen bg-slate-50 dark:bg-slate-950">
      <aside className="hidden w-60 shrink-0 flex-col border-r border-slate-200 bg-white px-4 py-6 dark:border-slate-800 dark:bg-slate-900 md:flex">
        <Link href="/dashboard" className="mb-8 px-2 text-lg font-semibold tracking-tight text-slate-900 dark:text-slate-50">
          CompliPilot
        </Link>
        <nav className="flex flex-1 flex-col gap-1">
          {NAV.filter((item) => !item.caFirmOnly || isCaFirmMember)
            .filter((item) => roleRank >= item.minRole)
            .map((item) => {
              const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`rounded-lg px-3 py-2 text-sm font-medium transition ${
                    active
                      ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-900"
                      : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
                  }`}
                >
                  {item.label}
                </Link>
              );
            })}
        </nav>
        <button
          onClick={logout}
          className="mt-4 rounded-lg px-3 py-2 text-left text-sm font-medium text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800"
        >
          Sign out
        </button>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-slate-200 bg-white px-4 py-3 dark:border-slate-800 dark:bg-slate-900 md:px-6">
          <ClientSwitcher />
          <div className="flex items-center gap-4">
            <Link
              href="/alerts"
              className="relative rounded-lg p-2 text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800"
              aria-label="Alerts"
            >
              <BellIcon />
              {alertCount !== null && alertCount > 0 && (
                <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-rose-500 px-1 text-[10px] font-semibold text-white">
                  {alertCount > 99 ? "99+" : alertCount}
                </span>
              )}
            </Link>
            <div className="hidden text-right sm:block">
              <p className="text-sm font-medium text-slate-800 dark:text-slate-100">{user.full_name}</p>
              <p className="text-xs text-slate-500 dark:text-slate-400">{session.organization_name}</p>
            </div>
          </div>
        </header>
        <main className="flex-1 px-4 py-6 md:px-6">{children}</main>
      </div>
    </div>
  );
}

function BellIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
      <path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M13.73 21a2 2 0 0 1-3.46 0" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
