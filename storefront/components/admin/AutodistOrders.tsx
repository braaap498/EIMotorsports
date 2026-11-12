"use client";

import { useEffect, useRef, useState } from "react";
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
  po_updated?: boolean | null;
  order_number: string;
  message: string | null;
  po: string | null;
  invoice_amount: string | null;
  lines: { number: string; description: string; ordered: string; shipped: string; backordered: string; price: string; amount: string; warehouse: string | null }[];
  totals: Totals;
  related: { order_number: string; po: string; status: string; carrier: string; pro_number: string; invoice_number: string; invoice_amount: string }[];
};
type TrackItem = { number: string; kind: "tracking" | "pro"; carrier: string | null; order_number: string | null; status: string | null };
type PastOrder = {
  id: number; mode: string; po_number: string; order_number: string; message: string | null; invoice_amount: string | null; created_at: string; created_by: string | null;
  status: string | null; tracking: TrackItem[]; tracking_checked_at: string | null;
};

// A link to the carrier's own tracking page when the number is clearly UPS or FedEx; otherwise the number alone.
function trackUrl(t: TrackItem): string | null {
  if (t.kind !== "tracking") return null;
  const n = t.number.replace(/\s/g, "");
  const c = (t.carrier ?? "").toUpperCase();
  if (/^1Z[0-9A-Z]{16}$/i.test(n) || c.includes("UPS")) return `https://www.ups.com/track?tracknum=${encodeURIComponent(n)}`;
  if (c.includes("FED") || c.includes("FDX")) return `https://www.fedex.com/fedextrack/?trknbr=${encodeURIComponent(n)}`;
  return null;
}
type Status = { configured: boolean; server: string; mode: "live" | "test" };
type PoRow = { id: number; po_number: number; vendor_name: string; sent_at: string | null; po_type: "stock" | "drop_ship"; open_lines: number };
type Cand = { sku: string; manufacturer: string | null; name: string | null; stock: number | null; cost: string | number | null };
type PoFull = {
  id: number;
  po_number: number;
  po_type: "stock" | "drop_ship";
  note: string | null;
  source?: string | null;
  sent_at: string | null;
  ship_to_text: string | null;
  ship_to: Record<string, string>;
  lines: { line_id: number; item: string | null; description: string | null; quantity: number; rate: number | null; number: string; candidates: Cand[] }[];
};

const money = (v: string | number | null | undefined) => (v == null || v === "" || isNaN(Number(v)) ? "—" : `$${Number(v).toFixed(2)}`);
const emptyAddr = { name: "", contact: "", addressLine1: "", addressLine2: "", city: "", state: "", zipCode: "", countryCode: "US", phone: "", classification: "" };

