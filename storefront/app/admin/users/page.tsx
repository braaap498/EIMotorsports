"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, apiDownload, errMsg } from "../../../lib/adminApi";
import { Badge, Field, Notice, btnCls, btn2Cls, dangerCls, inputCls } from "../../../components/admin/ui";
import PasswordInput from "../../../components/admin/PasswordInput";

type Staff = {
  id: string;
  email: string | null;
  display_name: string;
  last_name: string;
  role: string;
  permissions: string[];
  last_sign_in_at: string | null;
  created_at: string | null;
};
type PermGroup = { key: string; label: string; perms: { key: string; label: string }[] };
type PermCatalog = { groups: PermGroup[]; implies: Record<string, string[]> };
type Created = { email: string; display_name: string; password: string };
type Customer = {
  id: string;
  email: string | null;
  name: string;
  phone: string;
  confirmed: boolean;
  blocked: boolean;
  created_at: string | null;
  last_sign_in_at: string | null;
  address: string | null;
};
type CustomerList = { customers: Customer[]; matched: number; total: number; addresses_error?: string | null };

const ROLE_LABEL: Record<string, string> = { owner: "Owner", admin: "Admin", staff: "Employee" };

// No look-alike characters (0/O, 1/l/I) so a password read out loud is not misheard.
const PW_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789";
function generatePassword(length = 16): string {
  const bytes = new Uint32Array(length);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => PW_CHARS[b % PW_CHARS.length]).join("");
}

// Ticking a permission also ticks the ones it needs (you can't edit what you can't open);
// unticking one also unticks anything that needs it.
function toggled(current: string[], key: string, on: boolean, implies: Record<string, string[]>): string[] {
  const set = new Set(current);
  if (on) {
    set.add(key);
    (implies[key] ?? []).forEach((k) => set.add(k));
  } else {
    set.delete(key);
    Object.entries(implies).forEach(([k, needs]) => {
      if (needs.includes(key)) set.delete(k);
    });
  }
  return Array.from(set);
}

function PermissionGrid({
  catalog,
  value,
  onChange,
}: {
  catalog: PermCatalog | null;
  value: string[];
  onChange: (v: string[]) => void;
}) {
  if (!catalog) return <p className="text-xs text-gray-500">Loading permissions…</p>;
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {catalog.groups.map((g) => {
        const keys = g.perms.map((p) => p.key);
        const allOn = keys.every((k) => value.includes(k));
        return (
          <fieldset key={g.key} className="space-y-1 rounded border border-gray-200 bg-white p-3">
            <legend className="px-1 text-sm font-semibold">{g.label}</legend>
            <label className="flex items-center gap-2 text-sm font-medium">
              <input
                type="checkbox"
                checked={allOn}
                onChange={(e) => {
                  let next = value;
                  keys.forEach((k) => (next = toggled(next, k, e.target.checked, catalog.implies)));
                  onChange(next);
                }}
              />
              Allow all
            </label>
            {g.perms.map((p) => (
              <label key={p.key} className="flex items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  className="mt-1"
                  checked={value.includes(p.key)}
                  onChange={(e) => onChange(toggled(value, p.key, e.target.checked, catalog.implies))}
                />
                <span>{p.label}</span>
              </label>
            ))}
          </fieldset>
        );
      })}
    </div>
  );
}

