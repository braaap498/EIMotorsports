"use client";

// Warning bar at the top of every admin page when the password expires within WARN_DAYS.
// "Change password" opens a small window to set a new one without leaving the page.
// daysLeft comes from the API (/admin/me -> password_days_left); null means no limit or no date, so nothing shows.

import { useState } from "react";
import { getSupabase } from "../../lib/supabase";
import { Notice, btnCls, btn2Cls, Field } from "./ui";
import PasswordInput from "./PasswordInput";
import { policyProblem } from "./ExpiredPasswordGate";

const WARN_DAYS = 7;

function when(days: number): string {
  if (days <= 0) return "today";
  if (days === 1) return "in 1 day";
  return `in ${days} days`;
}

export default function PasswordExpiryBanner({ daysLeft, onChanged }: { daysLeft: number | null; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  if (daysLeft === null || daysLeft > WARN_DAYS) return null;

  function close() {
    setOpen(false);
    setPw("");
    setPw2("");
    setError("");
  }

  async function change(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    const bad = policyProblem(pw);
    if (bad) return setError(bad);
    if (pw !== pw2) return setError("The two passwords don’t match.");
    setBusy(true);
    const sb = getSupabase();
    const { error: upErr } = await sb.auth.updateUser({ password: pw });
    if (upErr) {
      setError(upErr.message);
      setBusy(false);
      return;
    }
    await sb.auth.refreshSession(); // new token carries the new password date; the banner then disappears
    setBusy(false);
    close();
    onChanged();
  }

  return (
    <>
      <div
        role="alert"
        className="mb-4 flex flex-wrap items-center justify-between gap-2 rounded border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900"
      >
        <span>
          <strong>Your password expires {when(daysLeft)}.</strong> After that you won’t be able to use the admin until you set a new one.
        </span>
        <button type="button" className={btn2Cls} onClick={() => setOpen(true)}>
          Change password
        </button>
      </div>

      {open && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
          <form onSubmit={change} className="w-full max-w-md space-y-3 rounded bg-white p-5 shadow-lg">
            <h2 className="text-base font-semibold">Change your password</h2>
            <Field label="New password">
              <PasswordInput autoComplete="new-password" value={pw} onChange={setPw} required />
            </Field>
            <Field label="Confirm new password">
              <PasswordInput autoComplete="new-password" value={pw2} onChange={setPw2} required />
            </Field>
            <p className="text-xs text-gray-500">At least 12 characters, with a letter, a number and a special character.</p>
            {error && <Notice kind="error">{error}</Notice>}
            <div className="flex gap-2">
              <button type="submit" className={btnCls} disabled={busy}>
                {busy ? "Saving…" : "Change password"}
              </button>
              <button type="button" className={btn2Cls} onClick={close} disabled={busy}>
                Cancel
              </button>
            </div>
          </form>
        </div>
      )}
    </>
  );
}