export default function AutodistOrders({ status, openPo }: { status: Status | null; openPo?: number | null }) {
  const [po, setPo] = useState("");
  const [comment, setComment] = useState("");
  const [addr, setAddr] = useState({ ...emptyAddr });
  const [lines, setLines] = useState<{ number: string; quantity: string; label?: string; cands?: Cand[] }[]>([{ number: "", quantity: "1" }]);
  const [pos, setPos] = useState<PoRow[] | null>(null);
  const [posError, setPosError] = useState("");
  const [poId, setPoId] = useState<number | "">("");
  const [poInfo, setPoInfo] = useState<PoFull | null>(null);
  const [chk, setChk] = useState({ addr: false, parts: false, total: false }); // the "I checked it" boxes; cleared whenever the order changes
  const openedPo = useRef(false);
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
  const [trackingMissing, setTrackingMissing] = useState(false);
  const [trackBusy, setTrackBusy] = useState<number | "all" | null>(null);
  const [trackMsg, setTrackMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);

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
    po_id: poId === "" ? null : poId,
    confirm,
  });
  // the quote only goes stale when something that can change the price changes (not the PO number, comment or shipping method)
  const keyNow = JSON.stringify({ ...body(false), po_number: "", comment: "", shipping_method: "" });
  const fresh = quote != null && quote.key === keyNow; // the quote still matches what is on screen

  async function loadPast() {
    try {
      const r = await api<{ table_missing: boolean; tracking_missing?: boolean; orders: PastOrder[] }>("/autodist/api/orders");
      setPast(r.orders);
      setTableMissing(r.table_missing);
      setTrackingMissing(!!r.tracking_missing);
    } catch {
      setPast([]);
    }
  }
  async function checkTracking(id: number | "all") {
    setTrackBusy(id);
    setTrackMsg(null);
    try {
      if (id === "all") {
        const r = await api<{ checked: number; with_tracking: number; problems: string[]; closed_pos?: number[]; ebay_filled?: string[] }>("/autodist/api/orders/refresh-tracking", { method: "POST" });
        const closed = r.closed_pos && r.closed_pos.length ? ` Drop ship PO ${r.closed_pos.join(", ")} closed out.` : "";
        const ebay = r.ebay_filled && r.ebay_filled.length ? ` Tracking added to eBay order${r.ebay_filled.length === 1 ? "" : "s"} ${r.ebay_filled.join(", ")}: open Pick and Ship and press Mark shipped.` : "";
        const base = `Checked ${r.checked} order${r.checked === 1 ? "" : "s"}; ${r.with_tracking} have tracking.${closed}${ebay}`;
        setTrackMsg(r.problems.length ? { kind: "error", text: `${base} Problems: ${r.problems.join(" | ")}` } : { kind: "success", text: base });
      } else {
        const r = await api<{ tracking: TrackItem[]; po_closed?: number | null; ebay_filled?: string | null }>(`/autodist/api/orders/${id}/tracking`, { method: "POST" });
        setTrackMsg({
          kind: "success",
          text: (r.tracking.length ? "Tracking updated." : "Checked: no tracking numbers yet.") + (r.po_closed ? ` Drop ship PO ${r.po_closed} closed out.` : "") + (r.ebay_filled ? ` Tracking added to eBay order ${r.ebay_filled}: press Mark shipped in Pick and Ship.` : ""),
        });
      }
      await loadPast();
    } catch (e) {
      setTrackMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setTrackBusy(null);
    }
  }
  async function cancelOrder(o: PastOrder) {
    if (
      !window.confirm(
        `Cancel order ${o.order_number} (PO ${o.po_number}) with Automatic Distributors?\n\nThis stops it from shipping and can't be undone. It only works while the order has not been invoiced. If you place it again later, it needs a new PO number.`
      )
    )
      return;
    setTrackBusy(o.id);
    setTrackMsg(null);
    try {
      const r = await api<{
        order_number: string; already_cancelled: boolean; cancelled: string[]; failed: string[];
        po_number: string; po_id: number | null; ebay_order_id: string | null;
      }>(`/autodist/api/orders/${o.id}/cancel`, { method: "POST", body: { confirm: true } });
      let text = r.already_cancelled ? `Order ${r.order_number} was already cancelled.` : `Order ${r.cancelled.join(", ")} cancelled with Automatic Distributors.`;
      if (r.failed.length) text += ` These could not be cancelled: ${r.failed.join(" | ")}`;
      if (r.po_id && window.confirm(`Also cancel purchase order ${r.po_number}, so it leaves the open lists?`)) {
        try {
          await api(`/purchase-orders/${r.po_id}/cancel`, { method: "POST", body: { reason: "Cancelled with Automatic Distributors" } });
          text += ` Purchase order ${r.po_number} cancelled.`;
        } catch (e) {
          text += ` The purchase order was NOT cancelled (${errMsg(e)}); cancel it in PO history.`;
        }
      } else if (r.po_id) {
        text += ` Purchase order ${r.po_number} is still open.`;
      }
      if (r.ebay_order_id) text += ` The customer's eBay order ${r.ebay_order_id} is still open: ship it from stock, order it another way, or cancel it on eBay.`;
      setTrackMsg({ kind: r.failed.length ? "error" : "success", text });
      await loadPast();
    } catch (e) {
      setTrackMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setTrackBusy(null);
    }
  }
  async function loadPos() {
    try {
      setPosError("");
      setPos((await api<{ items: PoRow[] }>("/autodist/api/pos")).items);
    } catch (e) {
      setPos([]);
      setPosError(e instanceof Error ? e.message : "Could not load the purchase order list.");
    }
  }
  useEffect(() => {
    void loadPast();
    void loadPos();
  }, []);

  useEffect(() => {
    if (openPo != null && pos != null && !openedPo.current) {
      openedPo.current = true;
      void pickPo(String(openPo));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openPo, pos]);

  async function pickPo(v: string) {
    setQuote(null);
    setPlaced(null);
    setErr(null);
    if (!v) {
      setPoId("");
      setPoInfo(null);
      setPo("");
      setComment("");
      setAddr({ ...emptyAddr });
      setLines([{ number: "", quantity: "1" }]);
      return;
    }
    setBusy("po");
    try {
      const p = await api<PoFull>(`/autodist/api/pos/${v}`);
      setPoId(p.id);
      setPoInfo(p);
      setPo(String(p.po_number));
      setComment(p.note ?? "");
      setAddr({ ...emptyAddr, ...p.ship_to });
      setMethod("");
      setLines(p.lines.map((l) => ({ number: l.number, quantity: String(l.quantity), label: l.item || l.description || "", cands: l.candidates })));
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy("");
    }
  }

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
      void loadPos();
      setPoId("");
      setPoInfo(null);
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
  useEffect(() => {
    setChk({ addr: false, parts: false, total: false });
  }, [keyNow, poId]);
  const reviewed = chk.addr && chk.parts && chk.total;
  const sourceLabel = poInfo?.source?.startsWith("web:") ? `Website order ${poInfo.source.slice(4)}` : poInfo?.source?.startsWith("ebay:") ? `eBay order ${poInfo.source.slice(5)}` : null;
  const need: [string, string][] = [["name", "name"], ["addressLine1", "street"], ["city", "city"], ["state", "state"], ["zipCode", "ZIP"]];
  const addrMissing = need.filter(([k]) => !(addr as Record<string, string>)[k]?.trim()).map(([, l]) => l);
  const poCost = poInfo ? poInfo.lines.reduce((n, l) => n + (l.rate ?? 0) * l.quantity, 0) : null;
  const t = quote?.q.totals;
  const unmatched = poInfo ? lines.filter((l) => !l.number.trim()).length : 0; // PO lines with no Automatic Distributors part number would be left off the order
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

      <div className="space-y-2 rounded border border-gray-200 bg-white p-4">
        <label className="flex flex-col gap-0.5 text-xs text-gray-600">
          Start from a purchase order to Automatic Distributors
          <select className={`${inputCls} !w-auto min-w-[22rem]`} value={poId} disabled={busy !== ""} onChange={(e) => void pickPo(e.target.value)}>
            <option value="">{pos && pos.length === 0 ? "No open purchase orders to Automatic Distributors" : "Or type an order by hand…"}</option>
            {(pos ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                PO {p.po_number} · {p.po_type === "drop_ship" ? "Drop ship" : "Stock"} · {p.open_lines} line{p.open_lines === 1 ? "" : "s"}
                {p.sent_at ? " · marked sent" : ""}
              </option>
            ))}
          </select>
          <button type="button" className="self-start text-xs text-blue-700 underline" onClick={() => void loadPos()}>Refresh list</button>
          {posError && <span className="text-red-700">Could not load the list: {posError}</span>}
        </label>
        {poInfo && (
          <div className="flex flex-wrap items-center gap-2 text-xs text-gray-600">
            <Badge tone={poInfo.po_type === "drop_ship" ? "blue" : "gray"}>{poInfo.po_type === "drop_ship" ? "Drop ship: ships to the customer" : "Stock: ships to us"}</Badge>
            {poInfo.sent_at && <span className="text-amber-700">This PO is already marked sent ({new Date(poInfo.sent_at).toLocaleDateString()}). Check it was not ordered another way.</span>}
            {poInfo.ship_to_text && !addr.addressLine1 && <span className="text-amber-700">The PO’s ship-to could not be split into fields. Fill them in on the right.</span>}
          </div>
        )}
      </div>

      <div className="grid gap-4 rounded border border-gray-200 bg-white p-4 md:grid-cols-2">
        <div className="space-y-2">
          <div className="font-medium">Order</div>
          {field("PO number (required to place)", <input className={inputCls} value={po} disabled={poId !== ""} onChange={(e) => setPo(e.target.value)} />)}
          {field("Comment", <input className={inputCls} value={comment} onChange={(e) => setComment(e.target.value)} />)}
          <div className="space-y-1">
            <div className="text-xs text-gray-600">Parts</div>
            {lines.map((l, i) => (
              <div key={i} className="space-y-0.5">
              <div className="flex gap-2">
                <input className={inputCls} placeholder="Part number" value={l.number} onChange={(e) => setLines((ls) => ls.map((x, j) => (j === i ? { ...x, number: e.target.value } : x)))} />
                <input className={`${inputCls} !w-20`} inputMode="numeric" value={l.quantity} onChange={(e) => setLines((ls) => ls.map((x, j) => (j === i ? { ...x, quantity: e.target.value } : x)))} />
                {lines.length > 1 && (
                  <button type="button" className={btn2Cls} onClick={() => setLines((ls) => ls.filter((_, j) => j !== i))}>
                    Remove
                  </button>
                )}
              </div>
              {l.label && <div className="text-xs text-gray-500">On the PO: {l.label}</div>}
              {l.cands && l.cands.length > 0 && !l.number.trim() && (
                <div className="flex flex-wrap items-center gap-1 text-xs">
                  <span className="text-amber-700">More than one match, pick one:</span>
                  {l.cands.map((c) => (
                    <button
                      key={c.sku}
                      type="button"
                      className="rounded border border-gray-300 bg-white px-2 py-0.5 hover:bg-gray-100"
                      onClick={() => setLines((ls) => ls.map((x, j) => (j === i ? { ...x, number: c.sku } : x)))}
                    >
                      {c.sku}
                      {c.manufacturer ? ` · ${c.manufacturer}` : ""} · {c.stock ?? 0} in stock
                    </button>
                  ))}
                </div>
              )}
              {l.cands && l.cands.length === 0 && !l.number.trim() && <div className="text-xs text-amber-700">No Automatic Distributors part matched this line. Type the part number.</div>}
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
        <button type="button" className={btnCls} disabled={busy !== "" || !fresh || !po.trim() || unmatched > 0 || !reviewed} onClick={() => void place()}>
          {busy === "place" ? "Placing…" : "Place order"}
        </button>
        {quote && !fresh && <span className="text-xs text-amber-700">You changed the order after the quote. Get a new quote before placing.</span>}
        {quote && fresh && !po.trim() && <span className="text-xs text-amber-700">Enter a PO number to place.</span>}
        {quote && fresh && po.trim() && unmatched === 0 && !reviewed && <span className="text-xs text-amber-700">Tick the three checks under the quote to place.</span>}
        {unmatched > 0 && (
          <span className="text-xs text-amber-700">
            {unmatched} PO line{unmatched === 1 ? " has" : "s have"} no Automatic Distributors part number. Pick or type one, or fix the PO, before placing.
          </span>
        )}
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

      {quote && fresh && t && (
        <div className="space-y-2 rounded border-2 border-blue-200 bg-white p-3 text-xs">
          <div className="flex flex-wrap items-center gap-2 font-medium">
            Check before placing
            {poInfo && <Badge tone={poInfo.po_type === "drop_ship" ? "blue" : "gray"}>{poInfo.po_type === "drop_ship" ? "Drop ship" : "Stock"}</Badge>}
            {sourceLabel && <span className="font-normal text-gray-600">for {sourceLabel}</span>}
            <span className="font-normal text-gray-600">· PO {po || "(none)"}</span>
          </div>
          <div className="grid gap-3 md:grid-cols-2">
            <div className="space-y-0.5">
              <div className="text-gray-500">Ships to</div>
              <div>{addr.name}</div>
              {addr.contact && <div>Attn: {addr.contact}</div>}
              <div>{[addr.addressLine1, addr.addressLine2].filter(Boolean).join(", ")}</div>
              <div>
                {addr.city}, {addr.state} {addr.zipCode} {addr.countryCode}
              </div>
              {addr.phone && <div>{addr.phone}</div>}
              {method && <div className="text-gray-600">Shipping: {method}</div>}
              {addrMissing.length > 0 && <div className="text-red-700">Missing: {addrMissing.join(", ")}</div>}
            </div>
            <div>
              <table className="w-full text-left">
                <thead className="text-gray-500">
                  <tr>
                    <th className="py-0.5 pr-3">Part</th>
                    <th className="py-0.5 pr-3">Qty</th>
                    <th className="py-0.5">In stock</th>
                  </tr>
                </thead>
                <tbody>
                  {lines
                    .filter((l) => l.number.trim())
                    .map((l, i) => {
                      const q = Math.max(1, parseInt(l.quantity, 10) || 1);
                      const c = l.cands?.find((x) => x.sku === l.number.trim());
                      const low = c != null && (c.stock ?? 0) < q;
                      return (
                        <tr key={i}>
                          <td className="py-0.5 pr-3">
                            {l.number}
                            {l.label ? <span className="text-gray-500"> · {l.label}</span> : null}
                          </td>
                          <td className="py-0.5 pr-3">{q}</td>
                          <td className={`py-0.5 ${low ? "font-medium text-red-700" : ""}`}>{c ? `${c.stock ?? 0}${low ? " (not enough)" : ""}` : "—"}</td>
                        </tr>
                      );
                    })}
                </tbody>
              </table>
              <div className="pt-1 text-gray-600">
                Quote parts total {money(t.sales_amount)}
                {poCost != null && ` · PO cost ${money(poCost)}`}
                {poCost != null && t.sales_amount != null && Math.abs(Number(t.sales_amount) - poCost) > Math.max(1, poCost * 0.02) && (
                  <span className="text-amber-700"> · these differ, check the prices</span>
                )}
              </div>
            </div>
          </div>
          <div className="flex flex-wrap gap-4 pt-1">
            <label className="flex items-center gap-1">
              <input type="checkbox" checked={chk.addr} disabled={addrMissing.length > 0} onChange={(e) => setChk((c) => ({ ...c, addr: e.target.checked }))} /> Address is correct
            </label>
            <label className="flex items-center gap-1">
              <input type="checkbox" checked={chk.parts} onChange={(e) => setChk((c) => ({ ...c, parts: e.target.checked }))} /> Parts and quantities are correct
            </label>
            <label className="flex items-center gap-1">
              <input type="checkbox" checked={chk.total} onChange={(e) => setChk((c) => ({ ...c, total: e.target.checked }))} /> Prices and shipping are OK
            </label>
          </div>
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
          {placed.po_updated === true && <div className="text-green-700">The purchase order is marked sent and now shows this order number.</div>}
          {placed.po_updated === false && <Notice kind="error">The order went through, but the purchase order could not be updated. Mark PO {placed.po || po} sent by hand and note order {placed.order_number}.</Notice>}
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
        <div className="flex flex-wrap items-center gap-3">
          <div className="font-medium">Orders placed from here</div>
          {past && past.length > 0 && !trackingMissing && (
            <button type="button" className="text-xs underline" disabled={trackBusy !== null} onClick={() => void checkTracking("all")}>
              {trackBusy === "all" ? "Checking…" : "Check tracking for recent orders"}
            </button>
          )}
        </div>
        {trackingMissing && !tableMissing && <Notice kind="info">Run sql/autodist_tracking.sql in Supabase to turn on status and tracking numbers.</Notice>}
        {trackMsg && <Notice kind={trackMsg.kind}>{trackMsg.text}</Notice>}
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
                <th className="py-0.5 pr-3">Status</th>
                <th className="py-0.5 pr-3">Tracking</th>
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
                  <td className="py-0.5 pr-3">
                    {o.status === "9" || /cancel/i.test(o.status ?? "") ? <span className="font-medium text-red-700">Cancelled</span> : o.status || "—"}
                    {o.order_number && o.status !== "9" && !/cancel/i.test(o.status ?? "") && o.tracking.length === 0 && (
                      <button type="button" className="ml-2 text-red-700 underline" disabled={trackBusy !== null} onClick={() => void cancelOrder(o)}>
                        Cancel
                      </button>
                    )}
                  </td>
                  <td className="py-0.5 pr-3">
                    {o.tracking.map((t) => {
                      const url = trackUrl(t);
                      return (
                        <div key={`${t.kind}-${t.number}`} className="font-mono">
                          {url ? <a className="underline" href={url} target="_blank" rel="noreferrer">{t.number}</a> : t.number}
                          {t.kind === "pro" && <span className="ml-1 font-sans text-gray-500">(freight PRO)</span>}
                          {t.carrier && <span className="ml-1 font-sans text-gray-500">{t.carrier}</span>}
                        </div>
                      );
                    })}
                    {o.order_number && !trackingMissing && (
                      <button type="button" className="underline" disabled={trackBusy !== null} onClick={() => void checkTracking(o.id)}>
                        {trackBusy === o.id ? "Checking…" : o.tracking_checked_at ? "Recheck" : o.tracking.length ? "" : "Check"}
                      </button>
                    )}
                    {o.tracking_checked_at && o.tracking.length === 0 && <span className="ml-1 text-gray-500">none yet</span>}
                  </td>
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
