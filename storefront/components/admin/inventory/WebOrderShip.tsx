"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, errMsg } from "../../../lib/adminApi";
import { useAccess } from "../AdminAccess";
import { Badge, Notice, btnCls, btn2Cls, dangerCls, inputCls } from "../ui";
import ScanBox from "./ScanBox";
import { beep, lookupCode, newId } from "./shared";

type Item = {
  item_id: number;
  product_id: number | null;
  sku: string | null;
  title: string;
  bin_location: string | null;
  ordered: number;
  shipped_before: number; // already picked (scanned out) and saved
  short_qty: number; // flagged: could not be found / damaged
  on_hand: number;
  thumb: string | null;
};
type Order = {
  order_number: string;
  status: string;
  customer_name: string | null;
  paid_at: string | null;
  picked_at: string | null;
  shipped_at: string | null;
  tracking_number: string | null;
  carrier: string | null;
  cancelled_at: string | null;
  cancel_reason: string | null;
  total_cents: number;
  refunded_cents: number;
  pays_by_stripe: boolean;
  attention: string[];
  shipping_method: string | null;
  ship_to: { name?: string; line1?: string; line2?: string; city?: string; region?: string; postal_code?: string };
  items: Item[];
};
type ToShip = {
  order_number: string;
  customer_name: string | null;
  paid_at: string | null;
  units: number;
  stage: "pick" | "ship";
  attention: string[];
};
type Flash = { kind: "success" | "error" | "info"; text: string };

const money = (c: number) => `$${(c / 100).toFixed(2)}`;

/**
 * Website orders, in three steps:
 *   1. PICK    open the order, scan every part. Committing a pick takes the parts off the shelf count.
 *   2. SHIP    once everything is picked, enter the tracking number and mark it shipped. That completes it.
 *   3. If something goes wrong: flag a line as short, cancel the order, or (owner / admin) refund.
 * Orders with a red flag (a short pick, or not enough on the shelf to cover every paid order) sort to the top.
 */
