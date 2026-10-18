"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Badge, Field, Notice, btnCls, btn2Cls, inputCls } from "../../../components/admin/ui";
import PasswordInput from "../../../components/admin/PasswordInput";
import { getSupabase } from "../../../lib/supabase";

type Staff = {
  id: string;
  email: string | null;
  display_name: string;
  role: string;
  last_sign_in_at: string | null;
  created_at: string | null;
};
type Created = { email: string; display_name: string; password: string };

const ROLE_LABEL: Record<string, string> = { owner: "Owner", admin: "Admin", staff: "Staff" };

// No look-alike characters (0/O, 1/l/I) so a password read out loud is not misheard.
const PW_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789";
function generatePassword(length = 16): string {
  const bytes = new Uint32Array(length);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => PW_CHARS[b % PW_CHARS.length]).join("");
}

function Row({ s, meId, onSaved }: { s: Staff; meId: string; onSaved: () => void }) {
  const [name, setName] = useState(s.display_name);
  const [role, setRole] = useState(s.role);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [pwOpen, setPwOpen] = useState(false);
  const [pw, setPw] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [pwBusy, setPwBusy] = useState(false);
  const [pwError, setPwError] = useState("");
  const [pwDone, setPwDone] = useState("");

  const isMe = s.id === meId;
  const roleLocked = isMe || s.role === "owner";
  const changed = name.trim() !== s.display_name || role !== s.role;

  async function save() {
    setBusy(true);
    setError("");
    try {
      const body: Record<string, string> = {};
      if (name.trim() !== s.display_name) body.display_name = name.trim();
      if (role !== s.role) body.role = role;
      await api(`/users/${s.id}`, { method: "PATCH", body });
      onSaved();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  async function setPassword() {
    setPwBusy(true);
    setPwError("");
    setPwDone("");
    try {
      await api(`/users/${s.id}/password`, { method: "POST", body: { password: pw } });
      setPwDone(pw);
      setPw("");
      setShowPw(false);
    } catch (e) {
      setPwError(errMsg(e));
    } finally {
      setPwBusy(false);
    }
  }

  return (
    <>
    <tr className="align-top">
      <td className="px-3 py-2">
        <input
          className={inputCls}
          value={name}
          maxLength={40}
          placeholder="Display name (required)"
          onChange={(e) => setName(e.target.value)}
        />
        {!s.display_name && <p className="mt-1 text-xs text-amber-700">No display name yet. The site greets them by their email.</p>}
        {error && <p className="mt-1 text-xs text-red-700">{error}</p>}
      </td>
      <td className="px-3 py-2 text-sm text-gray-700">
        {s.email}
        {isMe && <span className="ml-2"><Badge tone="blue">You</Badge></span>}
      </td>
      <td className="px-3 py-2">
        <select className={inputCls} value={role} disabled={roleLocked} onChange={(e) => setRole(e.target.value)}>
          {s.role === "owner" && <option value="owner">Owner</option>}
          <option value="admin">Admin</option>
          <option value="staff">Staff</option>
        </select>
      </td>
      <td className="whitespace-nowrap px-3 py-2 text-xs text-gray-500">
        {s.last_sign_in_at ? new Date(s.last_sign_in_at).toLocaleDateString() : "Never"}
      </td>
      <td className="whitespace-nowrap px-3 py-2 text-right">
        <button className={btnCls} disabled={busy || !changed || !name.trim()} onClick={save}>
          {busy ? "Saving…" : "Save"}
        </button>{" "}
        <button
          className={btn2Cls}
          onClick={() => {
            setPwOpen((o) => !o);
            setPwDone("");
            setPwError("");
          }}
        >
          {pwOpen ? "Close" : "Set password"}
        </button>
      </td>
    </tr>
    {pwOpen && (
      <tr className="bg-gray-50">
        <td colSpan={5} className="px-3 py-3">
          <div className="max-w-xl space-y-2">
            <p className="text-sm font-medium">
              Set a new password for {s.display_name || s.email}
              {isMe ? " (your own account)" : ""}
            </p>
            <div className="flex gap-2">
              <PasswordInput value={pw} onChange={setPw} autoComplete="new-password" show={showPw} onToggle={() => setShowPw((v) => !v)} placeholder="At least 12 characters" />
              <button
                type="button"
                className={btn2Cls}
                onClick={() => {
                  setPw(generatePassword());
                  setShowPw(true);
                }}
              >
                Generate
              </button>
            </div>
            {pwError && <Notice kind="error">{pwError}</Notice>}
            {pwDone && (
              <div className="space-y-1 rounded border border-green-300 bg-green-50 p-2 text-sm text-green-900">
                <p>
                  Password changed. New password: <code className="rounded bg-white px-1">{pwDone}</code>
                </p>
                <button type="button" className="text-xs underline" onClick={() => navigator.clipboard?.writeText(pwDone)}>
                  Copy
                </button>
                <p className="text-xs">Give it to them privately. It is only shown now.</p>
              </div>
            )}
            <button className={btnCls} disabled={pwBusy || pw.length < 12} onClick={setPassword}>
              {pwBusy ? "Setting…" : "Set password"}
            </button>
          </div>
        </td>
      </tr>
    )}
    </>
  );
}

function MyPassword() {
  const [open, setOpen] = useState(false);
  const [pw, setPw] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setDone(false);
    if (pw.length < 12) return setError("Use at least 12 characters.");
    if (pw !== again) return setError("The two passwords don't match.");
    setBusy(true);
    try {
      const { error: err } = await getSupabase().auth.updateUser({ password: pw });
      if (err) throw new Error(err.message);
      setDone(true);
      setPw("");
      setAgain("");
    } catch (e2) {
      setError(errMsg(e2));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="space-y-3 rounded-lg border border-gray-200 bg-white p-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Change my password</h2>
        <button className={btn2Cls} onClick={() => setOpen((o) => !o)}>
          {open ? "Close" : "Change"}
        </button>
      </div>
      {open && (
        <form onSubmit={save} className="max-w-md space-y-3">
          <Field label="New password (at least 12 characters)">
            <PasswordInput value={pw} onChange={setPw} autoComplete="new-password" />
          </Field>
          <Field label="Type it again">
            <PasswordInput value={again} onChange={setAgain} autoComplete="new-password" />
          </Field>
          {error && <Notice kind="error">{error}</Notice>}
          {done && <Notice kind="success">Your password was changed. Use it the next time you sign in.</Notice>}
          <button className={btnCls} disabled={busy || !pw || !again}>
            {busy ? "Saving…" : "Change my password"}
          </button>
        </form>
      )}
    </section>
  );
}

export default function AdminUsersPage() {
  const [staff, setStaff] = useState<Staff[]>([]);
  const [meId, setMeId] = useState("");
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);

  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("admin");
  const [password, setPassword] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState("");
  const [created, setCreated] = useState<Created | null>(null);

  const load = useCallback(() => {
    api<Staff[]>("/users")
      .then((r) => {
        setStaff(r);
        setError("");
      })
      .catch((e) => setError(errMsg(e)))
      .finally(() => setLoaded(true));
  }, []);

  useEffect(() => {
    api<{ user_id: string }>("/me")
      .then((m) => setMeId(m.user_id))
      .catch(() => {});
    load();
  }, [load]);

  const emailOk = /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email.trim());
  const canAdd = displayName.trim().length > 0 && emailOk && password.length >= 12 && !adding;

  async function add(e: React.FormEvent) {
    e.preventDefault();
    if (!canAdd) return;
    setAdding(true);
    setAddError("");
    setCreated(null);
    try {
      await api("/users", {
        method: "POST",
        body: { display_name: displayName.trim(), email: email.trim(), role, password },
      });
      setCreated({ email: email.trim().toLowerCase(), display_name: displayName.trim(), password });
      setDisplayName("");
      setEmail("");
      setPassword("");
      setShowPw(false);
      setRole("admin");
      load();
    } catch (err) {
      setAddError(errMsg(err));
    } finally {
      setAdding(false);
    }
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Admin users</h1>

      <MyPassword />

      <section className="space-y-4 rounded-lg border border-gray-200 bg-white p-4">
        <h2 className="text-lg font-semibold">Add an admin</h2>
        <form onSubmit={add} className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Display name (required) — shown as “Hi, …” on the site">
              <input className={inputCls} value={displayName} maxLength={40} onChange={(e) => setDisplayName(e.target.value)} placeholder="e.g. Eric" required />
            </Field>
            <Field label="Email (they sign in with this)">
              <input className={inputCls} type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="name@example.com" required />
            </Field>
            <Field label="Role">
              <select className={inputCls} value={role} onChange={(e) => setRole(e.target.value)}>
                <option value="admin">Admin — can use every admin screen</option>
                <option value="staff">Staff — cannot open the admin screens yet</option>
              </select>
            </Field>
            <Field label="Temporary password (at least 12 characters)">
              <div className="flex gap-2">
                <PasswordInput value={password} onChange={setPassword} autoComplete="new-password" show={showPw} onToggle={() => setShowPw((v) => !v)} />
                <button
                  type="button"
                  className={btn2Cls}
                  onClick={() => {
                    setPassword(generatePassword());
                    setShowPw(true);
                  }}
                >
                  Generate
                </button>
              </div>
            </Field>
          </div>
          {addError && <Notice kind="error">{addError}</Notice>}
          <div className="flex flex-wrap items-center gap-3">
            <button className={btnCls} disabled={!canAdd}>
              {adding ? "Adding…" : "Add admin"}
            </button>
            {!displayName.trim() && <span className="text-xs text-gray-500">A display name is required.</span>}
          </div>
        </form>

        {created && (
          <div className="space-y-2 rounded border border-green-300 bg-green-50 p-3 text-sm text-green-900">
            <p className="font-semibold">
              {created.display_name} was added. Give them these sign-in details privately:
            </p>
            <p>
              Email: <code className="rounded bg-white px-1">{created.email}</code>
              <br />
              Temporary password: <code className="rounded bg-white px-1">{created.password}</code>
            </p>
            <div className="flex flex-wrap items-center gap-3">
              <button type="button" className={btn2Cls} onClick={() => navigator.clipboard?.writeText(`${created.email}\n${created.password}`)}>
                Copy both
              </button>
              <button type="button" className="text-xs underline" onClick={() => setCreated(null)}>
                Hide
              </button>
            </div>
            <p className="text-xs">
              This password is only shown now. After signing in, they should change it with “Forgot password” on the
              store&apos;s sign-in window.
            </p>
          </div>
        )}
      </section>

      <section className="space-y-3 rounded-lg border border-gray-200 bg-white p-4">
        <h2 className="text-lg font-semibold">Current admins</h2>
        {error && <Notice kind="error">{error}</Notice>}
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="bg-gray-50 text-xs uppercase text-gray-500">
              <tr>
                <th className="px-3 py-2">Display name</th>
                <th className="px-3 py-2">Email</th>
                <th className="px-3 py-2">Role</th>
                <th className="px-3 py-2">Last sign-in</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {staff.map((s) => (
                <Row key={`${s.id}:${s.display_name}:${s.role}`} s={s} meId={meId} onSaved={load} />
              ))}
              {loaded && staff.length === 0 && !error && (
                <tr>
                  <td colSpan={5} className="px-3 py-6 text-center text-gray-500">
                    No staff accounts found.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <p className="text-xs text-gray-500">
          A changed display name or role appears on the person&apos;s next sign-in (or within about an hour). You can&apos;t
          change your own role, and the Owner role can only be given directly in the database.
        </p>
      </section>
    </div>
  );
}
