"use client";

// TEMPORARY check while automatic drop shipping is proved out: when an eBay order has made its purchase order by itself, this
// pop-up shows the PO and waits for a person to say it is correct (or to hold the order).
import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Notice, btnCls, btn2Cls } from "./ui";

export type DsLine = { id: number; sku: string | null; item: string | null; description: string; quantity: number; rate: number; amount: number };
export type DsPo = { id: number; po_number: number; vendor_name: string; ship_to: string; note: string | null; total: number; lines: DsLine[] };
export type DsQuote = {
  mode?: string;
  server?: string;
  method?: string | null;
  options?: { name: string | null; cost: string | number | null; date: string | null }[];
  totals?: {
    sales_amount?: string | number | null;
    sales_tax?: string | number | null;
    special_charge?: string | number | null;
    order_value?: string | number | null;
    invoice_total?: string | number | null;
    message?: string | null;
  };
};
export type DsJob = {
  id: number;
  ebay_order_id: string;
  store: string | null;
  po_number: number | null;
  stage: string;
  ship_service: string | null;
  buyer_name: string | null;
  problem: string | null;
  created_at: string;
  quote?: DsQuote | null;
  ship_method?: string | null;
  supplier_order?: string | null;
  supplier_mode?: string | null;
  tracking_number?: string | null;
  tracking_carrier?: string | null;
  po?: DsPo | null;
};

const money = (v: string | number | null | undefined) => (v == null || v === "" ? "–" : Number.isNaN(Number(v)) ? String(v) : Number(v).toFixed(2));