export default function WebOrderShip({ active }: { active: boolean }) {
  const { isAdmin } = useAccess();
  const scanRef = useRef<HTMLInputElement>(null);
  const [order, setOrder] = useState<Order | null>(null);
  const [scanned, setScanned] = useState<Record<number, number>>({});
  const [list, setList] = useState<ToShip[]>([]);
  const [batchId, setBatchId] = useState(newId);
  const [perScan, setPerScan] = useState(1);
  const [flash, setFlash] = useState<Flash | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  // short pick
  const [shortFor, setShortFor] = useState<number | null>(null);
  const [shortQty, setShortQty] = useState("1");
  const [shortCount, setShortCount] = useState("");
  // ship
  const [tracking, setTracking] = useState("");
  const [carrier, setCarrier] = useState("");
  // cancel / refund
  const [cancelling, setCancelling] = useState(false);
  const [cancelReason, setCancelReason] = useState("");
  const [refundAmt, setRefundAmt] = useState("");
  const [refundConfirm, setRefundConfirm] = useState(false);

  const focus = () => setTimeout(() => scanRef.current?.focus(), 0);
  useEffect(() => {
    if (active) scanRef.current?.focus();
  }, [active]);

  const loadList = useCallback(() => {
    api<{ items: ToShip[] }>("/inventory/orders/to-ship")
      .then((r) => setList(r.items))
      .catch((e) => setFlash({ kind: "error", text: errMsg(e) }));
  }, []);
  useEffect(loadList, [loadList]);

  function resetPanels() {
    setShortFor(null);
    setCancelling(false);
    setCancelReason("");
    setRefundConfirm(false);
    setConfirming(false);
    setTracking("");
    setCarrier("");
  }

  const left = (i: Item) => (i.product_id == null ? 0 : i.ordered - i.shipped_before - (scanned[i.product_id] ?? 0));
  const scannable = order ? order.items.filter((i) => i.product_id != null) : [];
  const complete = !!order && order.items.length > 0 && order.items.every((i) => i.product_id != null && left(i) === 0);
  const scannedUnits = Object.values(scanned).reduce((a, b) => a + b, 0);
  const totalLeft = order ? order.items.reduce((n, i) => n + Math.max(0, left(i)), 0) : 0;
  const pickedUnits = order ? order.items.reduce((n, i) => n + i.shipped_before, 0) : 0;

  const stage: "pick" | "ship" | "done" | "cancelled" = !order
    ? "pick"
    : order.status === "cancelled"
    ? "cancelled"
    : order.status !== "paid" || order.shipped_at
    ? "done"
    : order.picked_at
    ? "ship"
    : "pick";

  async function fetchOrder(num: string) {
    return api<Order>(`/inventory/orders/${encodeURIComponent(num.trim())}`);
  }

  async function openOrder(num: string) {
    setFlash(null);
    try {
      const o = await fetchOrder(num);
      setOrder(o);
      setScanned({});
      setBatchId(newId());
      resetPanels();
      setRefundAmt(((o.total_cents - o.refunded_cents) / 100).toFixed(2));
      beep(true);
    } catch (e) {
      beep(false);
      setFlash({ kind: "error", text: errMsg(e) });
    }
  }

  /** Re-read the open order from the server, keeping the scans that haven't been committed yet. */
  async function refreshOrder(num: string) {
    const o = await fetchOrder(num);
    setOrder(o);
    setRefundAmt(((o.total_cents - o.refunded_cents) / 100).toFixed(2));
    return o;
  }

  async function handleScan(code: string) {
    setFlash(null);
    setConfirming(false);
    if (!order) {
      await openOrder(code); // with no order open, the scan box takes the order number
      focus();
      return;
    }
    if (stage !== "pick") {
      beep(false);
      setFlash({ kind: "info", text: "This order isn’t being picked, so there is nothing to scan." });
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

  /** Commit the pick: the scanned parts come off the shelf count and the order moves along. */
  async function commitPick() {
    if (!order) return;
    setBusy(true);
    setFlash(null);
    try {
      const r = await api<{ fully_picked: boolean }>("/inventory/ship", {
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
      setScanned({});
      setBatchId(newId());
      loadList();
      if (r.fully_picked) {
        // Straight on to the shipping stage so the tracking number can be entered.
        await refreshOrder(order.order_number);
        setFlash({ kind: "success", text: `Order ${order.order_number} is fully picked. Enter the tracking number to ship it.` });
      } else {
        setFlash({
          kind: "success",
          text: `Partial pick saved for order ${order.order_number}. It stays open for the rest.`,
        });
        setOrder(null);
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && e.message.startsWith("Already saved")) {
        setFlash({ kind: "success", text: "That pick was already saved the first time." });
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

  async function submitShort(item: Item) {
    if (!order || item.product_id == null) return;
    const q = parseInt(shortQty, 10);
    const count = shortCount.trim() === "" ? null : parseInt(shortCount, 10);
    if (!Number.isInteger(q) || q < 1 || (count !== null && (!Number.isInteger(count) || count < 0))) {
      setFlash({ kind: "error", text: "Enter how many are missing, and (optionally) the real shelf count as a whole number." });
      return;
    }
    setBusy(true);
    try {
      await api(`/inventory/orders/${encodeURIComponent(order.order_number)}/short`, {
        method: "POST",
        body: { product_id: item.product_id, quantity: q, actual_on_hand: count },
      });
      await refreshOrder(order.order_number);
      loadList();
      setShortFor(null);
      beep(false);
      setFlash({ kind: "info", text: `${item.sku} flagged as short. The order is marked red until it is picked or cancelled.` });
    } catch (e) {
      setFlash({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy(false);
    }
  }

  async function markShipped() {
    if (!order) return;
    setBusy(true);
    setFlash(null);
    try {
      await api(`/inventory/orders/${encodeURIComponent(order.order_number)}/shipped`, {
        method: "POST",
        body: { tracking_number: tracking.trim(), carrier: carrier.trim() || null },
      });
      setFlash({ kind: "success", text: `Order ${order.order_number} shipped. Tracking ${tracking.trim()}.` });
      setOrder(null);
      resetPanels();
      loadList();
    } catch (e) {
      setFlash({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy(false);
    }
  }

  async function cancelOrder() {
    if (!order) return;
    setBusy(true);
    setFlash(null);
    try {
      const r = await api<{ put_back: number; refundable_cents: number }>(
        `/inventory/orders/${encodeURIComponent(order.order_number)}/cancel`,
        { method: "POST", body: { reason: cancelReason.trim() || null } }
      );
      await refreshOrder(order.order_number);
      setScanned({});
      resetPanels();
      loadList();
      setFlash({
        kind: "success",
        text:
          `Order ${order.order_number} cancelled. The parts are available to sell again` +
          (r.put_back > 0 ? ` and ${r.put_back} picked unit${r.put_back === 1 ? "" : "s"} went back onto the shelf count` : "") +
          `. The customer has NOT been refunded yet.`,
      });
    } catch (e) {
      setFlash({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy(false);
    }
  }

  async function refund() {
    if (!order) return;
    const cents = Math.round(parseFloat(refundAmt) * 100);
    if (!Number.isFinite(cents) || cents < 1) {
      setFlash({ kind: "error", text: "Enter the amount to refund, for example 24.99." });
      return;
    }
    setBusy(true);
    setFlash(null);
    try {
      const r = await api<{ left_cents: number }>(`/inventory/orders/${encodeURIComponent(order.order_number)}/refund`, {
        method: "POST",
        body: { amount_cents: cents },
      });
      await refreshOrder(order.order_number);
      setRefundConfirm(false);
      setFlash({
        kind: "success",
        text: `Refunded ${money(cents)} through Stripe.` + (r.left_cents > 0 ? ` ${money(r.left_cents)} is still unrefunded.` : " The order is fully refunded."),
      });
    } catch (e) {
      setFlash({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy(false);
    }
  }

  const a = order?.ship_to;
  const refundLeft = order ? order.total_cents - order.refunded_cents : 0;
  const flagged = list.filter((o) => o.attention.length > 0);
  const toPick = list.filter((o) => o.attention.length === 0 && o.stage === "pick");
  const toShip = list.filter((o) => o.attention.length === 0 && o.stage === "ship");

  const Row = ({ o }: { o: ToShip }) => (
    <li key={o.order_number} className={o.attention.length ? "bg-red-50" : ""}>
      <button
        type="button"
        onClick={() => openOrder(o.order_number)}
        className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left text-sm hover:bg-gray-50"
      >
        <span className="min-w-0">
          <span className="font-semibold">{o.order_number}</span>
          <span className="ml-2 text-gray-600">{o.customer_name}</span>
          {o.attention.map((t) => (
            <span key={t} className="mt-1 block text-xs font-semibold text-red-700">
              ⚠ {t}
            </span>
          ))}
        </span>
        <span className="shrink-0 text-gray-500">
          {o.stage === "ship" ? "picked · " : ""}
          {o.units} unit{o.units === 1 ? "" : "s"}
          {o.paid_at ? ` · paid ${new Date(o.paid_at).toLocaleDateString()}` : ""}
        </span>
      </button>
    </li>
  );
  const Group = ({ title, rows, tone }: { title: string; rows: ToShip[]; tone?: "red" }) =>
    rows.length === 0 ? null : (
      <div className="space-y-2">
        <h3 className={`text-sm font-semibold uppercase tracking-wide ${tone === "red" ? "text-red-700" : "text-gray-500"}`}>
          {title} ({rows.length})
        </h3>
        <ul
          className={`divide-y divide-gray-100 rounded-lg border bg-white ${tone === "red" ? "border-red-300" : "border-gray-200"}`}
        >
          {rows.map((o) => (
            <Row key={o.order_number} o={o} />
          ))}
        </ul>
      </div>
    );

  return (
    <div className="space-y-4">
      <p className="text-sm text-gray-600">
        Scan or type the order number to open it, then scan every part going in the box. Committing the pick takes those
        parts off the shelf count. When everything is picked, enter the tracking number to ship it. A part you can’t find
        or that is damaged can be flagged as short.
      </p>

      <div className="grid gap-3 md:grid-cols-[1fr_auto]">
        <ScanBox
          ref={scanRef}
          onScan={handleScan}
          placeholder={order ? (stage === "pick" ? "Scan a part…" : "Order open") : "Scan or type the order number…"}
        />
        {order && stage === "pick" && (
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
        <div className="space-y-4">
          {list.length === 0 ? (
            <p className="rounded border border-dashed border-gray-300 p-6 text-center text-sm text-gray-500">
              No website orders are waiting.
            </p>
          ) : (
            <>
              <Group title="Needs attention" rows={flagged} tone="red" />
              <Group title="To pick" rows={toPick} />
              <Group title="Picked, ready to ship (needs tracking)" rows={toShip} />
            </>
          )}
          <button className="text-sm underline" onClick={loadList}>
            Refresh list
          </button>
        </div>
      ) : (
        <div className="space-y-4">
          <div className="flex flex-wrap items-start justify-between gap-3 rounded-lg border border-gray-200 bg-white p-4">
            <div className="text-sm">
              <div className="flex flex-wrap items-center gap-2 text-lg font-semibold">
                Order {order.order_number}
                {stage === "pick" && <Badge tone="amber">picking</Badge>}
                {stage === "ship" && <Badge tone="blue">picked · ready to ship</Badge>}
                {stage === "done" && <Badge tone="green">{order.status}</Badge>}
                {stage === "cancelled" && <Badge tone="gray">cancelled</Badge>}
              </div>
              <div>{a?.name ?? order.customer_name}</div>
              <div className="text-gray-600">
                {[a?.line1, a?.line2].filter(Boolean).join(", ")}
                {a?.city ? `, ${a.city}` : ""} {a?.region} {a?.postal_code}
              </div>
              {order.shipping_method && <div className="mt-1 text-gray-600">Ship via: {order.shipping_method}</div>}
              {order.tracking_number && (
                <div className="mt-1 text-gray-600">
                  Tracking: {order.carrier ? `${order.carrier} ` : ""}
                  {order.tracking_number}
                </div>
              )}
              {order.cancelled_at && order.cancel_reason && (
                <div className="mt-1 text-gray-600">Cancel reason: {order.cancel_reason}</div>
              )}
            </div>
            <button
              className={btn2Cls}
              onClick={() => {
                setOrder(null);
                setScanned({});
                resetPanels();
                setFlash({ kind: "info", text: "Order closed. Anything scanned but not committed was not saved." });
                loadList();
                focus();
              }}
            >
              Close order
            </button>
          </div>

          {order.attention.length > 0 && (
            <div className="rounded-lg border-2 border-red-400 bg-red-50 p-3 text-sm font-semibold text-red-800">
              {order.attention.map((t) => (
                <div key={t}>⚠ {t}</div>
              ))}
              <div className="mt-1 font-normal text-red-700">
                Decide whether to wait for stock (mark the part On order on the Stock tab) or cancel this order.
              </div>
            </div>
          )}

          <ul className="divide-y divide-gray-100 rounded-lg border border-gray-200 bg-white">
            {order.items.map((i) => {
              const done = i.product_id != null && left(i) === 0;
              const got = i.product_id != null ? i.shipped_before + (scanned[i.product_id] ?? 0) : 0;
              const canFlag = stage === "pick" && i.product_id != null && !done;
              return (
                <li key={i.item_id} className={`px-4 py-3 ${done ? "bg-green-50" : i.short_qty > 0 ? "bg-red-50" : ""}`}>
                  <div className="flex items-center gap-3">
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
                        {stage === "pick" ? ` · shelf count ${i.on_hand}` : ""}
                      </div>
                      {i.product_id == null && (
                        <div className="text-xs text-red-700">This line has no product attached, so it can’t be scanned.</div>
                      )}
                      {i.short_qty > 0 && (
                        <div className="text-xs font-semibold text-red-700">SHORT: {i.short_qty} could not be found or was damaged</div>
                      )}
                    </div>
                    <div className="text-right">
                      <div className="text-xl font-semibold">
                        {got} / {i.ordered}
                      </div>
                      {done ? <Badge tone="green">done</Badge> : <Badge tone="amber">{left(i)} to go</Badge>}
                    </div>
                  </div>
                  {canFlag && (
                    <div className="mt-2 pl-[68px]">
                      {shortFor === i.product_id ? (
                        <div className="flex flex-wrap items-end gap-2 rounded border border-red-200 bg-white p-2 text-sm">
                          <label className="text-xs font-medium text-gray-700">
                            <span className="block">How many can’t you find?</span>
                            <input
                              type="number"
                              min={1}
                              max={left(i)}
                              className={`${inputCls} w-24`}
                              value={shortQty}
                              onChange={(e) => setShortQty(e.target.value)}
                            />
                          </label>
                          <label className="text-xs font-medium text-gray-700">
                            <span className="block">Real shelf count (optional)</span>
                            <input
                              type="number"
                              min={0}
                              className={`${inputCls} w-28`}
                              value={shortCount}
                              placeholder={`now ${i.on_hand}`}
                              onChange={(e) => setShortCount(e.target.value)}
                            />
                          </label>
                          <button className={dangerCls} disabled={busy} onClick={() => submitShort(i)}>
                            Flag as short
                          </button>
                          <button className={btn2Cls} disabled={busy} onClick={() => setShortFor(null)}>
                            Never mind
                          </button>
                        </div>
                      ) : (
                        <button
                          className="text-xs font-medium text-red-700 underline"
                          onClick={() => {
                            setShortFor(i.product_id);
                            setShortQty(String(Math.max(1, left(i))));
                            setShortCount("");
                          }}
                        >
                          Can’t find it / damaged
                        </button>
                      )}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>

          {/* ---------------- step 1: pick ---------------- */}
          {stage === "pick" && (
            <div className="flex flex-wrap items-center gap-3">
              {!confirming ? (
                <button className={btnCls} disabled={busy || (scannedUnits === 0 && !complete)} onClick={() => setConfirming(true)}>
                  {complete
                    ? "Commit pick"
                    : scannedUnits === 0
                    ? "Scan items to pick"
                    : `Commit partial pick (${scannedUnits} scanned, ${totalLeft} not yet)`}
                </button>
              ) : (
                <>
                  <span className="text-sm font-medium">
                    {complete
                      ? `Commit the pick for order ${order.order_number}? The parts come off the shelf count and the order moves on to shipping.`
                      : `Only ${scannedUnits} of the units are scanned. Commit a partial pick and keep the order open?`}
                  </span>
                  <button className={btnCls} disabled={busy} onClick={commitPick}>
                    {busy ? "Saving…" : "Yes"}
                  </button>
                  <button className={btn2Cls} disabled={busy} onClick={() => setConfirming(false)}>
                    Go back
                  </button>
                </>
              )}
              {scannable.length < order.items.length && (
                <span className="text-sm text-amber-700">Some lines can’t be scanned; they will need a partial pick.</span>
              )}
            </div>
          )}

          {/* ---------------- step 2: ship ---------------- */}
          {stage === "ship" && (
            <div className="space-y-3 rounded-lg border border-blue-200 bg-blue-50 p-4">
              <div className="text-sm font-semibold text-blue-900">Every part is picked. Enter the tracking number to ship it.</div>
              <div className="flex flex-wrap items-end gap-3">
                <label className="text-sm font-medium text-gray-700">
                  <span className="block">Carrier (optional)</span>
                  <input className={`${inputCls} w-40`} value={carrier} placeholder="USPS, UPS…" onChange={(e) => setCarrier(e.target.value)} />
                </label>
                <label className="min-w-[16rem] flex-1 text-sm font-medium text-gray-700">
                  <span className="block">Tracking number</span>
                  <input className={inputCls} value={tracking} onChange={(e) => setTracking(e.target.value)} />
                </label>
                <button className={btnCls} disabled={busy || tracking.trim().length < 3} onClick={markShipped}>
                  {busy ? "Saving…" : "Mark shipped"}
                </button>
              </div>
            </div>
          )}

          {/* ---------------- cancel (paid orders that haven't shipped) ---------------- */}
          {(stage === "pick" || stage === "ship") && (
            <div className="rounded-lg border border-gray-200 bg-white p-4">
              {!cancelling ? (
                <button className={dangerCls} onClick={() => setCancelling(true)}>
                  Cancel this order…
                </button>
              ) : (
                <div className="space-y-3 text-sm">
                  <div className="font-semibold text-red-800">Cancel order {order.order_number}?</div>
                  <p className="text-gray-700">
                    The parts that haven’t been picked are released and can be sold again.
                    {pickedUnits > 0
                      ? ` ${pickedUnits} unit${pickedUnits === 1 ? "" : "s"} already picked will be added back to the shelf count, so put them back on the shelf.`
                      : ""}{" "}
                    This does not refund the customer; you can do that next.
                  </p>
                  <label className="block max-w-md text-sm font-medium text-gray-700">
                    <span className="block">Reason (optional)</span>
                    <input className={inputCls} value={cancelReason} onChange={(e) => setCancelReason(e.target.value)} />
                  </label>
                  <div className="flex gap-2">
                    <button className={dangerCls} disabled={busy} onClick={cancelOrder}>
                      {busy ? "Cancelling…" : "Yes, cancel the order"}
                    </button>
                    <button className={btn2Cls} disabled={busy} onClick={() => setCancelling(false)}>
                      Keep the order
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}

          {/* ---------------- refund (owner / admin) ---------------- */}
          {(stage === "cancelled" || stage === "done") && order.paid_at && (
            <div className="space-y-2 rounded-lg border border-gray-200 bg-white p-4 text-sm">
              <div className="font-semibold">Refund</div>
              <div className="text-gray-700">
                Order total {money(order.total_cents)} · refunded so far {money(order.refunded_cents)}
              </div>
              {!isAdmin ? (
                <div className="text-gray-500">Only an owner or admin can refund through Stripe.</div>
              ) : !order.pays_by_stripe ? (
                <div className="text-gray-500">This order wasn’t paid through Stripe on the website, so it can’t be refunded here.</div>
              ) : refundLeft <= 0 ? (
                <div className="font-medium text-green-800">Fully refunded.</div>
              ) : !refundConfirm ? (
                <div className="flex flex-wrap items-end gap-2">
                  <label className="text-xs font-medium text-gray-700">
                    <span className="block">Amount to refund ($)</span>
                    <input
                      className={`${inputCls} w-32`}
                      value={refundAmt}
                      inputMode="decimal"
                      onChange={(e) => setRefundAmt(e.target.value)}
                    />
                  </label>
                  <button className={dangerCls} onClick={() => setRefundConfirm(true)}>
                    Refund through Stripe…
                  </button>
                </div>
              ) : (
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium text-red-800">
                    Send {money(Math.round((parseFloat(refundAmt) || 0) * 100))} back to the customer’s card? This can’t be undone.
                  </span>
                  <button className={dangerCls} disabled={busy} onClick={refund}>
                    {busy ? "Refunding…" : "Yes, refund"}
                  </button>
                  <button className={btn2Cls} disabled={busy} onClick={() => setRefundConfirm(false)}>
                    Go back
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
