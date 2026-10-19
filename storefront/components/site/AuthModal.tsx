"use client";

import { useEffect, useState } from "react";
import { getSupabase } from "../../lib/supabase";
import { useAuth, type AuthMode } from "./AuthProvider";
import PasswordField from "./PasswordField";

const input =
  "w-full rounded border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 outline-none focus:border-[#06bde7] focus:ring-2 focus:ring-[#06bde7]/30";
const primary =
  "w-full rounded bg-[#06bde7] px-4 py-2.5 text-sm font-bold text-[#04202b] hover:brightness-110 disabled:opacity-50";

const TITLES: Record<AuthMode, string> = {
  signin: "Sign in",
  signup: "Create your account",
  reset: "Reset your password",
  newpassword: "Choose a new password",
};

export default function AuthModal() {
  const { modal, openAuth, closeAuth, completeGate, cancelGate } = useAuth();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");
  const [sentTo, setSentTo] = useState(""); // set once a confirmation email has been sent

  useEffect(() => {
    setError("");
    setInfo("");
    setPassword("");
    setConfirm("");
    setSentTo("");
  }, [modal]);

  if (!modal) return null;
  const mode: AuthMode = modal;

  function close() {
    cancelGate(); // closing without signing in drops any checkout waiting on it
    closeAuth();
  }

  function done() {
    completeGate(); // continues a checkout that was waiting for sign-in, if any
    closeAuth();
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    setInfo("");
    try {
      const sb = getSupabase();
      if (mode === "signin") {
        const { error } = await sb.auth.signInWithPassword({ email, password });
        if (error) throw error;
        done();
      } else if (mode === "signup") {
        const fullName = name.trim().replace(/\s+/g, " ");
        if (!fullName) throw new Error("Please enter your name.");
        if (password.length < 8) throw new Error("Use at least 8 characters for your password.");
        if (password !== confirm) throw new Error("The two passwords do not match.");
        const { data, error } = await sb.auth.signUp({
          email,
          password,
          options: {
            // The email link brings them back to this page, which shows the "confirmed" message.
            emailRedirectTo: `${window.location.origin}/auth/confirmed`,
            data: { full_name: fullName, display_name: fullName.split(" ")[0] },
          },
        });
        if (error) throw error;
        if (data.user && data.user.identities && data.user.identities.length === 0) {
          throw new Error("An account with that email already exists. Try signing in instead.");
        }
        if (data.session) done();
        else setSentTo(email);
      } else if (mode === "reset") {
        const { error } = await sb.auth.resetPasswordForEmail(email, { redirectTo: window.location.origin });
        if (error) throw error;
        setInfo("If that email has an account, a reset link is on its way.");
      } else {
        if (password.length < 8) throw new Error("Use at least 8 characters for your password.");
        if (password !== confirm) throw new Error("The two passwords do not match.");
        const { error } = await sb.auth.updateUser({ password });
        if (error) throw error;
        closeAuth();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-[100] flex items-start justify-center overflow-y-auto bg-black/60 p-4">
      <div className="my-12 w-full max-w-sm rounded-lg bg-white p-6 text-gray-900 shadow-xl">
        <div className="mb-4 flex items-start justify-between">
          <h2 className="text-xl font-bold">{sentTo ? "Check your email" : TITLES[mode]}</h2>
          <button type="button" onClick={close} aria-label="Close" className="text-2xl leading-none text-gray-400 hover:text-black">
            ×
          </button>
        </div>

        {sentTo ? (
          <div className="space-y-4 text-sm text-gray-800">
            <p className="rounded border border-green-200 bg-green-50 px-3 py-2 text-green-800">
              Almost done! We sent a confirmation link to <span className="font-semibold">{sentTo}</span>.
            </p>
            <p>Click the link in that email to confirm your account. It will bring you back to the store.</p>
            <p className="text-gray-600">Nothing there? Check your spam folder.</p>
            <button type="button" className={primary} onClick={close}>
              Got it
            </button>
          </div>
        ) : (
          <>
            {(mode === "signin" || mode === "signup") && (
              <div className="mb-4 flex rounded bg-gray-100 p-1 text-sm font-semibold">
                {(["signin", "signup"] as const).map((m) => (
                  <button
                    key={m}
                    type="button"
                    onClick={() => openAuth(m)}
                    className={`flex-1 rounded py-1.5 ${mode === m ? "bg-white shadow-sm" : "text-gray-500"}`}
                  >
                    {m === "signin" ? "Sign in" : "Create account"}
                  </button>
                ))}
              </div>
            )}

            <form onSubmit={submit} className="space-y-3">
              {mode === "signup" && (
                <label className="block text-sm font-medium">
                  Full name
                  <input className={`${input} mt-1`} type="text" autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} required />
                </label>
              )}
              {mode !== "newpassword" && (
                <label className="block text-sm font-medium">
                  Email
                  <input className={`${input} mt-1`} type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
                </label>
              )}
              {mode !== "reset" && (
                <label className="block text-sm font-medium">
                  {mode === "newpassword" ? "New password" : "Password"}
                  <PasswordField value={password} onChange={setPassword} autoComplete={mode === "signin" ? "current-password" : "new-password"} />
                  {(mode === "signup" || mode === "newpassword") && <span className="mt-1 block text-xs font-normal text-gray-500">At least 8 characters.</span>}
                </label>
              )}
              {(mode === "signup" || mode === "newpassword") && (
                <label className="block text-sm font-medium">
                  Confirm password
                  <PasswordField value={confirm} onChange={setConfirm} autoComplete="new-password" />
                </label>
              )}
              {error && <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">{error}</p>}
              {info && <p className="rounded border border-green-200 bg-green-50 px-3 py-2 text-sm text-green-800">{info}</p>}
              <button className={primary} disabled={busy}>
                {busy ? "Please wait…" : mode === "reset" ? "Send reset link" : TITLES[mode]}
              </button>
            </form>

            {mode === "signin" && (
              <button type="button" onClick={() => openAuth("reset")} className="mt-3 text-sm text-gray-600 underline">
                Forgot your password?
              </button>
            )}
            {mode === "reset" && (
              <button type="button" onClick={() => openAuth("signin")} className="mt-3 text-sm text-gray-600 underline">
                Back to sign in
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );
}
