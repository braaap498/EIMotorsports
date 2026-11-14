"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Notice, btnCls, btn2Cls, inputCls } from "./ui";
import { PoReview, type DsJob } from "./DropShipReview";

type Settings = { auto_po: boolean; last_scan_at: string | null; last_scan_note: string | null };

const STAGE: Record<string, string> = {
  review_po: "Waiting for you to check the PO",
  po_confirmed: "PO checked; quote not fetched",
  review_quote: "Waiting for you to check the quote",
  placing: "Placing the order…",
  placed: "Order placed; waiting for tracking",
  review_tracking: "Waiting for you to check the tracking",
  done: "Done: eBay told it shipped",
  held: "On hold",
  problem: "Needs attention",
};

export default function DropShipAdmin() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [jobs, setJobs] = useState<DsJob[]>([]);
  const [order, setOrder] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);

  const load = useCallback(async () => {
    try {
      const [s, j] = await Promise.all([api<Settings>("/dropship/settings"), api<{ jobs: DsJob[] }>("/dropship/jobs")]);
      setSettings(s);
      setJobs(j.jobs);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function run<T>(key: string, fn: () => Promise<T>, ok: (r: T) => string) {
    setBusy(key);
    setMsg(null);
    try {
      const r = await fn();
      setMsg({ kind: "success", text: ok(r) });
      await load();
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="max-w-5xl space-y-5">
      <div>
        <h1 className="text-lg font-semibold">Drop ship</h1>
        <p className="text-sm text-gray-600">
          An eBay order for a Drop ship product that is out of stock on our shelf makes its own purchase order to the distributor. For now
          you check the PO, then the quote (then the order is placed), then the tracking before eBay is told.
        </p>
      </div>

      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}

      {settings && (
        <div className="space-y-3 rounded border border-gray-200 p-4 text-sm">
          <label className="flex items-start gap-2">
            <input
              type="checkbox"
              className="mt-1"
              checked={settings.auto_po}
              disabled={busy !== ""}
              onChange={(e) =>
                void run("auto", () => api<Settings>("/dropship/settings", { method: "PUT", body: { auto_po: e.target.checked } }), (r) =>
                  r.auto_po ? "New eBay orders will now make their PO by themselves." : "Automatic POs are off."
                )
              }
            />
            <span>
              <span className="font-medium">Make POs automatically</span>
              <span className="block text-xs text-gray-500">
                Each time the eBay orders are checked, a paid order from the last 10 days with a Drop ship product that is out of stock gets
                its PO. Global Shipping Program orders and incomplete addresses are never made; they show here as needing attention.
              </span>
            </span>
          </label>
          <div className="flex flex-wrap items-center gap-2">
            <button type="button" className={btn2Cls} disabled={busy !== ""} onClick={() => void run("scan", () => api<{ note: string }>("/dropship/scan", { method: "POST" }), (r) => r.note)}>
              {busy === "scan" ? "Checking…" : "Check for new orders and tracking now"}
            </button>
            {settings.last_scan_note && <span className="text-xs text-gray-500">Last check: {settings.last_scan_note}</span>}
          </div>
          <div className="flex flex-wrap items-center gap-2 border-t border-gray-100 pt-3">
            <input className={`${inputCls} max-w-xs`} placeholder="eBay order number, to test with" value={order} onChange={(e) => setOrder(e.target.value)} />
            <button
              type="button"
              className={btnCls}
              disabled={busy !== "" || order.trim().length < 3}
              onClick={() =>
                void run("order", () => api<{ po_number: number }>("/dropship/jobs/from-order", { method: "POST", body: { order: order.trim() } }), (r) => {
                  setOrder("");
                  return `PO ${r.po_number} made. It is waiting for your check.`;
                })
              }
            >
              {busy === "order" ? "Making…" : "Make the PO now"}
            </button>
          </div>
        </div>
      )}

      <div className="space-y-2">
        {jobs.length === 0 && <p className="text-sm text-gray-500">No drop ship orders yet.</p>}
        {jobs.map((j) => (
          <div key={j.id} className="rounded border border-gray-200 p-3 text-sm">
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
              <span className="font-mono">{j.ebay_order_id}</span>
              {j.po_number != null && <span>PO {j.po_number}</span>}
              {j.buyer_name && <span>{j.buyer_name}</span>}
              {j.ship_service && <span className="text-gray-600">{j.ship_service}</span>}
              <span className={`ml-auto rounded px-2 py-0.5 text-xs ${j.stage === "problem" ? "bg-red-50 text-red-700" : ["review_po", "po_confirmed", "review_quote", "review_tracking"].includes(j.stage) ? "bg-amber-50 text-amber-800" : "bg-gray-100 text-gray-700"}`}>
                {STAGE[j.stage] ?? j.stage}
              </span>
            </div>
            {j.problem && <p className="mt-1 text-xs text-red-700">{j.problem}</p>}
            <div className="mt-2 flex flex-wrap gap-2">
              {["review_po", "po_confirmed", "review_quote", "review_tracking"].includes(j.stage) && (
                <button type="button" className={btn2Cls} onClick={() => setOpen(open === j.id ? null : j.id)}>
                  {open === j.id ? "Close" : "Review"}
                </button>
              )}
              {j.stage === "problem" && (
                <button type="button" className={btn2Cls} disabled={busy !== ""} onClick={() => void run("retry", () => api(`/dropship/jobs/${j.id}/retry`, { method: "POST" }), () => "It will be tried again at the next check.")}>
                  Try again
                </button>
              )}
            </div>
            {open === j.id && ["review_po", "po_confirmed", "review_quote", "review_tracking"].includes(j.stage) && (
              <div className="mt-3 border-t border-gray-100 pt-3">
                <PoReview job={j} onDone={() => { setOpen(null); void load(); }} />
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
