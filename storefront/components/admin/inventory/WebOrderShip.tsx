"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, errMsg } from "../../../lib/adminApi";
import { Badge, Notice, btnCls, btn2Cls, inputCls } from "../ui";
import ScanBox from "./ScanBox";
import { beep, lookupCode, newId } from "./shared";

type Item = {
  item_id: number;
  product_id: number | null;
  sku: string | null;
  title: string;
  bin_location: string | null;
  ordered: number;
  shipped_before: number;
  thumb: string | null;
};
type Order = {
  order_number: string;
  status: string;
  customer_name: string | null;
  shipped_at: string | null;
  shipping_method: string | null;
  ship_to: { name?: string; line1?: string; line2?: string; city?: string; region?: string; postal_code?: string };
  items: Item[];
};
type ToShip = { order_number: string; customer_name: string | null; paid_at: string | null; units: number };
type Flash = { kind: "success" | "error" | "info"; text: string };

/**
 * Scan out a website order. Open the order (scan or type its number, or click it in the list),
 * then scan every part. A wrong part or an extra unit is refused on the spot. When everything
 * is scanned, "Mark shipped" saves it and the order is complete.
 */
export default function WebOrderShip({ active }: { active: boolean }) {
  const scanRef = useRef<HTMLInputElement>(null);
  const [order, setOrder] = useState<Order | null>(null);
  const [scanned, setScanned] = useState<Record<number, number>>({});
  const [toShip, setToShip] = useState<ToShip[]>([]);
  const [batchId, setBatchId] = useState(newId);
  const [perScan, setPerScan] = useState(1);
  const [flash, setFlash] = useState<Flash | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  const focus = () => setTimeout(() => scanRef.current?.focus(), 0);
  useEffect(() => {
    if (active) scanRef.current?.focus();
  }, [active]);

  const loadList = useCallback(() => {
    api<{ items: ToShip[] }>("/inventory/orders/to-ship")
      .then((r) => setToShip(r.items))
      .catch((e) => setFlash({ kind: "error", text: errMsg(e) }));
  }, []);
  useEffect(loadList, [loadList]);

  const left = (i: Item) => (i.product_id == null ? 0 : i.ordered - i.shipped_before - (scanned[i.product_id] ?? 0));
  const scannable = order ? order.items.filter((i) => i.product_id != null) : [];
  const complete = !!order && order.items.length > 0 && order.items.every((i) => i.product_id != null && left(i) === 0);
  const scannedUnits = Object.values(scanned).reduce((a, b) => a + b, 0);
  const totalLeft = order ? order.items.reduce((n, i) => n + Math.max(0, left(i)), 0) : 0;

  async function openOrder(num: string) {
    setFlash(null);
    try {
      const o = await api<Order>(`/inventory/orders/${encodeURIComponent(num.trim())}`);
      if (o.shipped_at) {
        beep(false);
        setFlash({ kind: "error", text: `Order ${o.order_number} was already shipped.` });
        return;
      }
      if (o.status !== "paid") {
        beep(false);
        setFlash({ kind: "error", text: `Order ${o.order_number} is “${o.status}”, not paid, so it can’t be shipped.` });
        return;
      }
      setOrder(o);
      setScanned({});
      setBatchId(newId());
      setConfirming(false);
      beep(true);
    } catch (e) {
      beep(false);
      setFlash({ kind: "error", text: errMsg(e) });
    }
  }

  async function handleScan(code: string) {
    setFlash(null);
    setConfirming(false);
    if (!order) {
      await openOrder(code); // with no order open, the scan box takes the order number
      focus();
      return;
    }
    try {
      const r = await lookupCode(code);
      if (!r.found || !r.product) {
        beep(false);
        setFlash({ kind: "error", text: `Barcode ${code} isn’t linked to any part. Link it on the Barcodes tab first.` });
        return;
      }
      const item = order.items.find((i) => i.product_id === r.product!.id);
      if (!item) {
        beep(false);
        setFlash({ kind: "error", text: `WRONG PART — ${r.product.sku} (${r.product.title}) is not on this order.` });
        return;
      }
      const remaining = left(item);
      if (perScan > remaining) {
        beep(false);
        setFlash({
          kind: "error",
          text:
            remaining === 0
              ? `${item.sku} is already fully scanned — that would be an extra unit.`
              : `Only ${remaining} more of ${item.sku} needed on this order.`,
        });
        return;
      }
      setScanned((s) => ({ ...s, [item.product_id as number]: (s[item.product_id as number] ?? 0) + perScan }));
      setPerScan(1);
      beep(true);
      setFlash({ kind: "success", text: `+${perScan}  ${item.sku} — ${item.title}` });
    } catch (e) {
      beep(false);
      setFlash({ kind: "error", text: errMsg(e) });
    }
    focus();
  }

  async function commit() {
    if (!order) return;
    setBusy(true);
    setFlash(null);
    try {
      const r = await api<{ order_complete: boolean }>("/inventory/ship", {
        method: "POST",
        body: {
          batch_id: batchId,
          order_number: order.order_number,
          partial: !complete,
          lines: Object.entries(scanned)
            .filter(([, q]) => q > 0)
            .map(([pid, q]) => ({ product_id: Number(pid), quantity: q })),
        },
      });
      setFlash({
        kind: "success",
        text: r.order_complete
          ? `Order ${order.order_number} marked shipped.`
          : `Partial shipment saved for order ${order.order_number}. It stays open for the rest.`,
      });
      setOrder(null);
      setScanned({});
      setBatchId(newId());
      loadList();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && e.message.startsWith("Already saved")) {
        setFlash({ kind: "success", text: "That shipment was already saved the first time." });
        setOrder(null);
        setScanned({});
        setBatchId(newId());
        loadList();
      } else {
        setFlash({ kind: "error", text: errMsg(e) });
      }
    } finally {
      setConfirming(false);
      setBusy(false);
      focus();
    }
  }

  const a = order?.ship_to;
  return (
    <div className="space-y-4">
      <p className="text-sm text-gray-600">
        Scan or type the order number to open it, then scan every part going in the box. Stock was already reduced when the
        order was paid, so scanning checks the pick and does not remove stock a second time.
      </p>

      <div className="grid gap-3 md:grid-cols-[1fr_auto]">
        <ScanBox
          ref={scanRef}
          onScan={handleScan}
          placeholder={order ? "Scan a part…" : "Scan or type the order number…"}
        />
        {order && (
          <label className="text-sm font-medium text-gray-700">
            <span className="block">Quantity for next scan</span>
            <input
              type="number"
              min={1}
              className={`${inputCls} w-28`}
              value={perScan}
              onChange={(e) => setPerScan(Math.max(1, parseInt(e.target.value || "1", 10) || 1))}
            />
          </label>
        )}
      </div>

      {flash && <Notice kind={flash.kind}>{flash.text}</Notice>}

      {!order ? (
        <div className="space-y-2">
          <h3 className="text-sm font-semibold uppercase tracking-wide text-gray-500">Paid, waiting to ship</h3>
          {toShip.length === 0 ? (
            <p className="rounded border border-dashed border-gray-300 p-6 text-center text-sm text-gray-500">
              No website orders are waiting.
            </p>
          ) : (
            <ul className="divide-y divide-gray-100 rounded-lg border border-gray-200 bg-white">
              {toShip.map((o) => (
                <li key={o.order_number}>
                  <button
                    type="button"
                    onClick={() => openOrder(o.order_number)}
                    className="flex w-full items-center justify-between px-4 py-3 text-left text-sm hover:bg-gray-50"
                  >
                    <span>
                      <span className="font-semibold">{o.order_number}</span>
                      <span className="ml-2 text-gray-600">{o.customer_name}</span>
                    </span>
                    <span className="text-gray-500">
                      {o.units} unit{o.units === 1 ? "" : "s"}
                      {o.paid_at ? ` · paid ${new Date(o.paid_at).toLocaleDateString()}` : ""}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          <button className="text-sm underline" onClick={loadList}>
            Refresh list
          </button>
        </div>
      ) : (
        <div className="space-y-4">
          <div className="flex flex-wrap items-start justify-between gap-3 rounded-lg border border-gray-200 bg-white p-4">
            <div className="text-sm">
              <div className="text-lg font-semibold">Order {order.order_number}</div>
              <div>{a?.name ?? order.customer_name}</div>
              <div className="text-gray-600">
                {[a?.line1, a?.line2].filter(Boolean).join(", ")}
                {a?.city ? `, ${a.city}` : ""} {a?.region} {a?.postal_code}
              </div>
              {order.shipping_method && <div className="mt-1 text-gray-600">Ship via: {order.shipping_method}</div>}
            </div>
            <button
              className={btn2Cls}
              onClick={() => {
                setOrder(null);
                setScanned({});
                setFlash({ kind: "info", text: "Order closed. Nothing was saved." });
                focus();
              }}
            >
              Close order
            </button>
          </div>

          <ul className="divide-y divide-gray-100 rounded-lg border border-gray-200 bg-white">
            {order.items.map((i) => {
              const done = i.product_id != null && left(i) === 0;
              const got = i.product_id != null ? i.shipped_before + (scanned[i.product_id] ?? 0) : 0;
              return (
                <li key={i.item_id} className={`flex items-center gap-3 px-4 py-3 ${done ? "bg-green-50" : ""}`}>
                  {i.thumb ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img src={i.thumb} alt="" className="h-14 w-14 rounded border border-gray-200 object-contain" />
                  ) : (
                    <div className="h-14 w-14 rounded border border-gray-200 bg-gray-50" />
                  )}
                  <div className="min-w-0 flex-1 text-sm">
                    <div className="font-medium">{i.title}</div>
                    <div className="text-xs text-gray-500">
                      {i.sku ?? "no linked product"}
                      {i.bin_location ? ` · bin ${i.bin_location}` : ""}
                    </div>
                    {i.product_id == null && (
                      <div className="text-xs text-red-700">This line has no product attached, so it can’t be scanned.</div>
                    )}
                  </div>
                  <div className="text-right">
                    <div className="text-xl font-semibold">
                      {got} / {i.ordered}
                    </div>
                    {done ? <Badge tone="green">done</Badge> : <Badge tone="amber">{left(i)} to go</Badge>}
                  </div>
                </li>
              );
            })}
          </ul>

          <div className="flex flex-wrap items-center gap-3">
            {!confirming ? (
              <button className={btnCls} disabled={busy || (scannedUnits === 0 && !complete)} onClick={() => setConfirming(true)}>
                {complete ? "Mark shipped" : `Ship partial (${scannedUnits} scanned, ${totalLeft} not yet)`}
              </button>
            ) : (
              <>
                <span className="text-sm font-medium">
                  {complete
                    ? `Mark order ${order.order_number} as shipped?`
                    : `Only ${scannedUnits} of the units are scanned. Save a partial shipment and keep the order open?`}
                </span>
                <button className={btnCls} disabled={busy} onClick={commit}>
                  {busy ? "Saving…" : "Yes"}
                </button>
                <button className={btn2Cls} disabled={busy} onClick={() => setConfirming(false)}>
                  Go back
                </button>
              </>
            )}
            {scannable.length < order.items.length && (
              <span className="text-sm text-amber-700">Some lines can’t be scanned; they will need a partial shipment.</span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