function Row({
  s,
  meId,
  catalog,
  onSaved,
}: {
  s: Staff;
  meId: string;
  catalog: PermCatalog | null;
  onSaved: () => void;
}) {
  const [name, setName] = useState(s.display_name);
  const [lastName, setLastName] = useState(s.last_name ?? "");
  const [role, setRole] = useState(s.role);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [pwOpen, setPwOpen] = useState(false);
  const [pw, setPw] = useState("");
  const [pwAgain, setPwAgain] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [pwBusy, setPwBusy] = useState(false);
  const [pwError, setPwError] = useState("");
  const [pwDone, setPwDone] = useState("");
  const [permOpen, setPermOpen] = useState(false);
  const [perms, setPerms] = useState<string[]>(s.permissions ?? []);
  const [permBusy, setPermBusy] = useState(false);
  const [permError, setPermError] = useState("");
  const [permDone, setPermDone] = useState(false);

  const isMe = s.id === meId;
  const roleLocked = isMe || s.role === "owner";
  const changed = name.trim() !== s.display_name || lastName.trim() !== (s.last_name ?? "") || role !== s.role;

  async function save() {
    setBusy(true);
    setError("");
    try {
      const body: Record<string, string> = {};
      if (name.trim() !== s.display_name) body.display_name = name.trim();
      if (lastName.trim() !== (s.last_name ?? "")) body.last_name = lastName.trim();
      if (role !== s.role) body.role = role;
      await api(`/users/${s.id}`, { method: "PATCH", body });
      onSaved();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  async function savePerms() {
    setPermBusy(true);
    setPermError("");
    setPermDone(false);
    try {
      await api(`/users/${s.id}`, { method: "PATCH", body: { permissions: perms } });
      setPermDone(true);
      onSaved();
    } catch (e) {
      setPermError(errMsg(e));
    } finally {
      setPermBusy(false);
    }
  }

  async function setPassword() {
    setPwError("");
    setPwDone("");
    if (pw.length < 12) return setPwError("Use at least 12 characters.");
    if (pw !== pwAgain) return setPwError("The two passwords don't match.");
    setPwBusy(true);
    try {
      await api(`/users/${s.id}/password`, { method: "POST", body: { password: pw } });
      setPwDone(pw);
      setPw("");
      setPwAgain("");
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
        <input
          className={`${inputCls} mt-1`}
          value={lastName}
          maxLength={40}
          placeholder="Last name (shown on purchase orders)"
          onChange={(e) => setLastName(e.target.value)}
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
          <option value="staff">Employee</option>
        </select>
      </td>
      <td className="whitespace-nowrap px-3 py-2 text-xs text-gray-500">
        {s.last_sign_in_at ? new Date(s.last_sign_in_at).toLocaleDateString() : "Never"}
      </td>
      <td className="whitespace-nowrap px-3 py-2 text-right">
        <button className={btnCls} disabled={busy || !changed || !name.trim()} onClick={save}>
          {busy ? "Saving…" : "Save"}
        </button>{" "}
        {s.role === "staff" && (
          <>
            <button
              className={btn2Cls}
              onClick={() => {
                setPermOpen((o) => !o);
                setPerms(s.permissions ?? []);
                setPermDone(false);
                setPermError("");
              }}
            >
              {permOpen ? "Close" : `Permissions (${(s.permissions ?? []).length})`}
            </button>{" "}
          </>
        )}
        <button
          className={btn2Cls}
          onClick={() => {
            setPwOpen((o) => !o);
            setPw("");
            setPwAgain("");
            setPwDone("");
            setPwError("");
          }}
        >
          {pwOpen ? "Close" : "Change Password"}
        </button>
      </td>
    </tr>
    {permOpen && (
      <tr className="bg-gray-50">
        <td colSpan={5} className="px-3 py-3">
          <div className="space-y-3">
            <p className="text-sm font-medium">What {s.display_name || s.email} is allowed to do</p>
            <PermissionGrid catalog={catalog} value={perms} onChange={setPerms} />
            {permError && <Notice kind="error">{permError}</Notice>}
            {permDone && (
              <Notice kind="success">Saved. It takes effect on their next sign-in (or within about an hour).</Notice>
            )}
            <button className={btnCls} disabled={permBusy || !catalog} onClick={savePerms}>
              {permBusy ? "Saving…" : "Save permissions"}
            </button>
          </div>
        </td>
      </tr>
    )}
    {pwOpen && (
      <tr className="bg-gray-50">
        <td colSpan={5} className="px-3 py-3">
          <div className="max-w-xl space-y-2">
            <p className="text-sm font-medium">
              Set a new password for {s.display_name || s.email}
              {isMe ? " (your own account)" : ""}
            </p>
            <Field label="New password (at least 12 characters)">
              <div className="flex gap-2">
                <PasswordInput value={pw} onChange={setPw} autoComplete="new-password" show={showPw} onToggle={() => setShowPw((v) => !v)} />
                <button
                  type="button"
                  className={btn2Cls}
                  onClick={() => {
                    const g = generatePassword();
                    setPw(g);
                    setPwAgain(g);
                    setShowPw(true);
                  }}
                >
                  Generate
                </button>
              </div>
            </Field>
            <Field label="Type it again">
              <PasswordInput value={pwAgain} onChange={setPwAgain} autoComplete="new-password" show={showPw} onToggle={() => setShowPw((v) => !v)} />
            </Field>
            {pwError && <Notice kind="error">{pwError}</Notice>}
            {pwDone && isMe && <Notice kind="success">Your password was changed. Use it the next time you sign in.</Notice>}
            {pwDone && !isMe && (
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
            <button className={btnCls} disabled={pwBusy || !pw || !pwAgain} onClick={setPassword}>
              {pwBusy ? "Setting…" : "Set password"}
            </button>
          </div>
        </td>
      </tr>
    )}
    </>
  );
}

type MenuItem = { key: string; label: string; onClick: () => void; danger?: boolean; hidden?: boolean };

/** A "⋯" button that opens a small list of actions. The list is drawn on top of the page (position: fixed)
 *  so the scrolling table can't clip it, and it flips upward near the bottom of the screen. */
function RowMenu({ items, disabled, label }: { items: MenuItem[]; disabled: boolean; label: string }) {
  const [pos, setPos] = useState<{ top: number; right: number } | null>(null);
  const btnRef = useRef<HTMLButtonElement>(null);
  const shown = items.filter((i) => !i.hidden);

  useEffect(() => {
    if (!pos) return;
    const close = () => setPos(null);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
      window.removeEventListener("keydown", onKey);
    };
  }, [pos]);

  function toggle() {
    if (pos) return setPos(null);
    const r = btnRef.current?.getBoundingClientRect();
    if (!r) return;
    const menuHeight = shown.length * 38 + 12;
    const top = r.bottom + menuHeight > window.innerHeight ? Math.max(8, r.top - menuHeight) : r.bottom + 4;
    setPos({ top, right: Math.max(8, window.innerWidth - r.right) });
  }

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        className={`${btn2Cls} px-3 text-lg leading-none`}
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={!!pos}
        aria-label={`Actions for ${label}`}
        onClick={toggle}
      >
        ⋯
      </button>
      {pos && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setPos(null)} />
          <div
            role="menu"
            className="fixed z-50 min-w-[11rem] rounded border border-gray-200 bg-white py-1 shadow-lg"
            style={{ top: pos.top, right: pos.right }}
          >
            {shown.map((i) => (
              <button
                key={i.key}
                type="button"
                role="menuitem"
                className={`block w-full px-3 py-2 text-left text-sm hover:bg-gray-50 ${i.danger ? "text-red-700" : "text-gray-800"}`}
                onClick={() => {
                  setPos(null);
                  i.onClick();
                }}
              >
                {i.label}
              </button>
            ))}
          </div>
        </>
      )}
    </>
  );
}

function CustomerRow({ c, onChanged }: { c: Customer; onChanged: (message: string) => void }) {
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [delOpen, setDelOpen] = useState(false);
  const [typed, setTyped] = useState("");

  const label = c.name || c.email || "this customer";
  const emailMatches = !!c.email && typed.trim().toLowerCase() === c.email.toLowerCase();

  async function run(key: string, path: string, body: Record<string, unknown>, done: string) {
    setBusy(key);
    setError("");
    try {
      await api(`/users/customers/${c.id}/${path}`, { method: "POST", body });
      onChanged(done);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy("");
    }
  }

  const items: MenuItem[] = [
    c.blocked
      ? { key: "unblock", label: "Unblock", onClick: () => run("unblock", "unblock", {}, `${label} was unblocked.`) }
      : { key: "block", label: "Block", onClick: () => run("block", "block", {}, `${label} was blocked. They can't sign in.`) },
    {
      key: "resend",
      label: "Resend confirmation",
      hidden: c.confirmed,
      onClick: () => run("resend", "resend-confirmation", {}, `A new confirmation email was sent to ${c.email}.`),
    },
    {
      key: "reset",
      label: "Send password reset",
      onClick: () => run("reset", "reset-password", {}, `A password reset email was sent to ${c.email}.`),
    },
    {
      key: "delete",
      label: "Delete…",
      danger: true,
      onClick: () => {
        setDelOpen(true);
        setTyped("");
        setError("");
      },
    },
  ];

  return (
    <>
      <tr className="align-top">
        <td className="px-3 py-2">
          <div className="font-medium text-gray-900">{c.name || <span className="text-gray-400">No name</span>}</div>
          <div className="text-xs text-gray-600">{c.email}</div>
        </td>
        <td className="whitespace-nowrap px-3 py-2 text-sm text-gray-700">{c.phone || <span className="text-gray-400">—</span>}</td>
        <td className="px-3 py-2">
          {c.blocked ? (
            <span className="inline-block rounded bg-red-100 px-2 py-0.5 text-xs font-medium text-red-800">Blocked</span>
          ) : c.confirmed ? (
            <Badge tone="green">Active</Badge>
          ) : (
            <Badge tone="amber">Waiting for verification</Badge>
          )}
        </td>
        <td className="whitespace-nowrap px-3 py-2 text-xs text-gray-500">
          {c.created_at ? new Date(c.created_at).toLocaleDateString() : "—"}
        </td>
        <td className="whitespace-nowrap px-3 py-2 text-xs text-gray-500">
          {c.last_sign_in_at ? new Date(c.last_sign_in_at).toLocaleDateString() : "Never"}
        </td>
        <td className="px-3 py-2 text-xs text-gray-700">{c.address || <span className="text-gray-400">None saved</span>}</td>
        <td className="whitespace-nowrap px-3 py-2 text-right">
          {busy && <span className="mr-2 text-xs text-gray-500">Working…</span>}
          <RowMenu items={items} disabled={!!busy} label={label} />
          {error && <p className="mt-1 text-right text-xs text-red-700">{error}</p>}
        </td>
      </tr>
      {delOpen && (
        <tr className="bg-red-50">
          <td colSpan={7} className="px-3 py-3">
            <div className="max-w-xl space-y-2 text-sm">
              <p className="font-medium text-red-900">Permanently delete {label}?</p>
              <p className="text-red-900">
                This removes the account and can&apos;t be undone. If you only want to stop them signing in, use Block
                instead; that keeps their records. To confirm, type their email:
                <span className="ml-1 font-mono">{c.email}</span>
              </p>
              <input
                className={inputCls}
                value={typed}
                onChange={(e) => setTyped(e.target.value)}
                placeholder="Type the email to confirm"
                autoComplete="off"
              />
              <div className="flex gap-2">
                <button
                  className={dangerCls}
                  disabled={!emailMatches || !!busy}
                  onClick={() => run("delete", "delete", { confirm_email: typed.trim() }, `${label} was deleted.`)}
                >
                  {busy === "delete" ? "Deleting…" : "Delete permanently"}
                </button>
                <button className={btn2Cls} onClick={() => setDelOpen(false)}>
                  Cancel
                </button>
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

/** Downloads the whole customer list (not just what's on screen) as Excel or PDF. */
function ExportMenu({ onError }: { onError: (message: string) => void }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState("");

  async function run(format: "xlsx" | "pdf") {
    setOpen(false);
    setBusy(format);
    onError("");
    try {
      const blob = await apiDownload(`/users/customers/export?format=${format}`);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `customers-${new Date().toISOString().slice(0, 10)}.${format}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      onError(errMsg(e));
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="relative">
      <button
        type="button"
        className={btn2Cls}
        disabled={!!busy}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        {busy ? "Exporting…" : "Export ▾"}
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div role="menu" className="absolute right-0 z-50 mt-1 w-48 rounded border border-gray-200 bg-white py-1 shadow-lg">
            <button
              type="button"
              role="menuitem"
              className="block w-full px-3 py-2 text-left text-sm text-gray-800 hover:bg-gray-50"
              onClick={() => run("xlsx")}
            >
              Excel (.xlsx)
            </button>
            <button
              type="button"
              role="menuitem"
              className="block w-full px-3 py-2 text-left text-sm text-gray-800 hover:bg-gray-50"
              onClick={() => run("pdf")}
            >
              PDF
            </button>
          </div>
        </>
      )}
    </div>
  );
}

function CustomersPanel() {
  const [q, setQ] = useState("");
  const [data, setData] = useState<CustomerList | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState("");
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    // Wait a moment after the last keystroke so we don't ask the server on every letter.
    const t = setTimeout(
      () => {
        api<CustomerList>(`/users/customers?q=${encodeURIComponent(q.trim())}`)
          .then((r) => {
            if (cancelled) return;
            setData(r);
            setError("");
          })
          .catch((e) => {
            if (!cancelled) setError(errMsg(e));
          })
          .finally(() => {
            if (!cancelled) setLoading(false);
          });
      },
      q ? 300 : 0
    );
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [q, reloadKey]);

  const rows = data?.customers ?? [];

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <input
          className={`${inputCls} max-w-md`}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search customers by name, email, phone or address"
        />
        {data && (
          <span className="text-xs text-gray-500">
            {data.total} customer{data.total === 1 ? "" : "s"} in total
          </span>
        )}
        <div className="ml-auto">
          <ExportMenu onError={setError} />
        </div>
      </div>
      <details className="rounded border border-blue-200 bg-blue-50 p-3 text-sm text-blue-900">
        <summary className="cursor-pointer font-medium">? What do the actions in the ⋯ menu do</summary>
        <ul className="mt-2 list-disc space-y-1.5 pl-5">
          <li>
            Click the <strong>⋯</strong> button at the end of a customer&apos;s row to see their actions.
          </li>
          <li>
            <strong>Block</strong> stops the customer signing in. Their account, saved addresses, garage and order history
            are all kept, and <strong>Unblock</strong> puts everything back. Use it for a hacked, spammed or misused
            account. It does not cancel or refund anything.
          </li>
          <li>
            <strong>Delete</strong> permanently removes the account together with their saved addresses and garage. It can&apos;t be
            undone. Use it for test accounts, duplicates, or someone who asks to be removed. A customer who has placed an
            order can&apos;t be deleted (their order history is kept); block them instead.
          </li>
          <li>
            <strong>Resend confirmation</strong> emails a new confirmation link to someone who never received or lost the
            first one. It only appears for customers who haven&apos;t confirmed yet.
          </li>
          <li>
            <strong>Send password reset</strong> emails the customer a link to choose a new password. You never see or set
            their password.
          </li>
          <li>A block or delete can take up to an hour to take effect for someone who is already signed in.</li>
          <li>
            <strong>Export</strong> downloads the whole customer list, not just the rows on screen or the current search, as
            an Excel or PDF file.
          </li>
        </ul>
      </details>
      {notice && <Notice kind="success">{notice}</Notice>}
      {error && <Notice kind="error">{error}</Notice>}
      {data?.addresses_error && (
        <Notice kind="error">
          Saved addresses couldn&apos;t be loaded, so the Address column is empty and address search is off. Reason:{" "}
          {data.addresses_error}
        </Notice>
      )}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="bg-gray-50 text-xs uppercase text-gray-500">
            <tr>
              <th className="px-3 py-2">Customer</th>
              <th className="px-3 py-2">Phone</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2">Signed up</th>
              <th className="px-3 py-2">Last sign-in</th>
              <th className="px-3 py-2">Address</th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {rows.map((c) => (
              <CustomerRow
                key={`${c.id}:${c.blocked}:${c.confirmed}`}
                c={c}
                onChanged={(m) => {
                  setNotice(m);
                  setReloadKey((k) => k + 1);
                }}
              />
            ))}
            {!loading && !error && rows.length === 0 && (
              <tr>
                <td colSpan={7} className="px-3 py-6 text-center text-gray-500">
                  {q.trim() ? "No customers match that search." : "No customer accounts yet."}
                </td>
              </tr>
            )}
            {loading && rows.length === 0 && (
              <tr>
                <td colSpan={7} className="px-3 py-6 text-center text-gray-500">
                  Loading…
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {data && data.matched > rows.length && (
        <p className="text-xs text-gray-500">
          Showing the newest {rows.length} of {data.matched} matches. Type more of the name or email to narrow it down.
        </p>
      )}
    </div>
  );
}

export default function AdminUsersPage() {
  const [staff, setStaff] = useState<Staff[]>([]);
  const [meId, setMeId] = useState("");
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);

  const [displayName, setDisplayName] = useState("");
  const [newLastName, setNewLastName] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("admin");
  const [password, setPassword] = useState("");
  const [newPerms, setNewPerms] = useState<string[]>([]);
  const [catalog, setCatalog] = useState<PermCatalog | null>(null);
  const [tab, setTab] = useState<"admins" | "employees" | "customers">("admins");
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
    api<PermCatalog>("/users/permissions")
      .then(setCatalog)
      .catch(() => {});
    load();
  }, [load]);

  const adminList = staff.filter((s) => s.role === "owner" || s.role === "admin");
  const employeeList = staff.filter((s) => s.role === "staff");
  const shown = tab === "employees" ? employeeList : adminList;

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
        body: {
          display_name: displayName.trim(),
          last_name: newLastName.trim() || null,
          email: email.trim(),
          role,
          password,
          permissions: role === "staff" ? newPerms : [],
        },
      });
      setCreated({ email: email.trim().toLowerCase(), display_name: displayName.trim(), password });
      setDisplayName("");
      setNewLastName("");
      setEmail("");
      setPassword("");
      setShowPw(false);
      setRole("admin");
      setNewPerms([]);
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

      <section className="space-y-4 rounded-lg border border-gray-200 bg-white p-4">
        <h2 className="text-lg font-semibold">Add a team member</h2>
        <form onSubmit={add} className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Display name (required) — shown as “Hi, …” on the site">
              <input className={inputCls} value={displayName} maxLength={40} onChange={(e) => setDisplayName(e.target.value)} placeholder="e.g. Eric" required />
            </Field>
            <Field label="Last name (optional) — shown with the first name on purchase orders">
              <input className={inputCls} value={newLastName} maxLength={40} onChange={(e) => setNewLastName(e.target.value)} placeholder="e.g. Smith" />
            </Field>
            <Field label="Email (they sign in with this)">
              <input className={inputCls} type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="name@example.com" required />
            </Field>
            <Field label="Role">
              <select className={inputCls} value={role} onChange={(e) => setRole(e.target.value)}>
                <option value="admin">Admin — can use every admin screen</option>
                <option value="staff">Employee — only what you tick below</option>
              </select>
            </Field>
            <Field label="Set Password (at least 12 characters)">
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
          {role === "staff" && (
            <div className="space-y-2">
              <p className="text-sm font-medium">What this employee is allowed to do</p>
              <PermissionGrid catalog={catalog} value={newPerms} onChange={setNewPerms} />
              <p className="text-xs text-gray-500">
                Models, years, types, makes, parts clean-up and this Team page are for admins only.
              </p>
            </div>
          )}
          {addError && <Notice kind="error">{addError}</Notice>}
          <div className="flex flex-wrap items-center gap-3">
            <button className={btnCls} disabled={!canAdd}>
              {adding ? "Adding…" : role === "staff" ? "Add employee" : "Add admin"}
            </button>
            {!displayName.trim() && <span className="text-xs text-gray-500">A display name is required.</span>}
          </div>
        </form>

        {created && (
          <div className="space-y-2 rounded border border-green-300 bg-green-50 p-3 text-sm text-green-900">
            <div className="flex items-start justify-between gap-3">
              <p className="font-semibold">
                {created.display_name} was added. Give them these sign-in details privately:
              </p>
              <button
                type="button"
                className="-mt-1 rounded px-2 text-xl leading-none text-green-900 hover:bg-green-100"
                onClick={() => setCreated(null)}
                aria-label="Close"
              >
                ×
              </button>
            </div>
            <p>
              Email: <code className="rounded bg-white px-1">{created.email}</code>
              <br />
              Password: <code className="rounded bg-white px-1">{created.password}</code>
            </p>
            <div className="flex flex-wrap items-center gap-3">
              <button type="button" className={btn2Cls} onClick={() => navigator.clipboard?.writeText(`${created.email}\n${created.password}`)}>
                Copy both
              </button>
              <button type="button" className={btn2Cls} onClick={() => setCreated(null)}>
                Close
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
        <h2 className="text-lg font-semibold">Team</h2>
        <div className="flex gap-1 border-b border-gray-200" role="tablist">
          {(
            [
              ["admins", "Admins", adminList.length],
              ["employees", "Employees", employeeList.length],
              ["customers", "Customers", null],
            ] as const
          ).map(([key, label, count]) => (
            <button
              key={key}
              type="button"
              role="tab"
              aria-selected={tab === key}
              onClick={() => setTab(key)}
              className={`-mb-px rounded-t border px-4 py-2 text-sm font-medium ${
                tab === key
                  ? "border-gray-200 border-b-white bg-white text-gray-900"
                  : "border-transparent text-gray-500 hover:text-gray-800"
              }`}
            >
              {label}{count === null ? "" : ` (${count})`}
            </button>
          ))}
        </div>
        {tab === "customers" && <CustomersPanel />}
        {tab !== "customers" && error && <Notice kind="error">{error}</Notice>}
        {tab !== "customers" && (
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
              {shown.map((s) => (
                <Row
                  key={`${s.id}:${s.display_name}:${s.last_name}:${s.role}:${(s.permissions ?? []).join(",")}`}
                  s={s}
                  meId={meId}
                  catalog={catalog}
                  onSaved={load}
                />
              ))}
              {loaded && shown.length === 0 && !error && (
                <tr>
                  <td colSpan={5} className="px-3 py-6 text-center text-gray-500">
                    {tab === "admins" ? "No admin accounts found." : "No employees yet. Add one above."}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        )}
        {tab !== "customers" && (
        <p className="text-xs text-gray-500">
          A changed display name, role or permission appears on the person&apos;s next sign-in (or within about an hour). You can&apos;t
          change your own role, and the Owner role can only be given directly in the database.
        </p>
        )}
      </section>
    </div>
  );
}
