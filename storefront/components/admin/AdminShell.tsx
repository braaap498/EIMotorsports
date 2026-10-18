"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { api, ApiError, errMsg } from "../../lib/adminApi";
import { getSupabase } from "../../lib/supabase";
import { Notice, btnCls, btn2Cls, inputCls, Field } from "./ui";
import PasswordInput from "./PasswordInput";

type Status = "loading" | "signedout" | "forbidden" | "error" | "ok";

const NAV = [
  { href: "/admin", label: "Dashboard", exact: true },
  { href: "/admin/products", label: "Products" },
  { href: "/admin/models", label: "Models & years" },
  { href: "/admin/fitment", label: "Types & makes" },
  { href: "/admin/cleanup", label: "Parts clean-up" },
  { href: "/admin/users", label: "Admin users" },
];

function LoginForm() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    const { error } = await getSupabase().auth.signInWithPassword({ email, password });
    if (error) setError(error.message);
    setBusy(false);
  }

  return (
    <div className="mx-auto mt-24 w-full max-w-sm rounded-lg border border-gray-200 bg-white p-6 text-gray-900 shadow-sm">
      <h1 className="mb-4 text-xl font-semibold">Admin sign in</h1>
      <form onSubmit={submit} className="space-y-3">
        <Field label="Email">
          <input className={inputCls} type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required />
        </Field>
        <Field label="Password">
          <PasswordInput autoComplete="current-password" value={password} onChange={setPassword} required />
        </Field>
        {error && <Notice kind="error">{error}</Notice>}
        <button className={`${btnCls} w-full`} disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}

export default function AdminShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const [status, setStatus] = useState<Status>("loading");
  const [email, setEmail] = useState("");
  const [problem, setProblem] = useState("");
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let active = true;
    let sb: ReturnType<typeof getSupabase>;
    try {
      sb = getSupabase();
    } catch (e) {
      // Usually: the NEXT_PUBLIC_SUPABASE_* variables are missing on Vercel.
      setProblem(errMsg(e));
      setStatus("error");
      return;
    }

    async function check(hasSession: boolean) {
      if (!hasSession) {
        if (active) setStatus("signedout");
        return;
      }
      try {
        const me = await api<{ email: string | null; role: string }>("/me");
        if (!active) return;
        setEmail(me.email ?? "");
        setStatus(me.role === "admin" || me.role === "owner" ? "ok" : "forbidden");
      } catch (e) {
        if (!active) return;
        if (e instanceof ApiError && e.status === 403) setStatus("forbidden");
        else if (e instanceof ApiError && e.status === 401) setStatus("signedout");
        else {
          setProblem(errMsg(e));
          setStatus("error");
        }
      }
    }

    // Fires once with the current session, then on sign in / sign out / refresh.
    // The check is deferred because supabase-js must not be called from inside
    // this callback.
    const { data } = sb.auth.onAuthStateChange((_event, session) => {
      setTimeout(() => check(!!session), 0);
    });
    return () => {
      active = false;
      data.subscription.unsubscribe();
    };
  }, [attempt]);

  async function signOut() {
    await getSupabase().auth.signOut();
  }

  if (status === "loading") return <p className="p-8 text-sm text-gray-500">Loading…</p>;
  if (status === "signedout") return <LoginForm />;

  if (status === "forbidden" || status === "error") {
    return (
      <div className="mx-auto mt-24 w-full max-w-md space-y-4 p-4">
        {status === "forbidden" ? (
          <Notice kind="error">This account does not have admin access.</Notice>
        ) : (
          <Notice kind="error">The admin could not start: {problem}</Notice>
        )}
        <div className="flex gap-2">
          {status === "error" && (
            <button className={btnCls} onClick={() => setAttempt((n) => n + 1)}>
              Try again
            </button>
          )}
          <button className={btn2Cls} onClick={signOut}>
            Sign out
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen flex-col bg-gray-50 text-gray-900 md:flex-row">
      <aside className="border-b border-gray-200 bg-white md:w-56 md:border-b-0 md:border-r">
        <div className="px-4 py-4 text-sm font-bold uppercase tracking-wide">E&amp;I Admin</div>
        <nav className="flex gap-1 overflow-x-auto px-2 pb-2 md:flex-col md:pb-0">
          {NAV.map((n) => {
            const active = n.exact ? pathname === n.href : pathname.startsWith(n.href);
            return (
              <Link
                key={n.href}
                href={n.href}
                className={`whitespace-nowrap rounded px-3 py-2 text-sm font-medium ${
                  active ? "bg-black text-white" : "text-gray-700 hover:bg-gray-100"
                }`}
              >
                {n.label}
              </Link>
            );
          })}
        </nav>
        <div className="hidden px-4 py-4 text-xs text-gray-500 md:block">
          <div className="mb-2 break-all">{email}</div>
          <Link href="/" className="mr-3 underline">View store</Link>
          <button onClick={signOut} className="underline">Sign out</button>
        </div>
      </aside>
      <main className="min-w-0 flex-1 p-4 md:p-6">
        <div className="mx-auto max-w-6xl">{children}</div>
        <div className="mt-8 text-xs text-gray-500 md:hidden">
          {email} ·{" "}
          <button onClick={signOut} className="underline">Sign out</button>
        </div>
      </main>
    </div>
  );
}
