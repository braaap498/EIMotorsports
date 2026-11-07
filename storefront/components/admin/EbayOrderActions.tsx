"use client";

import { useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Field, Modal, btnCls, btn2Cls, inputCls } from "./ui";

/* The buttons for one eBay order. They live on Inventory > Pick & ship > eBay orders (the Orders page under Sales
   channels only shows the result: shipped in green, plus the tracking link).

     OrderShipControls  - tracking (shown, or "Check eBay" to fetch it), Open eBay label, Mark shipped (turns green)
     LineStockButtons   - Mark picked / Release for a line whose parts are set aside

   Tracking and shipping belong to the ORDER (every line of an order shows the same state); picking and
   releasing belong to the LINE. */

export type OrderLike = {
  id: number; // the order LINE id (used by pick / release)
  order_pk: number;
  store: string;
  ebay_order_id: string;
  state: string;
  cancelled: boolean;
  shipped_at_ebay: string | null;
  tracking_number: string | null;
  tracking_carrier: string | null;
  shipping_service: string | null;
};

export type Patch = Partial<Pick<OrderLike, "shipped_at_ebay" | "tracking_number" | "tracking_carrier">>;

/** The staff-accessible routes (inventory.ship), not the admin-only /ebay-sales ones. */
const BASE = "/inventory/ebay-orders";

const CARRIERS = ["USPS", "UPS", "FedEx", "DHL", "Other"] as const;

/** Where eBay's own page for this order lives (the "Print shipping label" button is on it). You must be signed in to
    the matching eBay account. If eBay ever changes the address, this is the one place to fix. */
export const ebayOrderUrl = (orderId: string) => `https://www.ebay.com/sh/ord/details?orderid=${encodeURIComponent(orderId)}`;

function normCarrier(c: string | null | undefined): string {
  const s = (c ?? "").toLowerCase().replace(/[^a-z]/g, "");
  if (!s) return "";
  if (s.includes("usps") || s.includes("postal")) return "USPS";
  if (s.includes("ups")) return "UPS";
  if (s.includes("fedex")) return "FedEx";
  if (s.includes("dhl")) return "DHL";
  return "Other";
}

/** Best guess of the carrier: what eBay already recorded, else what the buyer's shipping service says. */
function guessCarrier(o: OrderLike): string {
  return normCarrier(o.tracking_carrier) || normCarrier(o.shipping_service);
}

export function trackingUrl(carrier: string | null | undefined, num: string): string | null {
  const n = encodeURIComponent(num);
  switch (normCarrier(carrier)) {
    case "USPS":
      return `https://tools.usps.com/go/TrackConfirmAction?tLabels=${n}`;
    case "UPS":
      return `https://www.ups.com/track?tracknum=${n}`;
    case "FedEx":
      return `https://www.fedex.com/fedextrack/?trknbr=${n}`;
    case "DHL":
      return `https://www.dhl.com/us-en/home/tracking.html?tracking-id=${n}`;
    default:
      return null;
  }
}

