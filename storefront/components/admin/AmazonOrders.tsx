"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Badge, Notice, Pager, btn2Cls, inputCls } from "./ui";
import { money, useAmazonAccounts } from "./amazonShared";

type Line = {
  id: number; order_pk: number; account: string; amazon_order_id: string; sku: string | null; asin: string | null; title: string | null; quantity: number;
  unit_price: number | null; state: string; product_id: number | null; product_sku: string | null; pieces_per_sale: number; sale_date: string | null;
  cancelled: boolean; shipped_at: string | null; tracking_number: string | null; tracking_carrier: string | null; fulfillment: string | null;
};
const LIMIT = 50;
const STATES = ["", "reserved", "dropship", "picked", "history", "unmatched", "cancelled"];

/* Amazon > Orders: every order line, read only, plus Poll now. Picking and shipping happen on Inventory > Pick & ship. */
export default function AmazonOrders() {
  const { accounts, account, setAccount } = useAmazonAccounts();
  const [rows, setRows] = useState<Line[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [state, setState] = useState("");
  const [q, setQ] = useState("");
  const [qIn, setQIn] = useState("");
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const p = new URLSearchParams({ offset: String(offset), limit: String(LIMIT) });
      if (account) p.set("account", account);
      if (state) p.set("state", state);
      if (q) p.set("q", q);
      const r = await api<{ total: number; items: Line[] }>(`/amazon-sales/lines?${p}`);
      setRows(r.items);
      setTotal(r.total);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy(false);
    }
  }, [offset, account, state, q]);
  useEffect(() => {
    void load();
  }, [load]);

  async function poll(days?: number) {
    setMsg(null);
    setBusy(true);
    try {
      const r = await api<{ ok?: boolean; summary?: string }>("/amazon-sales/poll-now", { method: "POST", body: { account, ...(days ? { days } : {}) } });
      setMsg({ kind: "success", text: r.summary ?? "Checked Amazon for orders." });
      await load();
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
      setBusy(false);
    }
  }

  async function pollFinances() {
    setMsg(null);
    setBusy(true);
    try {
      const r = await api<{ lines_with_fees: number; refund_events: number; more_waiting: boolean }>("/amazon-finances/poll-now", { method: "POST", body: { account } });
      setMsg({ kind: "success", text: `Fees saved on ${r.lines_with_fees} line(s); ${r.refund_events} refund event(s) read.${r.more_waiting ? " More is waiting: press it again." : ""}` });
      await load();
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3">
      <p className="text-xs text-gray-500">
        Amazon orders, read only. Parts are set aside when an order arrives; pick and ship them on <strong>Inventory &gt; Pick &amp; ship</strong>.
      </p>
      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}
      <div className="flex flex-wrap items-end gap-2">
        <select className={`${inputCls} !w-48`} value={account} onChange={(e) => { setAccount(e.target.value); setOffset(0); }} aria-label="Account">
          <option value="">All accounts</option>
          {accounts.map((a) => <option key={a.account} value={a.account}>{a.display_name || a.account}</option>)}
        </select>
        <select className={`${inputCls} !w-44`} value={state} onChange={(e) => { setState(e.target.value); setOffset(0); }} aria-label="State">
          {STATES.map((s) => <option key={s} value={s}>{s || "Every state"}</option>)}
        </select>
        <input className={`${inputCls} !w-72`} placeholder="Order #, SKU, ASIN, title or tracking" value={qIn} onChange={(e) => setQIn(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") { setQ(qIn.trim()); setOffset(0); } }} />
        <button className={btn2Cls} onClick={() => { setQ(qIn.trim()); setOffset(0); }}>Search</button>
        <button className={btn2Cls} disabled={busy} onClick={() => void load()}>{busy ? "Loading…" : "Refresh"}</button>
        <button className={btn2Cls} disabled={!account || busy} title={account ? "" : "Pick an account first"} onClick={() => void poll()}>Poll now</button>
        <button className={btn2Cls} disabled={!account || busy} title="Reads Amazon's fees and refunds for these orders. It also runs about once an hour by itself." onClick={() => void pollFinances()}>Read fees &amp; refunds</button>
        <button className={btn2Cls} disabled={!account || busy} title="Re-read the last 30 days" onClick={() => void poll(30)}>Re-read 30 days</button>
      </div>
      <Pager total={total} limit={LIMIT} offset={offset} onChange={setOffset} />
      <div className="overflow-x-auto rounded border border-gray-200 bg-white">
        <table className="w-full min-w-[70rem] text-left text-sm">
          <thead className="bg-gray-50 text-xs uppercase text-gray-500">
            <tr><th className="px-3 py-2">Date</th><th className="px-3 py-2">Order #</th><th className="px-3 py-2">Item</th><th className="px-3 py-2">Qty</th><th className="px-3 py-2">Price</th><th className="px-3 py-2">State</th><th className="px-3 py-2">Tracking</th></tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {rows.map((l) => (
              <tr key={l.id} className={l.shipped_at ? "bg-green-50" : ""}>
                <td className="whitespace-nowrap px-3 py-2 text-gray-600">{l.sale_date ? new Date(l.sale_date).toLocaleString() : "—"}</td>
                <td className="whitespace-nowrap px-3 py-2">{l.amazon_order_id}</td>
                <td className="max-w-[24rem] px-3 py-2">
                  <div className="truncate" title={l.title ?? ""}>{l.title ?? "(no title)"}</div>
                  <div className="text-xs text-blue-600">{l.sku ?? "—"} · {l.asin ?? "—"}{l.product_sku ? ` · ${l.product_sku}` : ""}</div>
                </td>
                <td className="px-3 py-2">{l.quantity}{l.pieces_per_sale > 1 && <div className="text-xs font-medium text-amber-700">× {l.pieces_per_sale} pieces</div>}</td>
                <td className="px-3 py-2">{money(l.unit_price)}</td>
                <td className="px-3 py-2"><Badge tone={l.state === "reserved" ? "amber" : l.state === "dropship" ? "blue" : l.state === "unmatched" ? "gray" : "green"}>{l.state}</Badge>{l.cancelled && <span className="ml-1 text-xs text-red-700">cancelled</span>}</td>
                <td className="px-3 py-2 text-xs">{l.tracking_number ? `${l.tracking_carrier ?? ""} ${l.tracking_number}` : "—"}</td>
              </tr>
            ))}
            {rows.length === 0 && <tr><td colSpan={7} className="px-3 py-6 text-center text-gray-500">{busy ? "Loading…" : "No Amazon orders yet. Press Poll now."}</td></tr>}
          </tbody>
        </table>
      </div>
      <Pager total={total} limit={LIMIT} offset={offset} onChange={setOffset} />
    </div>
  );
}
