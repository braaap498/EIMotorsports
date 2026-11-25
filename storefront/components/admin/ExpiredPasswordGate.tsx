"use client";

// Shown when the API answers "password has expired" (PASSWORD_MAX_AGE_DAYS on the API).
// The person is signed in, so they can choose a new password right here (no admin needed).
// Fallback: email themselves a reset link.
// After the change the session is refreshed so the token carries the new password_changed_at, then onDone() re-checks access.

import { useEffect, useState } from "react";
import { getSupabase } from "../../lib/supabase";
import { Notice, btnCls, btn2Cls, Field } from "./ui";
import PasswordInput from "./PasswordInput";

// Same rule the API applies to passwords set by an admin (admin_users.check_password_policy).
export function policyProblem(v: string): string {
  if (v.length < 12) return "Use at least 12 characters.";
  if (!/[^A-Za-z0-9]/.test(v)) return "Include at least one special character, such as ! @ # $ % & *.";
  if (!/[A-Za-z]/.test(v) || !/[0-9]/.test(v)) return "Include at least one letter and one number.";
  return "";
}

export default function ExpiredPasswordGate({ onDone, onSignOut }: { onDone: () => void; onSignOut: () => void }) {
  const [email, setEmail] = useState("");
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");

  useEffect(() => {
    getSupabase()
      .auth.getSession()
      .then(({ data }) => setEmail(data.session?.user?.email ?? ""));
  }, []);

  async function change(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setInfo("");
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
    // Get a fresh token so it carries the new password date, then re-check access.
    const { error: refErr } = await sb.auth.refreshSession();
    setBusy(false);
    if (refErr) {
      setInfo("Password changed. Sign out and sign in again with the new password.");
      return;
    }
    onDone();
  }

  async function emailLink() {
    setError("");
    setInfo("");
    if (!email) return setError("Could not find your email. Sign out, then use “Forgot password?” on the sign-in page.");
    const { error: e } = await getSupabase().auth.resetPasswordForEmail(email, { redirectTo: window.location.origin });
    if (e) setError(e.message);
    else setInfo(`A reset link was sent to ${email}. Open it on this device.`);
  }

  return (
    <div className="mx-auto mt-24 w-full max-w-md space-y-4 p-4">
      <Notice kind="error">Your password has expired. Choose a new one to continue.</Notice>
      <form onSubmit={change} className="space-y-3">
        <Field label="New password">
          <PasswordInput autoComplete="new-password" value={pw} onChange={setPw} required />
        </Field>
        <Field label="Confirm new password">
          <PasswordInput autoComplete="new-password" value={pw2} onChange={setPw2} required />
        </Field>
        <p className="text-xs text-gray-500">At least 12 characters, with a letter, a number and a special character.</p>
        {error && <Notice kind="error">{error}</Notice>}
        {info && <Notice kind="success">{info}</Notice>}
        <div className="flex flex-wrap gap-2">
          <button type="submit" className={btnCls} disabled={busy}>
            {busy ? "Saving…" : "Change password"}
          </button>
          <button type="button" className={btn2Cls} onClick={emailLink}>
            Email me a reset link
          </button>
          <button type="button" className={btn2Cls} onClick={onSignOut}>
            Sign out
          </button>
        </div>
      </form>
    </div>
  );
}