export function OrderShipControls({
  o,
  onPatch,
  onError,
}: {
  o: OrderLike;
  onPatch: (p: Patch) => void;
  onError: (msg: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const shipped = !!o.shipped_at_ebay;

  async function checkTracking() {
    setBusy(true);
    try {
      const r = await api<{ tracking_number: string | null; tracking_carrier: string | null; shipped: boolean; found: boolean }>(
        `${BASE}/orders/${o.order_pk}/refresh-tracking`,
        { method: "POST" }
      );
      onPatch({
        tracking_number: r.tracking_number,
        tracking_carrier: r.tracking_carrier,
        ...(r.shipped && !o.shipped_at_ebay ? { shipped_at_ebay: new Date().toISOString() } : {}),
      });
      if (!r.found) onError("eBay has no tracking number on this order yet. Print the label on eBay first, or use Mark shipped.");
    } catch (e) {
      onError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  if (o.cancelled || o.state === "cancelled") return <span className="text-xs text-gray-400">Cancelled</span>;

  const link = o.tracking_number ? trackingUrl(o.tracking_carrier, o.tracking_number) : null;

  return (
    <div className="space-y-1.5">
      {/* tracking */}
      <div className="text-xs">
        {o.tracking_number ? (
          <span className="text-gray-700">
            {o.tracking_carrier ? `${o.tracking_carrier} ` : ""}
            {link ? (
              <a className="font-medium text-blue-700 underline" href={link} target="_blank" rel="noopener noreferrer">
                {o.tracking_number}
              </a>
            ) : (
              <span className="font-medium">{o.tracking_number}</span>
            )}
          </span>
        ) : (
          <button type="button" className="text-blue-700 underline disabled:opacity-40" disabled={busy} onClick={() => void checkTracking()}>
            {busy ? "Checking eBay…" : "Show tracking"}
          </button>
        )}
      </div>

      {/* label + shipped */}
      <div className="flex flex-wrap items-center gap-1.5">
        {!shipped && (
          <a
            className={`${btn2Cls} !px-2 !py-1 !text-xs`}
            href={ebayOrderUrl(o.ebay_order_id)}
            target="_blank"
            rel="noopener noreferrer"
            title="Opens this order on eBay, where you buy and print the label"
          >
            Open eBay label
          </a>
        )}
        {shipped ? (
          <button
            type="button"
            className="rounded bg-green-600 px-2 py-1 text-xs font-semibold text-white hover:bg-green-700"
            title={`Shipped ${new Date(o.shipped_at_ebay as string).toLocaleString()}. Click to change the tracking.`}
            onClick={() => setOpen(true)}
          >
            ✓ Shipped {new Date(o.shipped_at_ebay as string).toLocaleDateString()}
          </button>
        ) : (
          <button type="button" className={`${btnCls} !px-2 !py-1 !text-xs`} onClick={() => setOpen(true)}>
            Mark shipped
          </button>
        )}
      </div>

      {open && (
        <ShipModal
          o={o}
          shipped={shipped}
          onClose={() => setOpen(false)}
          onDone={(p) => {
            setOpen(false);
            onPatch(p);
          }}
        />
      )}
    </div>
  );
}

function ShipModal({
  o,
  shipped,
  onClose,
  onDone,
}: {
  o: OrderLike;
  shipped: boolean;
  onClose: () => void;
  onDone: (p: Patch) => void;
}) {
  const [carrier, setCarrier] = useState(guessCarrier(o) || "USPS");
  const [other, setOther] = useState("");
  const [num, setNum] = useState(o.tracking_number ?? "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit() {
    const c = carrier === "Other" ? other.trim() : carrier;
    const t = num.replace(/\s+/g, "");
    if (!c) return setErr("Type the carrier name.");
    if (!t) return setErr("Enter the tracking number.");
    setBusy(true);
    setErr(null);
    try {
      const r = await api<{ tracking_number: string; tracking_carrier: string }>(`${BASE}/orders/${o.order_pk}/ship`, {
        method: "POST",
        body: { carrier: c, tracking_number: t },
      });
      onDone({
        tracking_number: r.tracking_number,
        tracking_carrier: r.tracking_carrier,
        shipped_at_ebay: o.shipped_at_ebay ?? new Date().toISOString(),
      });
    } catch (e) {
      setErr(errMsg(e));
      setBusy(false);
    }
  }

  return (
    <Modal title={shipped ? "Change tracking" : "Mark shipped"} onClose={onClose}>
      <div className="space-y-3">
        <p className="text-sm text-gray-600">
          Order <strong>{o.ebay_order_id}</strong> ({o.store}).{" "}
          {shipped
            ? "This order is already marked shipped on eBay. Saving adds this tracking number to it."
            : "eBay marks the order shipped and sends the buyer a shipped notice with this tracking number."}
        </p>
        {err && <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">{err}</p>}
        <div className="flex flex-wrap gap-3">
          <Field label="Carrier" className="w-40">
            <select className={inputCls} value={carrier} onChange={(e) => setCarrier(e.target.value)}>
              {CARRIERS.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </Field>
          {carrier === "Other" && (
            <Field label="Carrier name" className="w-44">
              <input className={inputCls} value={other} onChange={(e) => setOther(e.target.value)} placeholder="e.g. OnTrac" />
            </Field>
          )}
          <Field label="Tracking number" className="min-w-[16rem] flex-1">
            <input
              className={inputCls}
              autoFocus
              value={num}
              onChange={(e) => setNum(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void submit();
              }}
              placeholder="Scan or paste it here"
            />
          </Field>
        </div>
        <div className="flex justify-end gap-2">
          <button type="button" className={btn2Cls} onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="rounded bg-green-600 px-4 py-2 text-sm font-semibold text-white hover:bg-green-700 disabled:opacity-40"
            disabled={busy}
            onClick={() => void submit()}
          >
            {busy ? "Sending to eBay…" : shipped ? "Save tracking" : "Mark shipped"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

/** Mark picked / Release for a line whose parts are set aside. Either one takes the line off the Inventory list. */
export function LineStockButtons({
  lineId,
  state,
  onDone,
  onError,
}: {
  lineId: number;
  state: string;
  onDone: () => void;
  onError: (msg: string) => void;
}) {
  if (state !== "reserved") return null;

  async function act(what: "pick" | "release") {
    const q =
      what === "pick"
        ? "Take these parts off the shelf now? Use this only when the part has already gone and was not scanned."
        : "Stop setting these parts aside WITHOUT changing the shelf count?";
    if (!window.confirm(q)) return;
    try {
      await api(`${BASE}/lines/${lineId}/${what}`, { method: "POST" });
      onDone();
    } catch (e) {
      onError(errMsg(e));
    }
  }

  return (
    <div className="flex flex-wrap gap-1.5">
      <button type="button" className={`${btn2Cls} !px-2 !py-1 !text-xs`} onClick={() => void act("pick")}>
        Mark picked
      </button>
      <button type="button" className={`${btn2Cls} !px-2 !py-1 !text-xs`} onClick={() => void act("release")}>
        Release
      </button>
    </div>
  );
}
