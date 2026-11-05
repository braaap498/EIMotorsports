"use client";

import { useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Notice, btnCls, btn2Cls, inputCls } from "../ui";

type Line = {
  product_id: number | null;
  sku: string | null;
  title: string | null;
  quantity: number;
  on_hand: number;
  available: number;
  short: boolean;
  matched: boolean;
};
type Lookup = {
  order: string;
  store: string;
  status: string | null;
  blocked: boolean;
  existing_po: number | null;
  warnings: string[];
  lines: Line[];
};

/**
 * Drop ship an eBay order: look the order up, tick the parts Automatic Distributors should send, and make the PO.
 * The buyer's address is fetched from eBay only when the PO is made, and is stored on that PO only.
 * Staff decide per order: leave this alone to ship from the shelf instead.
 */
export default function EbayDropShip({ initialOrder = "" }: { initialOrder?: string }) {
  const [open, setOpen] = useState(false);
  const [order, setOrder] = useState(initialOrder);
  const [info, setInfo] = useState<Lookup | null>(null);
  const [sel, setSel] = useState<Record<number, boolean>>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [made, setMade] = useState<{ id: number; po_number: number } | null>(null);

  const on = (l: Line) => l.product_id != null && (sel[l.product_id] ?? l.short);

  async function lookup() {
    setBusy(true);
    setErr(null);
    setMade(null);
    setInfo(null);
    setSel({});
    try {
      setInfo(await api<Lookup>(`/purchase-orders/ebay-order?order=${encodeURIComponent(order.trim())}`));
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  async function make() {
    if (!info) return;
    const ids = info.lines.filter(on).map((l) => l.product_id as number);
    if (ids.length === 0) {
      setErr("Tick at least one part to drop ship.");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const po = await api<{ id: number; po_number: number }>("/purchase-orders/from-ebay-order", {
        method: "POST",
        body: { order: info.order, product_ids: ids },
      });
      setMade(po);
    } catch (e) {
      setErr(errMsg(e)); // includes the Global Shipping Program warning
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button className="text-sm underline" onClick={() => setOpen(true)}>
        Drop ship this eBay order instead (order it from Automatic Distributors)
      </button>
    );
  }

  return (
    <div className="space-y-3 rounded-lg border border-blue-200 bg-blue-50 p-3 text-sm">
      <div className="flex items-center justify-between">
        <div className="font-medium">Drop ship an eBay order</div>
        <button className="text-xs underline" onClick={() => setOpen(false)}>
          close
        </button>
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <label className="text-sm">
          <span className="block text-gray-700">eBay order number</span>
          <input className={`${inputCls} w-64`} value={order} onChange={(e) => setOrder(e.target.value)} maxLength={40} />
        </label>
        <button className={btn2Cls} disabled={busy || order.trim().length < 3} onClick={() => void lookup()}>
          Look up
        </button>
      </div>

      {err && <Notice kind="error">{err}</Notice>}

      {info && (
        <div className="space-y-2">
          <div className="text-gray-700">
            Order <b>{info.order}</b> · store {info.store}
          </div>
          {info.warnings.map((w) => (
            <Notice key={w} kind="error">
              {w}
            </Notice>
          ))}
          <div className="rounded border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900">
            Orders sent through eBay&rsquo;s Global Shipping Program are refused when you press the button below: eBay
            gives a hub address for those, not the buyer&rsquo;s. Ship those from stock.
          </div>
          <ul className="space-y-1">
            {info.lines.map((l, idx) => (
              <li key={idx}>
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    disabled={!l.matched}
                    checked={on(l)}
                    onChange={(e) => l.product_id != null && setSel((d) => ({ ...d, [l.product_id as number]: e.target.checked }))}
                  />
                  <span>
                    {l.sku ?? l.title ?? "unknown part"} × {l.quantity}
                    {l.matched ? <span className="text-gray-500"> · shelf count {l.on_hand}</span> : null}
                    {!l.matched && <span className="text-red-700"> · listing not linked to a product</span>}
                  </span>
                </label>
              </li>
            ))}
          </ul>
          {info.existing_po != null ? (
            <Notice kind="info">PO {info.existing_po} was already made for this order. Cancel it first to make another.</Notice>
          ) : made ? (
            <div>
              Drop ship PO <b>{made.po_number}</b> was made. An owner or admin places it on{" "}
              <a className="underline" href={`/admin/distributors/autodist?po=${made.id}`}>
                Distributors &gt; Automatic Distributors &gt; Orders
              </a>
              , where the address and parts are checked before it is sent.
            </div>
          ) : (
            <button className={btnCls} disabled={busy || info.blocked} onClick={() => void make()}>
              Make drop ship PO (fetches the buyer&rsquo;s address from eBay)
            </button>
          )}
        </div>
      )}
    </div>
  );
}
