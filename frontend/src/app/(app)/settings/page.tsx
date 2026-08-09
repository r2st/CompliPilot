"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { useApi } from "@/lib/use-api";
import { authApi, organizationsApi } from "@/lib/resources";
import { ApiError } from "@/lib/api";
import { ROLE_RANK, ROLE_LABEL, type UserRole } from "@/lib/types";
import { Badge, Button, Card, ErrorBlock, Field, Input, LoadingBlock, Select } from "@/components/ui";

export default function SettingsPage() {
  const { user } = useAuth();
  const isAdmin = user ? ROLE_RANK[user.role] >= ROLE_RANK.admin : false;

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold text-slate-900 dark:text-slate-50">Settings</h1>
      <OrganizationCard />
      <SecurityCard />
      {isAdmin && <UsersCard />}
    </div>
  );
}

function OrganizationCard() {
  const { session, refreshSession } = useAuth();
  const { data: org, loading, error, reload } = useApi(() => organizationsApi.me(), []);
  const [form, setForm] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  if (loading) return <LoadingBlock label="Loading organization…" />;
  if (error) return <ErrorBlock message={error} />;
  if (!org) return null;

  const value = (key: string, fallback: string | null | undefined) => form[key] ?? fallback ?? "";

  async function save() {
    setSaving(true);
    setSaveError(null);
    setSaved(false);
    try {
      const payload: Record<string, unknown> = {};
      for (const key of ["name", "legal_name", "state", "industry", "contact_email", "contact_phone", "address"]) {
        if (key in form) payload[key] = form[key] || null;
      }
      for (const key of ["gstin", "pan", "cin", "llpin", "tan"]) {
        if (key in form && form[key]) payload[key] = form[key];
      }
      await organizationsApi.update(payload);
      await Promise.all([reload(), refreshSession()]);
      setSaved(true);
    } catch (err) {
      setSaveError(err instanceof ApiError ? err.message : "Could not save the organization profile");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card title={`Organization · ${org.type === "ca_firm" ? "CA firm" : "Company"}`}>
      {saveError && <div className="mb-3"><ErrorBlock message={saveError} /></div>}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Field label="Name">
          <Input value={value("name", org.name)} onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} />
        </Field>
        <Field label="Legal name">
          <Input value={value("legal_name", org.legal_name)} onChange={(e) => setForm((f) => ({ ...f, legal_name: e.target.value }))} />
        </Field>
        <Field label="State">
          <Input value={value("state", org.state)} onChange={(e) => setForm((f) => ({ ...f, state: e.target.value }))} />
        </Field>
        <Field label="Industry">
          <Input value={value("industry", org.industry)} onChange={(e) => setForm((f) => ({ ...f, industry: e.target.value }))} />
        </Field>
        <Field label="Contact email">
          <Input value={value("contact_email", org.contact_email)} onChange={(e) => setForm((f) => ({ ...f, contact_email: e.target.value }))} />
        </Field>
        <Field label="Contact phone">
          <Input value={value("contact_phone", org.contact_phone)} onChange={(e) => setForm((f) => ({ ...f, contact_phone: e.target.value }))} />
        </Field>
        <Field label="GSTIN">
          <Input value={value("gstin", org.gstin)} onChange={(e) => setForm((f) => ({ ...f, gstin: e.target.value }))} />
        </Field>
        <Field label="PAN">
          <Input value={value("pan", org.pan)} onChange={(e) => setForm((f) => ({ ...f, pan: e.target.value }))} />
        </Field>
        <Field label="CIN">
          <Input value={value("cin", org.cin)} onChange={(e) => setForm((f) => ({ ...f, cin: e.target.value }))} />
        </Field>
        <Field label="TAN">
          <Input value={value("tan", org.tan)} onChange={(e) => setForm((f) => ({ ...f, tan: e.target.value }))} />
        </Field>
        <Field label="Address">
          <Input value={value("address", org.address)} onChange={(e) => setForm((f) => ({ ...f, address: e.target.value }))} />
        </Field>
      </div>
      <div className="mt-4 flex items-center gap-3">
        <Button onClick={save} disabled={saving}>
          {saving ? "Saving…" : "Save changes"}
        </Button>
        {saved && <span className="text-sm text-emerald-600 dark:text-emerald-400">Saved.</span>}
      </div>
      {session?.is_delegated && (
        <p className="mt-3 text-xs text-slate-400">Editing {session.organization_name} on behalf of {session.home_organization_name}.</p>
      )}
    </Card>
  );
}

