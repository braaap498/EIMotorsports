"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { api, ApiError, errMsg } from "../../lib/adminApi";
import { getSupabase } from "../../lib/supabase";
import { Notice, btnCls, btn2Cls, inputCls, Field } from "./ui";
import PasswordInput from "./PasswordInput";
import { AdminAccessProvider } from "./AdminAccess";

type Status = "loading" | "signedout" | "forbidden" | "error" | "ok";

// `any`: shown to employees who hold at least one of these permissions.
// Items without `any` are for owners and admins only.
const NAV: { href: string; label: string; exact?: boolean; any?: string[]; match?: string[] }[] = [
  { href: "/admin", label: "Dashboard", exact: true, any: ["products.view", "fitment.view"] },
  { href: "/admin/products", label: "Products", any: ["products.view", "fitment.view"] },
  {
    href: "/admin/inventory",
    label: "Inventory",
    any: ["inventory.receive", "inventory.ship", "inventory.barcodes", "inventory.adjust"],
  },
  { href: "/admin/purchase-orders", label: "Purchase orders", any: ["inventory.purchase"] },
  { href: "/admin/models", label: "Fitment", match: ["/admin/models", "/admin/fitment"] },
  { href: "/admin/cleanup", label: "eBay", match: ["/admin/cleanup", "/admin/ebay-new", "/admin/ebay-photos"] },
  { href: "/admin/quickbooks", label: "QuickBooks" },
  { href: "/admin/users", label: "Team" },
  { href: "/admin/about", label: "About page" },
  { href: "/admin/help", label: "Help pages" },
];

// Sub-pages shown as tabs under the Fitment button.
const FITMENT_TABS = [
  { href: "/admin/models", label: "Models & years" },
  { href: "/admin/fitment", label: "Types & makes" },
];

// Sub-pages shown as tabs under the eBay button.
const EBAY_TABS = [
  { href: "/admin/cleanup", label: "Merge existing products" },
  { href: "/admin/ebay-new", label: "Link new eBay listings to a product" },
  { href: "/admin/ebay-photos", label: "eBay photo import" },
];

function isActive(n: (typeof NAV)[number], pathname: string) {
  if (n.match) return n.match.some((m) => pathname.startsWith(m));
  return n.exact ? pathname === n.href : pathname.startsWith(n.href);
}

// An employee who can only use some pages lands on /admin; send them to the first one they may open.
function Redirect({ to }: { to: string }) {
  const router = useRouter();
  useEffect(() => {
    router.replace(to);
  }, [router, to]);
  return <p className="p-8 text-sm text-gray-500">Loading…</p>;
}

function LoginForm() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");

  async function forgot() {
    setError("");
    setInfo("");
    if (!email.trim()) {
      setError("Type your email above first, then click “Forgot password?”.");
      return;
    }
    const { error } = await getSupabase().auth.resetPasswordForEmail(email.trim(), { redirectTo: window.location.origin });
    if (error) setError(error.message);
    else setInfo("A reset link has been sent to the email associated with this admin account. Open it on this device and choose a new password in the window that appears.");
  }

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
        {info && <Notice kind="success">{info}</Notice>}
        <button className={`${btnCls} w-full`} disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
        <button type="button" onClick={forgot} className="w-full text-center text-sm text-gray-600 underline">
          Forgot password?
        </button>
      </form>
    </div>
  );
}

export default function AdminShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const [status, setStatus] = useState<Status>("loading");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("");
  const [permissions, setPermissions] = useState<string[]>([]);
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
        const me = await api<{ email: string | null; role: string; permissions?: string[] }>("/me");
        if (!active) return;
        setEmail(me.email ?? "");
        setRole(me.role);
        setPermissions(me.permissions ?? []);
        setStatus(["owner", "admin", "staff"].includes(me.role) ? "ok" : "forbidden");
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

  const isAdmin = role === "admin" || role === "owner";
  const navItems = NAV.filter((n) => isAdmin || (n.any ?? []).some((k) => permissions.includes(k)));
  // An employee whose account has no permissions yet can sign in but sees nothing to do.
  const nothingGranted = !isAdmin && navItems.length === 0;
  // Typing an admin-only address into the browser: say so instead of showing a page full of errors.
  const here = NAV.filter((n) => isActive(n, pathname)).sort(
    (a, b) => b.href.length - a.href.length
  )[0];
  const blocked = !isAdmin && !nothingGranted && !!here && !navItems.includes(here);

  return (
    <AdminAccessProvider role={role} permissions={permissions}>
    <div className="flex min-h-screen flex-col bg-gray-50 text-gray-900 md:flex-row">
      <aside className="border-b border-gray-200 bg-white md:sticky md:top-0 md:flex md:h-screen md:w-56 md:shrink-0 md:flex-col md:self-start md:overflow-y-auto md:border-b-0 md:border-r">
        <div className="px-4 py-4 text-sm font-bold uppercase tracking-wide">E&amp;I Admin</div>
        <nav className="flex gap-1 overflow-x-auto px-2 pb-2 md:flex-col md:pb-0">
          {navItems.map((n) => {
            const active = isActive(n, pathname);
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
        <div className="hidden px-4 py-4 text-xs text-gray-500 md:mt-auto md:block">
          <div className="mb-2 break-all">{email}</div>
          <Link href="/" className="mr-3 underline">View store</Link>
          <button onClick={signOut} className="underline">Sign out</button>
        </div>
      </aside>
      <main className="min-w-0 flex-1 p-4 md:p-6">
        <div className="w-full">
          {blocked && pathname === "/admin" && navItems[0] ? (
            <Redirect to={navItems[0].href} />
          ) : nothingGranted || blocked ? (
            <Notice kind="error">
              {nothingGranted
                ? "Your account is set up, but no permissions have been given to it yet. Ask an admin to turn some on."
                : "Your account doesn't have access to this page."}
            </Notice>
          ) : (
            <>
              {[
                { title: "Fitment", tabs: FITMENT_TABS },
                { title: "eBay", tabs: EBAY_TABS },
              ].map(
                (g) =>
                  g.tabs.some((t) => pathname.startsWith(t.href)) && (
                    <div key={g.title}>
                      <h1 className="mb-3 text-2xl font-semibold">{g.title}</h1>
                      <div className="mb-4 flex gap-1 overflow-x-auto border-b border-gray-200">
                        {g.tabs.map((t) => {
                          const on = pathname.startsWith(t.href);
                          return (
                            <Link
                              key={t.href}
                              href={t.href}
                              className={`-mb-px whitespace-nowrap border-b-2 px-4 py-2 text-sm font-medium ${
                                on ? "border-black text-black" : "border-transparent text-gray-500 hover:text-gray-800"
                              }`}
                            >
                              {t.label}
                            </Link>
                          );
                        })}
                      </div>
                    </div>
                  )
              )}
              {children}
            </>
          )}
        </div>
        <div className="mt-8 text-xs text-gray-500 md:hidden">
          {email} ·{" "}
          <button onClick={signOut} className="underline">Sign out</button>
        </div>
      </main>
    </div>
    </AdminAccessProvider>
  );
}
