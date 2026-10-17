"use client";

import { useEffect, useState } from "react";
import { getSupabase } from "../../lib/supabase";
import { useAuth, type AuthMode } from "./AuthProvider";

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
  const { modal, openAuth, closeAuth } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");

  useEffect(() => {
    setError("");
    setInfo("");
    setPassword("");
  }, [modal]);

  if (!modal) return null;
  const mode: AuthMode = modal;

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
        closeAuth();
      } else if (mode === "signup") {
        if (password.length < 8) throw new Error("Use at least 8 characters for your password.");
        const { data, error } = await sb.auth.signUp({
          email,
          password,
          options: { emailRedirectTo: window.location.origin },
        });
        if (error) throw error;
        if (data.session) closeAuth();
        else setInfo("Almost done! Check your email and click the link to confirm your account, then sign in.");
      } else if (mode === "reset") {
        const { error } = await sb.auth.resetPasswordForEmail(email, { redirectTo: window.location.origin });
        if (error) throw error;
        setInfo("If that email has an account, a reset link is on its way.");
      } else {
        if (password.length < 8) throw new Error("Use at least 8 characters for your password.");
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
          <h2 className="text-xl font-bold">{TITLES[mode]}</h2>
          <button type="button" onClick={closeAuth} aria-label="Close" className="text-2xl leading-none text-gray-400 hover:text-black">
            ×
          </button>
        </div>

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
          {mode !== "newpassword" && (
            <label className="block text-sm font-medium">
              Email
              <input className={`${input} mt-1`} type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
            </label>
          )}
          {mode !== "reset" && (
            <label className="block text-sm font-medium">
              {mode === "newpassword" ? "New password" : "Password"}
              <input
                className={`${input} mt-1`}
                type="password"
                autoComplete={mode === "signin" ? "current-password" : "new-password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
              />
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
      </div>
    </div>
  );
}