export function PoReview({ job, onDone, onClose }: { job: DsJob; onDone: () => void; onClose?: () => void }) {
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const po = job.po;
  const stage = job.stage;
  const q = job.quote;

  async function act(kind: "confirm" | "hold" | "place" | "send-tracking") {
    setBusy(kind);
    setErr(null);
    try {
      await api(`/dropship/jobs/${job.id}/${kind}`, { method: "POST" });
      onDone();
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy("");
    }
  }

  async function requote(method: string | null) {
    setBusy("quote");
    setErr(null);
    try {
      await api(`/dropship/jobs/${job.id}/quote`, { method: "POST", body: { method } });
      onDone();
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="space-y-3 text-sm">
      <p className="text-gray-600">
        eBay order <span className="font-mono">{job.ebay_order_id}</span>
        {job.store ? ` (${job.store})` : ""} made PO {job.po_number}.{" "}
        {stage === "review_po" && "Check it against the order before anything is sent to the distributor."}
        {stage === "po_confirmed" && "The PO is confirmed, but the quote could not be fetched."}
        {stage === "review_quote" && "Check the quote. The next button places the order for real."}
        {stage === "review_tracking" && "Tracking came back. The next button tells eBay the order shipped and eBay notifies the buyer."}
      </p>
      {job.problem && <Notice kind="error">{job.problem}</Notice>}
      {stage === "review_quote" && q && (
        <div className="space-y-2 rounded border border-gray-200 p-3">
          <div className="text-xs font-semibold uppercase text-gray-500">
            Quote from Automatic Distributors{q.mode ? ` (${q.mode} server)` : ""}
            {q.mode === "live" && <span className="ml-2 rounded bg-red-50 px-1 text-red-700">REAL ORDER</span>}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <span>Shipping option:</span>
            <select
              className="rounded border border-gray-300 px-2 py-1 text-sm"
              value={job.ship_method ?? ""}
              disabled={busy !== ""}
              onChange={(e) => e.target.value && void requote(e.target.value)}
            >
              <option value="">{job.ship_method ? job.ship_method : "Pick one (no safe match to the buyer's choice)"}</option>
              {(q.options ?? []).map((o) => (
                <option key={String(o.name)} value={String(o.name)}>
                  {o.name} – {money(o.cost)}{o.date ? `, by ${o.date}` : ""}
                </option>
              ))}
            </select>
            <span className="text-xs text-gray-500">buyer chose: {job.ship_service || "unknown"}</span>
          </div>
          <div className="text-xs text-gray-700">
            Parts {money(q.totals?.sales_amount)} · tax {money(q.totals?.sales_tax)} · shipping and fees {money(q.totals?.special_charge)} · order total{" "}
            <b>{money(q.totals?.invoice_total ?? q.totals?.order_value)}</b>
          </div>
          {q.totals?.message && <div className="text-xs italic text-gray-600">{q.totals.message}</div>}
        </div>
      )}
      {stage === "review_tracking" && (
        <div className="rounded border border-gray-200 p-3">
          <div className="text-xs font-semibold uppercase text-gray-500">Tracking</div>
          <div className="font-mono">{job.tracking_carrier || "carrier unknown"} {job.tracking_number}</div>
          <div className="text-xs text-gray-500">Distributor order {job.supplier_order}{job.supplier_mode ? ` (${job.supplier_mode} server)` : ""}</div>
        </div>
      )}
      {!po ? (
        <Notice kind="error">The PO could not be loaded.</Notice>
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="rounded border border-gray-200 p-3">
              <div className="mb-1 text-xs font-semibold uppercase text-gray-500">Ship to (the buyer)</div>
              <div className="whitespace-pre-line">{po.ship_to}</div>
            </div>
            <div className="rounded border border-gray-200 p-3">
              <div className="mb-1 text-xs font-semibold uppercase text-gray-500">Buyer&rsquo;s shipping choice on eBay</div>
              <div>{job.ship_service || "eBay gave no shipping service"}</div>
              <div className="mt-2 text-xs font-semibold uppercase text-gray-500">Vendor</div>
              <div>{po.vendor_name}</div>
            </div>
          </div>
          <div className="overflow-x-auto rounded border border-gray-200">
            <table className="w-full text-left text-xs">
              <thead className="bg-gray-50 text-gray-500">
                <tr>
                  <th className="px-2 py-1">Item</th>
                  <th className="px-2 py-1">Description</th>
                  <th className="px-2 py-1 text-right">Qty</th>
                  <th className="px-2 py-1 text-right">Cost</th>
                  <th className="px-2 py-1 text-right">Amount</th>
                </tr>
              </thead>
              <tbody>
                {po.lines.map((l) => (
                  <tr key={l.id} className="border-t border-gray-100">
                    <td className="px-2 py-1 font-mono">{l.item || "(no part number)"}</td>
                    <td className="px-2 py-1">{l.description}</td>
                    <td className="px-2 py-1 text-right">{l.quantity}</td>
                    <td className="px-2 py-1 text-right">{l.rate.toFixed(2)}</td>
                    <td className="px-2 py-1 text-right">{l.amount.toFixed(2)}</td>
                  </tr>
                ))}
                <tr className="border-t border-gray-300 font-semibold">
                  <td className="px-2 py-1 text-right" colSpan={4}>Total</td>
                  <td className="px-2 py-1 text-right">{po.total.toFixed(2)}</td>
                </tr>
              </tbody>
            </table>
          </div>
          {po.note && <p className="text-xs italic text-gray-600">{po.note}</p>}
          {po.lines.some((l) => !l.item) && <Notice kind="error">A line has no part number, so it cannot be ordered. Fix the PO first.</Notice>}
        </>
      )}
      {err && <Notice kind="error">{err}</Notice>}
      <div className="flex flex-wrap items-center gap-2">
        {stage === "review_po" && (
          <button type="button" className={btnCls} disabled={busy !== "" || !po} onClick={() => void act("confirm")}>
            {busy === "confirm" ? "Getting the quote…" : "The PO is correct. Get the quote"}
          </button>
        )}
        {stage === "po_confirmed" && (
          <button type="button" className={btnCls} disabled={busy !== ""} onClick={() => void requote(null)}>
            {busy === "quote" ? "Getting the quote…" : "Try the quote again"}
          </button>
        )}
        {stage === "review_quote" && (
          <button type="button" className={btnCls} disabled={busy !== "" || !job.ship_method} onClick={() => void act("place")}>
            {busy === "place" ? "Placing…" : q?.mode === "live" ? "The quote is correct. PLACE THE REAL ORDER" : "The quote is correct. Place the order"}
          </button>
        )}
        {stage === "review_tracking" && (
          <button type="button" className={btnCls} disabled={busy !== ""} onClick={() => void act("send-tracking")}>
            {busy === "send-tracking" ? "Telling eBay…" : "Tracking is correct. Tell eBay it shipped"}
          </button>
        )}
        <button type="button" className={btn2Cls} disabled={busy !== ""} onClick={() => void act("hold")}>
          Hold this order
        </button>
        {onClose && (
          <button type="button" className="ml-auto text-xs underline" onClick={onClose}>
            Decide later
          </button>
        )}
      </div>
      <p className="text-xs text-gray-500">Only an owner or admin can place the order. Each stop here is temporary, to be removed once you trust the chain.</p>
    </div>
  );
}

export default function DropShipReview() {
  const [jobs, setJobs] = useState<DsJob[]>([]);
  const [later, setLater] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await api<{ jobs: DsJob[] }>("/dropship/jobs?stage=review");
      setJobs(r.jobs);
    } catch {
      setJobs([]); // no permission, or the server is busy: stay quiet
    }
  }, []);

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 45000);
    return () => clearInterval(t);
  }, [load]);

  const job = jobs.find((j) => j.id !== later);
  if (!job) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="max-h-[90vh] w-full max-w-3xl overflow-auto rounded-lg bg-white p-5 shadow-xl">
        <h2 className="mb-2 text-lg font-semibold">
          {job.stage === "review_po" ? "Check this drop ship PO" : job.stage === "review_quote" ? "Check the quote and place the order" : job.stage === "review_tracking" ? "Check the tracking before eBay is told" : "Drop ship order needs you"}
        </h2>
        <PoReview job={job} onDone={() => void load()} onClose={() => setLater(job.id)} />
        {jobs.length > 1 && <p className="mt-3 text-xs text-gray-500">{jobs.length - 1} more waiting after this one.</p>}
      </div>
    </div>
  );
}
