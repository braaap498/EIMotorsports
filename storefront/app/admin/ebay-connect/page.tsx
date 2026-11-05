"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Notice, btnCls } from "../../../components/admin/ui";

type Done = { ok: boolean; store: string; ebay_username: string; refresh_token_expires_at: string };

/** eBay sends the person back here after "I Agree" with a one-time code in the address. */
export default function EbayConnectPage() {
  const [state, setState] = useState<"working" | "done" | "error">("working");
  const [result, setResult] = useState<Done | null>(null);
  const [error, setError] = useState("");
  const started = useRef(false); // the code works once, so never send it twice

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const p = new URLSearchParams(window.location.search);
    const code = p.get("code");
    const st = p.get("state");
    // take the one-time code out of the address bar and history right away
    window.history.replaceState(null, "", window.location.pathname);
    if (!code || !st) {
      setState("error");
      setError(
        p.get("error")
          ? "eBay did not approve the request."
          : "This page is where eBay sends you after approval. Start from the Connect button on Store API → Stores."
      );
      return;
    }
    api<Done>("/ebay-connect/callback", { method: "POST", body: { code, state: st } })
      .then((r) => {
        setResult(r);
        setState("done");
      })
      .catch((e) => {
        setError(errMsg(e));
        setState("error");
      });
  }, []);

  return (
    <div className="max-w-2xl space-y-4">
      <h1 className="text-2xl font-semibold">Connect eBay</h1>
      {state === "working" && <p className="text-sm text-gray-600">Finishing the approval with eBay…</p>}
      {state === "done" && result && (
        <Notice kind="success">
          {result.store} is connected as {result.ebay_username}. The approval lasts until{" "}
          {new Date(result.refresh_token_expires_at).toLocaleDateString()}.
        </Notice>
      )}
      {state === "error" && <Notice kind="error">{error}</Notice>}
      {state !== "working" && (
        <Link href="/admin/ebay-stores" className={btnCls}>
          Back to Stores
        </Link>
      )}
    </div>
  );
}
