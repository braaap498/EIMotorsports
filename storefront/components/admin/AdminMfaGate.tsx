"use client";

// Second-factor screen for the admin (Supabase TOTP: Google Authenticator, Authy, 1Password...).
// Shown only when the server answers "MFA required" (REQUIRE_MFA=true on the API).
//  - No authenticator set up yet: shows a QR code and the setup key, then asks for the 6-digit code.
//  - Already set up: asks for the 6-digit code.
// When the code is accepted the session is upgraded to the second-factor level and onDone() re-checks access.

import { useEffect, useState } from "react";
import { getSupabase } from "../../lib/supabase";
import { Notice, btnCls, btn2Cls, inputCls, Field } from "./ui";

export default function AdminMfaGate({ onDone, onSignOut }: { onDone: () => void; onSignOut: () => void }) {
  const [factorId, setFactorId] = useState("");
  const [qr, setQr] = useState("");
  const [secret, setSecret] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const mfa = getSupabase().auth.mfa;
        const { data, error } = await mfa.listFactors();
        if (error) throw error;
        const verified = data?.totp?.[0]; // totp = verified authenticator apps only
        if (verified) {
          if (alive) setFactorId(verified.id);
        } else {
          // Remove half-finished setups so enrolling again never fails with "name already exists".
          for (const f of data?.all ?? []) {
            if (f.factor_type === "totp" && f.status !== "verified") await mfa.unenroll({ factorId: f.id });
          }
          const enr = await mfa.enroll({ factorType: "totp", friendlyName: "Authenticator" });
          if (enr.error) throw enr.error;
          if (alive) {
            setFactorId(enr.data.id);
            setQr(enr.data.totp.qr_code);
            setSecret(enr.data.totp.secret);
          }
        }
      } catch (e) {
        if (alive) setErr(e instanceof Error ? e.message : "Could not start two-step sign-in.");
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  async function submit() {
    setBusy(true);
    setErr("");
    try {
      const { error } = await getSupabase().auth.mfa.challengeAndVerify({ factorId, code: code.replace(/\s/g, "") });
      if (error) throw error;
      onDone();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "That code was not accepted.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto mt-16 w-full max-w-md space-y-4 p-4">
      <h1 className="text-xl font-semibold">Two-step sign-in</h1>
      {loading ? (
        <p className="text-sm text-gray-500">Loading…</p>
      ) : (
        <>
          {qr ? (
            <div className="space-y-2 text-sm">
              <p>Scan this with an authenticator app (Google Authenticator, Authy, 1Password), then enter the 6-digit code it shows.</p>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={qr} alt="Authenticator QR code" className="h-44 w-44 rounded border border-gray-200 bg-white p-1" />
              <p className="break-all text-xs text-gray-500">Can&apos;t scan? Enter this key instead: {secret}</p>
            </div>
          ) : (
            <p className="text-sm">Enter the 6-digit code from your authenticator app.</p>
          )}
          <Field label="6-digit code">
            <input
              className={`${inputCls} w-full`}
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={7}
              value={code}
              autoFocus
              onChange={(e) => setCode(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && code.trim().length >= 6 && !busy && factorId) void submit();
              }}
            />
          </Field>
        </>
      )}
      {err && <Notice kind="error">{err}</Notice>}
      <div className="flex gap-2">
        <button type="button" className={btnCls} disabled={busy || loading || !factorId || code.trim().length < 6} onClick={() => void submit()}>
          {busy ? "Checking…" : qr ? "Turn on and continue" : "Continue"}
        </button>
        <button type="button" className={btn2Cls} onClick={onSignOut}>
          Sign out
        </button>
      </div>
    </div>
  );
}
