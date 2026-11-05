"use client";

import { useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Badge, Notice, btnCls, btn2Cls, inputCls } from "./ui";

/* The "Orders" tab: price an order with Automatic Distributors, then place it. Nothing is sent until you press
   "Place order" and confirm. Practice on the TEST server first (AUTOMATIC_DIST_API_URL in Render). */

type Totals = {
  message: string | null;
  sales_amount: string | null;
  sales_tax: string | null;
  excise: string | null;
  special_charge: string | null;
  container_charge: string | null;
  trade_discount: string | null;
  order_value: string | null;
  invoice_total: string | null;
  weight: string | null;
  shipping_options: { name: string | null; carrier_code: string | null; date: string | null; cost: number | null }[];
};
type Quote = { server: string; mode: "live" | "test"; totals: Totals };
type Placed = {
  server: string;
  mode: "live" | "test";
  placed: boolean;
  saved: boolean;
  order_number: string;
  message: string | null;
  po: string | null;
  invoice_amount: string | null;
  lines: { number: string; description: string; ordered: string; shipped: string; backordered: string; price: string; amount: string; warehouse: string | null }[];
  totals: Totals;
  related: { order_number: string; po: string; status: string; carrier: string; pro_number: string; invoice_number: string; invoice_amount: string }[];
};
type PastOrder = { id: number; mode: string; po_number: string; order_number: string; message: string | null; invoice_amount: string | null; created_at: string; created_by: string | null };
type Status = { configured: boolean; server: string; mode: "live" | "test" };

const money = (v: string | number | null | undefined) => (v == null || v === "" || isNaN(Number(v)) ? "—" : `$${Number(v).toFixed(2)}`);
const emptyAddr = { name: "", contact: "", addressLine1: "", addressLine2: "", city: "", state: "", zipCode: "", countryCode: "US", phone: "", classification: "" };

