"use client";

// TEMPORARY check while automatic drop shipping is proved out: when an eBay order has made its purchase order by itself, this
// pop-up shows the PO and waits for a person to say it is correct (or to hold the order).
import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Notice, btnCls, btn2Cls } from "./ui";

export type DsLine = { id: number; sku: string | null; item: string | null; description: string; quantity: number; rate: number; amount: number };
export type DsPo = { id: number; po_number: number; vendor_name: string; ship_to: string; note: string | null; total: number; lines: DsLine[] };
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
  po?: DsPo | null;
};

export function PoReview({ job, onDone, onClose }: { job: DsJob; onDone: () => void; onClose?: () => void }) {
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const po = job.po;

  async function act(kind: "confirm" | "hold") {
    setBusy(kind);
    setErr(null);
    try {
      await api(`/dropship/jobs/${job.id}/${kind}`, { method: "POST" });
      onDone();
    } catch (e) {
      setErr(errMsg(e));
      setBusy("");
    }
  }

  return (
    <div className="space-y-3 text-sm">
      <p className="text-gray-600">
        eBay order <span className="font-mono">{job.ebay_order_id}</span>
        {job.store ? ` (${job.store})` : ""} made PO {job.po_number}. Check it against the order before anything is sent to the distributor.
      </p>
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
        <button type="button" className={btnCls} disabled={busy !== "" || !po} onClick={() => void act("confirm")}>
          {busy === "confirm" ? "Saving…" : "The PO is correct. Continue"}
        </button>
        <button type="button" className={btn2Cls} disabled={busy !== ""} onClick={() => void act("hold")}>
          Hold this order
        </button>
        {onClose && (
          <button type="button" className="ml-auto text-xs underline" onClick={onClose}>
            Decide later
          </button>
        )}
      </div>
      <p className="text-xs text-gray-500">The next step (a quote from the distributor) is not switched on yet, so continuing only records that you checked the PO.</p>
    </div>
  );
}

export default function DropShipReview() {
  const [jobs, setJobs] = useState<DsJob[]>([]);
  const [later, setLater] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await api<{ jobs: DsJob[] }>("/dropship/jobs?stage=review_po");
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
        <h2 className="mb-2 text-lg font-semibold">Check this drop ship PO</h2>
        <PoReview job={job} onDone={() => void load()} onClose={() => setLater(job.id)} />
        {jobs.length > 1 && <p className="mt-3 text-xs text-gray-500">{jobs.length - 1} more waiting after this one.</p>}
      </div>
    </div>
  );
}
