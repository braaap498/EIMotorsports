"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";

type Q = {
  ei_total: number;
  own_available: number;
  warehouses: { id: number; name: string; quantity: number; main: boolean }[];
  // use_mfr_number: the part is set to "use the manu. part # for the custom label", so show its manufacturer number (when the server sends one)
  kit_parts?: { part_number: string; need: number; on_our_shelf: number; kits: number; use_mfr_number?: boolean; mfr_number?: string | null }[];
  indiana: number;
  nevada: number;
  // Rocky Mountain ATV parts on the product: Utah and Kentucky stock (display only)
  rm_parts?: number;
  rm_utah?: number;
  rm_kentucky?: number;
  rm_total?: number;
  has_automatic?: boolean;
  distributor: number;
  distributor_usable?: number;
  safety_pct?: number;
  kits_we_can_assemble?: number | null;
  ebay_listings?: { listing_id: number; store: string; item_id: string; drop_ship: boolean; pieces_per_sale: number; units_available: number; shows: number; source?: "distributor" | "our shelf" | "none"; drop_ship_paused?: boolean }[];
  any_drop_ship?: boolean;
  distributor_name?: string;
  parts: number;
  is_kit: boolean;
  drop_ship: boolean;
  drop_ship_listings: number;
  total_available: number;
};

/**
 * Read-only summary under the stock fields: what we hold (total and per warehouse), what the distributor holds, and the
 * Total Quantity Available that the website and eBay use. Not drop ship = our shelf only. Drop ship = our shelf plus the distributor.
 * Shows what is SAVED; press Refresh after saving the product.
 */
