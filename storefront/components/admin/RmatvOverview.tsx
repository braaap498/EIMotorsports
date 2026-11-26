"use client";

import { useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Notice } from "./ui";

// The Rocky Mountain catalog at a glance (same idea as the Automatic Distributors overview): totals, data gaps, price bands, brands.
type Totals = Record<string, number | string | null>;
type Brand = { brand: string; unbranded: boolean; parts: number; in_stock: number; units: number; stock_value_at_cost: number; min_cost: number | null; max_cost: number | null; avg_cost: number | null };
type Overview = { totals: Totals; brands: Brand[]; price_bands: { band: string; parts: number }[]; units_of_measure: { uom: string; unset: boolean; parts: number }[] };

const n = (x: unknown) => (x == null ? "—" : Number(x).toLocaleString("en-US"));
const money = (x: unknown) => (x == null ? "—" : `$${Number(x).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);

export default function RmatvOverview({ version }: { version: number }) {
  const [d, setD] = useState<Overview | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    api<Overview>("/rmatv/overview").then(setD).catch((e) => setErr(errMsg(e)));
  }, [version]);
  if (err) return <Notice kind="error">{err}</Notice>;
  if (!d) return <p className="text-sm text-gray-500">Loading…</p>;
  const t = d.totals;
  const cards: [string, string][] = [
    ["Current parts", n(t.active)], ["Dropped parts", n(t.retired)], ["In stock", n(t.in_stock)], ["Out of stock", n(t.out_of_stock)],
    ["Units at Rocky Mountain", n(t.units)], ["Stock value at your cost", money(t.stock_value_at_cost)], ["Stock value at list", money(t.stock_value_at_list)],
    ["Brands", n(t.brands)], ["Average margin", t.avg_margin_pct == null ? "—" : `${Number(t.avg_margin_pct).toFixed(0)}%`],
    ["Changed in 7 days", n(t.changed_7d)], ["Changed in 30 days", n(t.changed_30d)],
  ];
  const gaps: [string, unknown][] = [
    ["No barcode", t.no_barcode], ["No photo link", t.no_photo], ["No Mfr number", t.no_mfr_number], ["No name", t.no_name],
    ["No cost", t.no_cost], ["No list price", t.no_list_price], ["List price below cost", t.list_below_cost],
  ];
  return (
    <div className="space-y-4 text-sm">
      <div className="grid gap-2 sm:grid-cols-3 lg:grid-cols-4">
        {cards.map(([k, v]) => (
          <div key={k} className="rounded border border-gray-200 bg-white p-3">
            <div className="text-xs text-gray-500">{k}</div>
            <div className="text-lg font-semibold">{v}</div>
          </div>
        ))}
      </div>
      <div className="grid gap-4 lg:grid-cols-3">
        <div className="rounded border border-gray-200 bg-white p-3">
          <h3 className="mb-2 font-semibold">Data gaps (current parts)</h3>
          <ul className="space-y-1 text-xs">
            {gaps.map(([k, v]) => (
              <li key={k} className="flex justify-between"><span>{k}</span><span className={Number(v) > 0 ? "font-medium" : "text-gray-400"}>{n(v)}</span></li>
            ))}
          </ul>
        </div>
        <div className="rounded border border-gray-200 bg-white p-3">
          <h3 className="mb-2 font-semibold">Parts by your cost</h3>
          <ul className="space-y-1 text-xs">
            {d.price_bands.map((b) => <li key={b.band} className="flex justify-between"><span>{b.band}</span><span>{n(b.parts)}</span></li>)}
          </ul>
        </div>
        <div className="rounded border border-gray-200 bg-white p-3">
          <h3 className="mb-2 font-semibold">Units of measure</h3>
          <ul className="space-y-1 text-xs">
            {d.units_of_measure.map((u) => <li key={u.uom} className="flex justify-between"><span>{u.uom}</span><span>{n(u.parts)}</span></li>)}
          </ul>
        </div>
      </div>
      <div className="rounded border border-gray-200 bg-white p-3">
        <h3 className="mb-2 font-semibold">Brands ({d.brands.length})</h3>
        <div className="max-h-[28rem] overflow-auto">
          <table className="w-full text-left text-xs">
            <thead className="sticky top-0 bg-white text-gray-500">
              <tr><th className="py-1 pr-2">Brand</th><th className="pr-2 text-right">Parts</th><th className="pr-2 text-right">In stock</th><th className="pr-2 text-right">Units</th><th className="pr-2 text-right">Stock at cost</th><th className="pr-2 text-right">Cheapest</th><th className="pr-2 text-right">Average</th><th className="text-right">Dearest</th></tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {d.brands.map((b) => (
                <tr key={b.brand}>
                  <td className={`py-0.5 pr-2 ${b.unbranded ? "text-gray-400" : ""}`}>{b.brand}</td>
                  <td className="pr-2 text-right">{n(b.parts)}</td><td className="pr-2 text-right">{n(b.in_stock)}</td><td className="pr-2 text-right">{n(b.units)}</td>
                  <td className="pr-2 text-right">{money(b.stock_value_at_cost)}</td><td className="pr-2 text-right">{money(b.min_cost)}</td>
                  <td className="pr-2 text-right">{money(b.avg_cost)}</td><td className="text-right">{money(b.max_cost)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
