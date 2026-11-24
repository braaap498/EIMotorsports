"use client";

import { useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Notice, btnCls, btn2Cls, inputCls } from "./ui";

/* For an Amazon drop ship order that stopped because the app has no buyer-address access (no Direct-to-Consumer Shipping role).
   Staff open the order in Seller Central, copy the ship-to here, and the app makes the purchase order. The address is written onto
   the PO only (the same as every other buyer address) and is removed by the usual cleanup job. */

export const NEEDS_ADDRESS = "Needs ship-to address:";
export const needsAddress = (problem: string | null | undefined) => !!problem && problem.startsWith(NEEDS_ADDRESS);

const EMPTY = { full_name: "", line1: "", line2: "", city: "", region: "", postal_code: "", country: "US", phone: "" };

export default function AmazonAddressForm({ jobId, orderRef, onDone }: { jobId: number; orderRef?: string; onDone: () => void }) {
  const [f, setF] = useState(EMPTY);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const set = (k: keyof typeof EMPTY) => (e: React.ChangeEvent<HTMLInputElement>) => setF({ ...f, [k]: e.target.value });
  const ok = f.full_name.trim() && f.line1.trim() && f.city.trim() && f.region.trim().length >= 2 && f.postal_code.trim().length >= 3;

  async function submit() {
    setBusy(true);
    setErr(null);
    try {
      await api(`/dropship/jobs/${jobId}/amazon-address`, { method: "POST", body: { ...f, country: (f.country || "US").toUpperCase() } });
      onDone();
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-2 rounded border border-gray-200 bg-white p-3 text-sm">
      <div className="text-xs font-semibold uppercase text-gray-500">Ship-to address{orderRef ? ` for ${orderRef}` : ""} (copy from Seller Central)</div>
      <div className="grid gap-2 sm:grid-cols-2">
        <input className={inputCls} placeholder="Full name" value={f.full_name} onChange={set("full_name")} />
        <input className={inputCls} placeholder="Phone (optional)" value={f.phone} onChange={set("phone")} />
        <input className={inputCls} placeholder="Address line 1" value={f.line1} onChange={set("line1")} />
        <input className={inputCls} placeholder="Address line 2 (optional)" value={f.line2} onChange={set("line2")} />
        <input className={inputCls} placeholder="City" value={f.city} onChange={set("city")} />
        <div className="flex gap-2">
          <input className={inputCls} placeholder="State" value={f.region} onChange={set("region")} />
          <input className={inputCls} placeholder="ZIP" value={f.postal_code} onChange={set("postal_code")} />
          <input className={`${inputCls} !w-16`} placeholder="US" maxLength={2} value={f.country} onChange={set("country")} />
        </div>
      </div>
      {err && <Notice kind="error">{err}</Notice>}
      <div className="flex gap-2">
        <button type="button" className={btnCls} disabled={busy || !ok} onClick={() => void submit()}>{busy ? "Making the PO…" : "Make the drop ship PO"}</button>
        <button type="button" className={btn2Cls} disabled={busy} onClick={() => setF(EMPTY)}>Clear</button>
      </div>
      <p className="text-xs text-gray-500">Drop shipping is set up for US addresses only. Plain packaging: no distributor branding or invoice (you are the seller of record).</p>
    </div>
  );
}