function SecurityCard() {
  const { user, refreshSession } = useAuth();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [pwError, setPwError] = useState<string | null>(null);
  const [pwSaving, setPwSaving] = useState(false);
  const [pwSaved, setPwSaved] = useState(false);

  const [setup, setSetup] = useState<{ secret: string; provisioning_uri: string } | null>(null);
  const [confirmCode, setConfirmCode] = useState("");
  const [disableCode, setDisableCode] = useState("");
  const [totpError, setTotpError] = useState<string | null>(null);
  const [totpBusy, setTotpBusy] = useState(false);

  async function changePassword(e: React.FormEvent) {
    e.preventDefault();
    setPwSaving(true);
    setPwError(null);
    setPwSaved(false);
    try {
      await authApi.changePassword(currentPassword, newPassword);
      setCurrentPassword("");
      setNewPassword("");
      setPwSaved(true);
    } catch (err) {
      setPwError(err instanceof ApiError ? err.message : "Could not change your password");
    } finally {
      setPwSaving(false);
    }
  }

  async function beginSetup() {
    setTotpBusy(true);
    setTotpError(null);
    try {
      const result = await authApi.totpSetup();
      setSetup(result);
    } catch (err) {
      setTotpError(err instanceof ApiError ? err.message : "Could not begin enrolment");
    } finally {
      setTotpBusy(false);
    }
  }

  async function confirmSetup() {
    setTotpBusy(true);
    setTotpError(null);
    try {
      await authApi.totpConfirm(confirmCode);
      setSetup(null);
      setConfirmCode("");
      await refreshSession();
    } catch (err) {
      setTotpError(err instanceof ApiError ? err.message : "Incorrect code");
    } finally {
      setTotpBusy(false);
    }
  }

  async function disable() {
    setTotpBusy(true);
    setTotpError(null);
    try {
      await authApi.totpDisable(disableCode);
      setDisableCode("");
      await refreshSession();
    } catch (err) {
      setTotpError(err instanceof ApiError ? err.message : "Incorrect code");
    } finally {
      setTotpBusy(false);
    }
  }

  return (
    <Card title="Security">
      <div className="grid grid-cols-1 gap-8 lg:grid-cols-2">
        <form onSubmit={changePassword} className="space-y-3">
          <h3 className="text-sm font-semibold text-slate-700 dark:text-slate-200">Change password</h3>
          {pwError && <ErrorBlock message={pwError} />}
          <Field label="Current password">
            <Input type="password" required value={currentPassword} onChange={(e) => setCurrentPassword(e.target.value)} />
          </Field>
          <Field label="New password">
            <Input type="password" required minLength={8} value={newPassword} onChange={(e) => setNewPassword(e.target.value)} />
          </Field>
          <Button type="submit" disabled={pwSaving}>
            {pwSaving ? "Saving…" : "Update password"}
          </Button>
          {pwSaved && <span className="ml-3 text-sm text-emerald-600 dark:text-emerald-400">Updated.</span>}
        </form>

        <div className="space-y-3">
          <h3 className="text-sm font-semibold text-slate-700 dark:text-slate-200">Two-factor authentication</h3>
          {totpError && <ErrorBlock message={totpError} />}
          {user?.has_totp ? (
            <div className="space-y-2">
              <p className="text-sm text-slate-600 dark:text-slate-300">Two-factor authentication is enabled.</p>
              <Field label="Enter a code to disable it">
                <Input value={disableCode} onChange={(e) => setDisableCode(e.target.value)} maxLength={10} />
              </Field>
              <Button variant="danger" disabled={totpBusy || !disableCode} onClick={disable}>
                Disable
              </Button>
            </div>
          ) : setup ? (
            <div className="space-y-2">
              <p className="text-sm text-slate-600 dark:text-slate-300">
                Scan this into your authenticator app, or enter the secret manually:
              </p>
              <code className="block break-all rounded-lg bg-slate-100 px-3 py-2 text-xs dark:bg-slate-800">{setup.secret}</code>
              <Field label="Enter the 6-digit code to confirm">
                <Input value={confirmCode} onChange={(e) => setConfirmCode(e.target.value)} maxLength={10} />
              </Field>
              <Button disabled={totpBusy || !confirmCode} onClick={confirmSetup}>
                Confirm
              </Button>
            </div>
          ) : (
            <div className="space-y-2">
              <p className="text-sm text-slate-600 dark:text-slate-300">Not enabled.</p>
              <Button variant="secondary" disabled={totpBusy} onClick={beginSetup}>
                Set up two-factor authentication
              </Button>
            </div>
          )}
        </div>
      </div>
    </Card>
  );
}

function UsersCard() {
  const { user: currentUser } = useAuth();
  const { data: users, loading, error, reload } = useApi(() => authApi.listUsers(), []);
  const [showForm, setShowForm] = useState(false);

  return (
    <Card
      title="Users"
      action={<Button variant="secondary" onClick={() => setShowForm((v) => !v)}>{showForm ? "Cancel" : "Add user"}</Button>}
    >
      {showForm && (
        <div className="mb-4">
          <NewUserForm
            onCreated={() => {
              setShowForm(false);
              reload();
            }}
          />
        </div>
      )}
      {loading && <LoadingBlock label="Loading users…" />}
      {error && <ErrorBlock message={error} />}
      {users && (
        <ul className="divide-y divide-slate-100 dark:divide-slate-800">
          {users.map((u) => (
            <li key={u.id} className="flex flex-wrap items-center justify-between gap-2 py-3">
              <div>
                <p className="font-medium text-slate-800 dark:text-slate-100">
                  {u.full_name} {u.id === currentUser?.id && <span className="text-xs text-slate-400">(you)</span>}
                </p>
                <p className="text-xs text-slate-500 dark:text-slate-400">{u.email}</p>
              </div>
              <div className="flex items-center gap-2">
                {!u.is_active && <Badge value="inactive" label="Inactive" />}
                <Badge value={u.role} label={ROLE_LABEL[u.role as UserRole]} />
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function NewUserForm({ onCreated }: { onCreated: () => void }) {
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<UserRole>("staff");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await authApi.createUser({ full_name: fullName, email, password, role });
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not add this user");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-3 rounded-lg border border-slate-200 p-4 dark:border-slate-800">
      {error && <ErrorBlock message={error} />}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label="Full name">
          <Input required minLength={2} value={fullName} onChange={(e) => setFullName(e.target.value)} />
        </Field>
        <Field label="Email">
          <Input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </Field>
        <Field label="Temporary password">
          <Input type="password" required minLength={8} value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        <Field label="Role">
          <Select value={role} onChange={(e) => setRole(e.target.value as UserRole)}>
            <option value="admin">Admin</option>
            <option value="compliance_manager">Compliance Manager</option>
            <option value="staff">Staff</option>
            <option value="read_only">Read only</option>
          </Select>
        </Field>
      </div>
      <Button type="submit" disabled={submitting}>
        {submitting ? "Adding…" : "Add user"}
      </Button>
    </form>
  );
}