export default function QuantitiesPanel({
  productId,
  refreshKey,
  canEdit = false,
  onSaved,
}: {
  productId: number;
  refreshKey?: unknown;
  /** may change stock (single-part products only: a kit's count is made from its parts) */
  canEdit?: boolean;
  /** called with the new total on hand after a warehouse count was saved */
  onSaved?: (total: number) => void;
}) {
  const [q, setQ] = useState<Q | null>(null);
  const [err, setErr] = useState("");
  const [drafts, setDrafts] = useState<Record<number, string>>({});
  const [busy, setBusy] = useState<number | null>(null);
  const [saveErr, setSaveErr] = useState("");

  async function saveWarehouse(id: number) {
    const raw = drafts[id];
    if (raw === undefined) return;
    const n = Number(raw);
    if (raw.trim() === "" || !Number.isInteger(n) || n < 0) {
      setSaveErr("Enter a whole number, 0 or more.");
      return;
    }
    setBusy(id);
    setSaveErr("");
    try {
      const r = await api<{ ok: boolean; quantity: number; total: number }>(`/inventory/warehouses/${id}/stock/${productId}`, {
        method: "PUT",
        body: { quantity: n },
      });
      setDrafts((d) => {
        const { [id]: _gone, ...rest } = d;
        return rest;
      });
      await load();
      onSaved?.(r.total);
    } catch (e) {
      setSaveErr(errMsg(e));
    } finally {
      setBusy(null);
    }
  }

  const load = useCallback(async () => {
    try {
      setQ(await api<Q>(`/availability/products/${productId}/quantities`));
      setErr("");
    } catch (e) {
      setErr(errMsg(e));
    }
  }, [productId]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  if (err) return <p className="mt-3 text-xs text-amber-800">Quantities could not be loaded: {err}</p>;
  if (!q) return null;

  const hasRm = (q.rm_parts ?? 0) > 0;
  const hasAuto = q.has_automatic ?? !hasRm; // older servers do not send the flag: treat the product as Automatic
  const hasDist = q.parts > 0 && hasAuto;
  const row = "flex justify-between gap-4 py-0.5";
  return (
    <div className="mt-4 rounded border border-gray-200 bg-gray-50 p-3 text-sm">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-xs font-semibold uppercase text-gray-500">Quantities (saved)</span>
        <button type="button" className="text-xs underline" onClick={() => void load()}>
          Refresh
        </button>
      </div>
      <div className="grid gap-x-8 gap-y-2 sm:grid-cols-2">
        <div>
          {!q.is_kit && q.warehouses.length > 0 ? (
            <>
              {q.warehouses.map((w) => {
                const shown = drafts[w.id] ?? String(w.quantity);
                const changed = drafts[w.id] !== undefined && drafts[w.id] !== String(w.quantity);
                return (
                  <div key={w.id} className={`${row} items-center ${w.main ? "font-medium" : "text-xs text-gray-700"}`}>
                    <span>{w.main ? "E&I warehouse quantity" : w.name}</span>
                    {canEdit ? (
                      <span className="relative inline-block">
                        <input
                          type="number"
                          min={0}
                          step={1}
                          className="w-32 rounded border border-gray-300 py-0.5 pl-16 pr-1 text-right text-sm"
                          value={shown}
                          disabled={busy === w.id}
                          onChange={(e) => setDrafts((d) => ({ ...d, [w.id]: e.target.value }))}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") {
                              e.preventDefault(); // inside the product form: Enter must not save the product
                              void saveWarehouse(w.id);
                            }
                          }}
                        />
                        {changed && (
                          <button
                            type="button"
                            className="absolute left-1 top-1/2 -translate-y-1/2 rounded bg-gray-800 px-2 py-0.5 text-xs text-white disabled:opacity-50"
                            disabled={busy === w.id}
                            onClick={() => void saveWarehouse(w.id)}
                          >
                            {busy === w.id ? "Saving…" : "Save"}
                          </button>
                        )}
                      </span>
                    ) : (
                      <span>{w.quantity}</span>
                    )}
                  </div>
                );
              })}
              {saveErr && <p className="text-xs text-red-700">{saveErr}</p>}
              <div className={`${row} mt-1 border-t border-gray-200 pt-1 text-xs text-gray-600`}>
                <span>Total on hand (all warehouses, calculated)</span>
                <span>{q.ei_total}</span>
              </div>
            </>
          ) : (
            <>
              <div className={`${row} font-medium`}>
                <span>E&amp;I warehouse quantity</span>
                <span>{q.ei_total}</span>
              </div>
              {q.is_kit && (
                <p className="text-xs text-gray-500">Kits made from our own single-part products. Change the count on those products.</p>
              )}
              {(q.kit_parts ?? []).map((k) => (
                <div key={k.part_number} className={`${row} pl-3 text-xs text-gray-600`}>
                  <span>
                    {k.use_mfr_number && k.mfr_number ? k.mfr_number : k.part_number}
                    {k.need > 1 ? ` (${k.need} per kit)` : ""}: {k.on_our_shelf} on our shelf
                  </span>
                  <span>{k.kits} kit{k.kits === 1 ? "" : "s"}</span>
                </div>
              ))}
            </>
          )}
          {q.is_kit && typeof q.kits_we_can_assemble === "number" && (
            <div className={`${row} mt-1 border-t border-gray-200 pt-1 font-medium`}>
              <span>Kits we can assemble now (not set aside for orders)</span>
              <span>{q.kits_we_can_assemble}</span>
            </div>
          )}
          {q.ei_total !== q.own_available && (
            <div className={`${row} pl-3 text-xs text-gray-600`}>
              <span>Available (not set aside for orders)</span>
              <span>{q.own_available}</span>
            </div>
          )}
        </div>
        <div>
          {hasDist ? (
            <>
              <div className={`${row} font-medium`}>
                <span>{q.distributor_name ?? "Distributor"}</span>
                <span>{q.distributor}</span>
              </div>
              <div className={`${row} pl-3 text-xs text-gray-600`}>
                <span>{q.distributor_name ? `${q.distributor_name} · Indiana` : "Indiana"}</span>
                <span>{q.indiana}</span>
              </div>
              <div className={`${row} pl-3 text-xs text-gray-600`}>
                <span>{q.distributor_name ? `${q.distributor_name} · Nevada` : "Nevada"}</span>
                <span>{q.nevada}</span>
              </div>
              {typeof q.distributor_usable === "number" && (q.safety_pct ?? 0) > 0 && (
                <div className={`${row} pl-3 text-xs text-gray-600`}>
                  <span>
                    Usable after the {q.safety_pct}% safety margin (better warehouse alone)
                  </span>
                  <span>{q.distributor_usable}</span>
                </div>
              )}
            </>
          ) : !hasRm ? (
            <div className="text-xs text-gray-500">No distributor part on this product.</div>
          ) : null}
          {hasRm && (
            <div className={hasDist ? "mt-2" : ""}>
              <div className={`${row} font-medium`}>
                <span>Rocky Mountain ATV</span>
                <span>{q.rm_total ?? 0}</span>
              </div>
              <div className={`${row} pl-3 text-xs text-gray-600`}>
                <span>Rocky Mountain ATV · Utah</span>
                <span>{q.rm_utah ?? 0}</span>
              </div>
              <div className={`${row} pl-3 text-xs text-gray-600`}>
                <span>Rocky Mountain ATV · Kentucky</span>
                <span>{q.rm_kentucky ?? 0}</span>
              </div>
            </div>
          )}
        </div>
      </div>
      <div className="mt-2 border-t border-gray-200 pt-2">
        <div className={`${row} text-base font-semibold`}>
          <span>Total quantity available</span>
          <span>{q.total_available}</span>
        </div>
        <p className="text-xs text-gray-500">
          {q.drop_ship
            ? "Drop ship is on: the better distributor warehouse's usable stock while it can fill a sale, otherwise our shelf (never added together). This is what the website and eBay use."
            : q.is_kit
              ? "Drop ship is off: the kits we can assemble from our own parts. This is what the website and eBay use."
              : "Drop ship is off: our shelf only. This is what the website and eBay use."}
          {!q.drop_ship && q.drop_ship_listings > 0 ? ` ${q.drop_ship_listings} eBay listing${q.drop_ship_listings === 1 ? " is" : "s are"} set to drop ship and show the distributor's usable stock while it can fill a sale.` : ""}
        </p>
        {(q.ebay_listings ?? []).length > 0 && (
          <details className="mt-2 border-t border-gray-200 pt-2">
            <summary className="cursor-pointer text-xs font-semibold uppercase text-gray-500">
              Quantity eBay shows ({(q.ebay_listings ?? []).length} listing{(q.ebay_listings ?? []).length === 1 ? "" : "s"})
            </summary>
            {(q.ebay_listings ?? []).map((l) => (
              <div key={l.listing_id} className={`${row} text-xs text-gray-700`}>
                <span>
                  {l.store} · item {l.item_id}
                  {l.pieces_per_sale > 1 ? ` · ${l.pieces_per_sale} per sale` : ""}
                  {l.drop_ship ? (l.drop_ship_paused ? " · drop ship paused, using our shelf" : " · drop ship, from the distributor") : ""}
                </span>
                <span className="font-medium">
                  {l.shows}
                  {l.pieces_per_sale > 1 ? ` (${l.units_available} units)` : ""}
                </span>
              </div>
            ))}
            <p className="text-xs text-gray-500">The number the quantity updates send to eBay: units available divided by pieces per sale, rounded down.</p>
          </details>
        )}
      </div>
    </div>
  );
}
