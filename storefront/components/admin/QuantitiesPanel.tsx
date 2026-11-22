"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";

type Q = {
  ei_total: number;
  own_available: number;
  warehouses: { id: number; name: string; quantity: number; main: boolean }[];
  kit_parts?: { part_number: string; need: number; on_our_shelf: number; kits: number }[];
  indiana: number;
  nevada: number;
  distributor: number;
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
    if (!Number.isInteger(n) || n < 0) {
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

  const hasDist = q.parts > 0;
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
                      <span className="flex items-center gap-1">
                        <input
                          type="number"
                          min={0}
                          step={1}
                          className="w-20 rounded border border-gray-300 px-1 py-0.5 text-right text-sm"
                          value={shown}
                          disabled={busy === w.id}
                          onChange={(e) => setDrafts((d) => ({ ...d, [w.id]: e.target.value }))}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") void saveWarehouse(w.id);
                          }}
                        />
                        {changed && (
                          <button type="button" className="rounded bg-gray-800 px-2 py-0.5 text-xs text-white disabled:opacity-50" disabled={busy === w.id} onClick={() => void saveWarehouse(w.id)}>
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
                    {k.part_number}
                    {k.need > 1 ? ` (${k.need} per kit)` : ""}: {k.on_our_shelf} on our shelf
                  </span>
                  <span>{k.kits} kit{k.kits === 1 ? "" : "s"}</span>
                </div>
              ))}
            </>
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
                <span>Distributor</span>
                <span>{q.distributor}</span>
              </div>
              <div className={`${row} pl-3 text-xs text-gray-600`}>
                <span>Indiana</span>
                <span>{q.indiana}</span>
              </div>
              <div className={`${row} pl-3 text-xs text-gray-600`}>
                <span>Nevada</span>
                <span>{q.nevada}</span>
              </div>
            </>
          ) : (
            <div className="text-xs text-gray-500">No distributor part on this product.</div>
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
            ? "Drop ship is on: our shelf plus the distributor. This is what the website and eBay use."
            : "Drop ship is off: our shelf only. This is what the website and eBay use."}
          {!q.drop_ship && q.drop_ship_listings > 0 ? ` ${q.drop_ship_listings} eBay listing${q.drop_ship_listings === 1 ? " is" : "s are"} set to drop ship and use our shelf plus the distributor.` : ""}
        </p>
      </div>
    </div>
  );
}
