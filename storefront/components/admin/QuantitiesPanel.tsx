"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";

type Q = {
  ei_total: number;
  own_available: number;
  warehouses: { name: string; quantity: number; main: boolean }[];
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
export default function QuantitiesPanel({ productId, refreshKey }: { productId: number; refreshKey?: unknown }) {
  const [q, setQ] = useState<Q | null>(null);
  const [err, setErr] = useState("");

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
          <div className={`${row} font-medium`}>
            <span>E&amp;I warehouse quantity</span>
            <span>{q.ei_total}</span>
          </div>
          {q.warehouses.length > 1 &&
            q.warehouses.map((w) => (
              <div key={w.name} className={`${row} pl-3 text-xs text-gray-600`}>
                <span>{w.name}</span>
                <span>{w.quantity}</span>
              </div>
            ))}
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