export default function AutodistOrders({ status }: { status: Status | null }) {
  const [po, setPo] = useState("");
  const [comment, setComment] = useState("");
  const [addr, setAddr] = useState({ ...emptyAddr });
  const [lines, setLines] = useState([{ number: "", quantity: "1" }]);
  const [method, setMethod] = useState("");
  const [carrier, setCarrier] = useState("");
  const [acct, setAcct] = useState("");
  const [promo, setPromo] = useState("");
  const [signature, setSignature] = useState(false);
  const [backorder, setBackorder] = useState(false);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [quote, setQuote] = useState<{ q: Quote; key: string } | null>(null);
  const [placed, setPlaced] = useState<Placed | null>(null);
  const [past, setPast] = useState<PastOrder[] | null>(null);
  const [tableMissing, setTableMissing] = useState(false);

  const body = (confirm: boolean) => ({
    po_number: po.trim(),
    comment: comment.trim(),
    shipping_method: method.trim(),
    ship_to: addr,
    items: lines.filter((l) => l.number.trim()).map((l) => ({ number: l.number.trim(), quantity: Math.max(1, parseInt(l.quantity, 10) || 1) })),
    carrier: carrier.trim(),
    carrier_account: acct.trim(),
    promo_code: promo.trim(),
    signature_required: signature,
    backorder,
    confirm,
  });
  // the quote only goes stale when something that can change the price changes (not the PO number, comment or shipping method)
  const keyNow = JSON.stringify({ ...body(false), po_number: "", comment: "", shipping_method: "" });
  const fresh = quote != null && quote.key === keyNow; // the quote still matches what is on screen

  async function loadPast() {
    try {
      const r = await api<{ table_missing: boolean; orders: PastOrder[] }>("/autodist/api/orders");
      setPast(r.orders);
      setTableMissing(r.table_missing);
    } catch {
      setPast([]);
    }
  }
  useEffect(() => {
    void loadPast();
  }, []);

  async function getQuote() {
    setBusy("quote");
    setErr(null);
    setPlaced(null);
    try {
      const q = await api<Quote>("/autodist/api/order/quote", { method: "POST", body: body(false) });
      setQuote({ q, key: keyNow });
    } catch (e) {
      setQuote(null);
      setErr(errMsg(e));
    } finally {
      setBusy("");
    }
  }

  async function place() {
    if (!quote || !fresh) return;
    const live = quote.q.mode === "live";
    const total = money(quote.q.totals.invoice_total ?? quote.q.totals.order_value);
    if (live) {
      const typed = window.prompt(`This places a REAL order with Automatic Distributors (about ${total}).\nType the PO number (${po.trim()}) to confirm:`);
      if (typed == null || typed.trim() !== po.trim()) return;
    } else if (!window.confirm(`Place this order on the TEST server (about ${total})?`)) {
      return;
    }
    setBusy("place");
    setErr(null);
    try {
      const r = await api<Placed>("/autodist/api/order/place", { method: "POST", body: body(true) });
      setPlaced(r);
      setQuote(null);
      void loadPast();
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy("");
    }
  }

  const setA = (k: keyof typeof emptyAddr, v: string) => setAddr((a) => ({ ...a, [k]: v }));
  const field = (label: string, el: React.ReactNode) => (
    <label className="flex flex-col gap-0.5 text-xs text-gray-600">
      {label}
      {el}
    </label>
  );
  const t = quote?.q.totals;
  const ready = status?.configured && lines.some((l) => l.number.trim());

  return (
    <div className="space-y-4 text-sm">
      <div className="flex flex-wrap items-center gap-2 text-xs text-gray-600">
        {status && (
          <>
            <Badge tone={status.mode === "test" ? "amber" : "blue"}>{status.mode === "test" ? "TEST server" : "LIVE server"}</Badge>
            <span>{status.server}</span>
            {!status.configured && <span className="text-amber-700">The API key is not set (AUTOMATIC_DIST_API_KEY in Render).</span>}
          </>
        )}
      </div>
      {status?.mode === "live" && (
        <Notice kind="info">This is the live server. Placing a real order is switched off unless AUTOMATIC_DIST_ALLOW_LIVE_ORDERS=yes is set in Render. Practice on the test server first.</Notice>
      )}

      <div className="grid gap-4 rounded border border-gray-200 bg-white p-4 md:grid-cols-2">
        <div className="space-y-2">
          <div className="font-medium">Order</div>
          {field("PO number (required to place)", <input className={inputCls} value={po} onChange={(e) => setPo(e.target.value)} />)}
          {field("Comment", <input className={inputCls} value={comment} onChange={(e) => setComment(e.target.value)} />)}
          <div className="space-y-1">
            <div className="text-xs text-gray-600">Parts</div>
            {lines.map((l, i) => (
              <div key={i} className="flex gap-2">
                <input className={inputCls} placeholder="Part number" value={l.number} onChange={(e) => setLines((ls) => ls.map((x, j) => (j === i ? { ...x, number: e.target.value } : x)))} />
                <input className={`${inputCls} !w-20`} inputMode="numeric" value={l.quantity} onChange={(e) => setLines((ls) => ls.map((x, j) => (j === i ? { ...x, quantity: e.target.value } : x)))} />
                {lines.length > 1 && (
                  <button type="button" className={btn2Cls} onClick={() => setLines((ls) => ls.filter((_, j) => j !== i))}>
                    Remove
                  </button>
                )}
              </div>
            ))}
            <button type="button" className={btn2Cls} onClick={() => setLines((ls) => [...ls, { number: "", quantity: "1" }])}>
              Add part
            </button>
          </div>
          <div className="flex flex-wrap gap-4 pt-1 text-xs">
            <label className="flex items-center gap-1">
              <input type="checkbox" checked={signature} onChange={(e) => setSignature(e.target.checked)} /> Signature required
            </label>
            <label className="flex items-center gap-1">
              <input type="checkbox" checked={backorder} onChange={(e) => setBackorder(e.target.checked)} /> Place on backorder if short
            </label>
          </div>
          {field("Promo code", <input className={`${inputCls} !w-48`} value={promo} onChange={(e) => setPromo(e.target.value)} />)}
        </div>

        <div className="space-y-2">
          <div className="font-medium">Ship to</div>
          {field("Name", <input className={inputCls} value={addr.name} onChange={(e) => setA("name", e.target.value)} />)}
          {field("Contact", <input className={inputCls} value={addr.contact} onChange={(e) => setA("contact", e.target.value)} />)}
          {field("Street address", <input className={inputCls} value={addr.addressLine1} onChange={(e) => setA("addressLine1", e.target.value)} />)}
          {field("Address line 2", <input className={inputCls} value={addr.addressLine2} onChange={(e) => setA("addressLine2", e.target.value)} />)}
          <div className="grid grid-cols-3 gap-2">
            {field("City", <input className={inputCls} value={addr.city} onChange={(e) => setA("city", e.target.value)} />)}
            {field("State", <input className={inputCls} value={addr.state} onChange={(e) => setA("state", e.target.value)} />)}
            {field("ZIP", <input className={inputCls} value={addr.zipCode} onChange={(e) => setA("zipCode", e.target.value)} />)}
          </div>
          <div className="grid grid-cols-3 gap-2">
            {field("Country", <input className={inputCls} value={addr.countryCode} onChange={(e) => setA("countryCode", e.target.value)} />)}
            {field("Phone", <input className={inputCls} value={addr.phone} onChange={(e) => setA("phone", e.target.value)} />)}
            {field("Type (if known)", <input className={inputCls} placeholder="residential / commercial" value={addr.classification} onChange={(e) => setA("classification", e.target.value)} />)}
          </div>
          <div className="font-medium pt-1">Shipping</div>
          {field("Shipping method (pick one from the quote, or type it)", <input className={inputCls} value={method} onChange={(e) => setMethod(e.target.value)} />)}
          <div className="grid grid-cols-2 gap-2">
            {field(
              "Bill to our own carrier account (optional)",
              <select className={inputCls} value={carrier} onChange={(e) => setCarrier(e.target.value)}>
                <option value="">Automatic Distributors bills shipping</option>
                <option value="UPS">UPS</option>
                <option value="FEDEX">FedEx</option>
              </select>
            )}
            {field("Account number", <input className={inputCls} value={acct} disabled={!carrier} onChange={(e) => setAcct(e.target.value)} />)}
          </div>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <button type="button" className={btnCls} disabled={busy !== "" || !ready} onClick={() => void getQuote()}>
          {busy === "quote" ? "Getting quote…" : "Get quote"}
        </button>
        <button type="button" className={btnCls} disabled={busy !== "" || !fresh || !po.trim()} onClick={() => void place()}>
          {busy === "place" ? "Placing…" : "Place order"}
        </button>
        {quote && !fresh && <span className="text-xs text-amber-700">You changed the order after the quote. Get a new quote before placing.</span>}
        {quote && fresh && !po.trim() && <span className="text-xs text-amber-700">Enter a PO number to place.</span>}
      </div>

      {err && <Notice kind="error">{err}</Notice>}

      {t && quote && (
        <div className="space-y-2 rounded border border-gray-200 bg-gray-50 p-3 text-xs">
          <div className="font-medium">Quote ({quote.q.mode} server, nothing placed)</div>
          {t.message && <div className="text-amber-700">{t.message}</div>}
          <table className="text-left">
            <tbody>
              {[
                ["Parts", t.sales_amount],
                ["Special charges", t.special_charge],
                ["Container charges", t.container_charge],
                ["Excise", t.excise],
                ["Trade discount", t.trade_discount],
                ["Tax", t.sales_tax],
                ["Invoice total", t.invoice_total ?? t.order_value],
              ].map(([k, v]) => (
                <tr key={k as string}>
                  <td className="py-0.5 pr-6 text-gray-500">{k}</td>
                  <td className="py-0.5 text-right">{money(v as string | null)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {t.shipping_options.length > 0 && (
            <div className="space-y-1">
              <div className="text-gray-500">Shipping options (click to use)</div>
              {t.shipping_options.map((o, i) => (
                <button
                  key={i}
                  type="button"
                  className="mr-2 rounded border border-gray-300 bg-white px-2 py-1 hover:bg-gray-100"
                  onClick={() => setMethod(o.carrier_code || o.name || "")}
                >
                  {o.name || o.carrier_code} · {money(o.cost)}
                  {o.date ? ` · ${new Date(o.date).toLocaleDateString()}` : ""}
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {placed && (
        <div className="space-y-2 rounded border border-gray-200 bg-white p-3 text-xs">
          <div className="flex flex-wrap items-center gap-2">
            {placed.placed ? <Badge tone="green">order placed</Badge> : <Badge tone="amber">no order number returned</Badge>}
            <Badge tone={placed.mode === "test" ? "amber" : "blue"}>{placed.mode} server</Badge>
            {placed.order_number && <b>Order {placed.order_number}</b>}
            <span>PO {placed.po || po}</span>
            <span>{money(placed.invoice_amount)}</span>
          </div>
          {placed.message && <div>{placed.message}</div>}
          {!placed.saved && <Notice kind="error">The order went through but could not be saved here. Write down order number {placed.order_number}.</Notice>}
          <table className="w-full text-left">
            <thead className="text-gray-500">
              <tr>
                <th className="py-0.5 pr-3">Part</th>
                <th className="py-0.5 pr-3">Ordered</th>
                <th className="py-0.5 pr-3">Shipping</th>
                <th className="py-0.5 pr-3">Backordered</th>
                <th className="py-0.5 pr-3">Warehouse</th>
                <th className="py-0.5 text-right">Amount</th>
              </tr>
            </thead>
            <tbody>
              {placed.lines.map((l, i) => (
                <tr key={i}>
                  <td className="py-0.5 pr-3">{l.number}</td>
                  <td className="py-0.5 pr-3">{l.ordered}</td>
                  <td className="py-0.5 pr-3">{l.shipped}</td>
                  <td className="py-0.5 pr-3">{l.backordered}</td>
                  <td className="py-0.5 pr-3">{l.warehouse || "—"}</td>
                  <td className="py-0.5 text-right">{money(l.amount)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {placed.related.length > 0 && (
            <div>
              Related orders:{" "}
              {placed.related.map((r) => `${r.order_number} (${r.status || "?"}${r.pro_number ? `, tracking ${r.pro_number}` : ""})`).join("; ")}
            </div>
          )}
        </div>
      )}

      <div className="space-y-1">
        <div className="font-medium">Orders placed from here</div>
        {tableMissing && <Notice kind="info">Run sql/autodist_orders.sql in Supabase before placing orders; it keeps the list below.</Notice>}
        {past && past.length === 0 && !tableMissing && <div className="text-xs text-gray-500">None yet.</div>}
        {past && past.length > 0 && (
          <table className="w-full text-left text-xs">
            <thead className="text-gray-500">
              <tr>
                <th className="py-0.5 pr-3">When</th>
                <th className="py-0.5 pr-3">Server</th>
                <th className="py-0.5 pr-3">PO</th>
                <th className="py-0.5 pr-3">Order</th>
                <th className="py-0.5 pr-3">Total</th>
                <th className="py-0.5">Message</th>
              </tr>
            </thead>
            <tbody>
              {past.map((o) => (
                <tr key={o.id} className="border-t border-gray-100">
                  <td className="py-0.5 pr-3">{new Date(o.created_at).toLocaleString()}</td>
                  <td className="py-0.5 pr-3">{o.mode}</td>
                  <td className="py-0.5 pr-3">{o.po_number}</td>
                  <td className="py-0.5 pr-3">{o.order_number || "—"}</td>
                  <td className="py-0.5 pr-3">{money(o.invoice_amount)}</td>
                  <td className="py-0.5">{o.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
