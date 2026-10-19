"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useAuth } from "../../../components/site/AuthProvider";

// Where the link in the confirmation email lands. The sign-up form sends people
// here with emailRedirectTo, and Supabase signs them in on arrival.
export default function Confirmed() {
  const { user, ready, openAuth } = useAuth();
  const [problem, setProblem] = useState<string | null>(null);

  // A used or expired link comes back with an error in the address. Read it
  // once, before the Supabase client tidies the address bar.
  useEffect(() => {
    const raw = `${window.location.hash.replace(/^#/, "")}&${window.location.search.replace(/^\?/, "")}`;
    const params = new URLSearchParams(raw);
    if (params.get("error") || params.get("error_code") || params.get("error_description")) {
      setProblem(params.get("error_description")?.replace(/\+/g, " ") ?? "That confirmation link is not valid.");
    }
  }, []);

  const card = "mx-auto mt-10 max-w-lg rounded-lg border border-gray-200 bg-white p-8 text-center shadow-sm";
  const primary = "inline-block rounded bg-[#06bde7] px-5 py-2.5 text-sm font-bold text-[#04202b] hover:brightness-110";
  const secondary = "inline-block rounded border border-gray-300 px-5 py-2.5 text-sm font-bold text-gray-900 hover:bg-gray-50";

  if (problem) {
    return (
      <main className="px-4 pb-16">
        <div className={card}>
          <h1 className="text-2xl font-extrabold text-gray-900">That link did not work</h1>
          <p className="mt-3 text-gray-600">
            It may have expired or already been used. If you have already confirmed your account, just sign in.
            Otherwise create your account again and we will send a fresh link.
          </p>
          <div className="mt-6 flex flex-wrap justify-center gap-3">
            <button type="button" onClick={() => openAuth("signin")} className={primary}>Sign in</button>
            <button type="button" onClick={() => openAuth("signup")} className={secondary}>Create account</button>
          </div>
        </div>
      </main>
    );
  }

  if (!ready) {
    return (
      <main className="px-4 pb-16">
        <div className={card}>
          <p className="text-gray-600">Confirming your account…</p>
        </div>
      </main>
    );
  }

  if (user) {
    const first = (user.user_metadata as { display_name?: string } | undefined)?.display_name;
    return (
      <main className="px-4 pb-16">
        <div className={card}>
          <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-full bg-green-100 text-3xl text-green-700" aria-hidden="true">
            ✓
          </div>
          <h1 className="text-2xl font-extrabold text-gray-900">Your account is confirmed{first ? `, ${first}` : ""}!</h1>
          <p className="mt-3 text-gray-600">You are signed in. Add your shipping and billing addresses now to check out faster.</p>
          <div className="mt-6 flex flex-wrap justify-center gap-3">
            <Link href="/account" className={primary}>Set up my account</Link>
            <Link href="/" className={secondary}>Start shopping</Link>
          </div>
        </div>
      </main>
    );
  }

  // Confirmed, but this browser did not receive a sign-in (for example, the link was opened on another device).
  return (
    <main className="px-4 pb-16">
      <div className={card}>
        <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-full bg-green-100 text-3xl text-green-700" aria-hidden="true">
          ✓
        </div>
        <h1 className="text-2xl font-extrabold text-gray-900">Your email is confirmed</h1>
        <p className="mt-3 text-gray-600">Sign in with your email and password to continue.</p>
        <div className="mt-6">
          <button type="button" onClick={() => openAuth("signin")} className={primary}>Sign in</button>
        </div>
      </div>
    </main>
  );
}
