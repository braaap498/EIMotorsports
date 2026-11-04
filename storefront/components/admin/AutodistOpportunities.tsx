"use client";

import { useEffect, useMemo, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Badge, Notice, inputCls } from "./ui";

/* The "Opportunities" tab: ranks the Automatic Distributors parts that look most worth selling, so effort goes to the ones most
   likely to be profitable. Read only. Selling price is Automatic Distributors' MAP price when it sets one, otherwise list. */

type Opp = {
  id: number;
  sku: string;
  manufacturer: string | null;
  name: string | null;
  image: string | null;
  price: string;
  price_basis: string;
  cost: string;
  margin: string;
  margin_pct: string | number;
  qty_total: number;
  warehouses: number;
  is_ours: boolean;
  special_order: boolean | null;
  ship_weight_lb: string | null;
};
type BrandOpp = {
  manufacturer: string;
  parts: number;
  in_stock_parts: number;
  avg_margin_pct: string | number;
  avg_margin: string | number;
  potential: string | number | null;
};
type SaleRow = {
  product_id: number;
  our_sku: string | null;
  our_title: string | null;
  units: number | null;
  id: number | null;
  sku: string | null;
  manufacturer: string | null;
  image: string | null;
  cost: string | null;
  price: string | null;
  price_basis: string | null;
  margin: string | null;
  margin_pct: string | number | null;
  qty_total: number | null;
};
type Data = {
  totals: { with_margin: number; after_filters: number; qualifying: number };
  lists: { best_bets: Opp[]; best_margin: Opp[]; best_margin_pct: Opp[] };
  brands: BrandOpp[];
  sales: { table: string; rows: SaleRow[] } | null;
};

const n = (v: number | string | null | undefined) => (v == null || v === "" ? "—" : Number(v).toLocaleString());
const money = (v: string | number | null | undefined) =>
  v == null || v === "" ? "—" : Number(v).toLocaleString(undefined, { style: "currency", currency: "USD" });
const money0 = (v: string | number | null | undefined) =>
  v == null || v === "" ? "—" : Number(v).toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const pct = (v: string | number | null | undefined) => (v == null || v === "" ? "—" : `${Number(v).toFixed(1)}%`);

type View = "best_bets" | "best_margin" | "best_margin_pct" | "brands" | "sales";
const VIEWS: { key: View; label: string; help: string }[] = [
  {
    key: "best_bets",
    label: "Best bets",
    help: "Ranked by profit per unit, weighted by how much Automatic Distributors has in stock (full weight at 10 or more units), so it favors parts you can actually fill orders for.",
  },
  { key: "best_margin", label: "Best margin $", help: "The most profit per unit sold." },
  { key: "best_margin_pct", label: "Best margin %", help: "The highest margin as a percent of the selling price (still at least your minimum dollars per unit)." },
  {
    key: "brands",
    label: "Brands to focus on",
    help: "Brands ranked by profit potential: for each qualifying part, its profit per unit times the units in stock (up to 10), added up. Brands with fewer than 3 qualifying parts are left out.",
  },
  { key: "sales", label: "Sells for us", help: "Parts we have already sold, from our own eBay sales, most units first, with what they would earn at Automatic Distributors' price today." },
];

function Thumb({ url }: { url: string | null }) {
  if (!url) return <div className="h-9 w-9" />;
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={url}
      alt=""
      loading="lazy"
      referrerPolicy="no-referrer"
      className="h-9 w-9 rounded border border-gray-100 bg-white object-contain"
      onError={(e) => {
        (e.currentTarget as HTMLImageElement).style.visibility = "hidden";
      }}
    />
  );
}

export default function AutodistOpportunities({ hasData, onFind }: { hasData: boolean; onFind: (sku: string) => void }) {
  const [minPct, setMinPct] = useState("20");
  const [maxPct, setMaxPct] = useState("80");
  const [minDollars, setMinDollars] = useState("5");
  const [brand, setBrand] = useState("");
  const [inStock, setInStock] = useState(true);
  const [hideSpecial, setHideSpecial] = useState(true);
  const [ours, setOurs] = useState("all");
  const [limit, setLimit] = useState("25");
  const [view, setView] = useState<View>("best_bets");
  const [data, setData] = useState<Data | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const qs = useMemo(() => {
    const p = new URLSearchParams();
    const num = (k: string, v: string) => {
      if (v.trim() !== "" && !Number.isNaN(Number(v))) p.set(k, v.trim());
    };
    num("min_margin_pct", minPct);
    num("max_margin_pct", maxPct);
    num("min_margin", minDollars);
    if (brand.trim()) p.set("brand", brand.trim());
    p.set("in_stock", String(inStock));
    p.set("hide_special_order", String(hideSpecial));
    p.set("ours", ours);
    p.set("limit", limit);
    return p.toString();
  }, [minPct, maxPct, minDollars, brand, inStock, hideSpecial, ours, limit]);

  useEffect(() => {
    if (!hasData) return;
    let cancelled = false;
    const t = setTimeout(() => {
      setLoading(true);
      api<Data>(`/autodist/opportunities?${qs}`)
        .then((d) => {
          if (!cancelled) {
            setData(d);
            setErr(null);
          }
        })
        .catch((e) => !cancelled && setErr(errMsg(e)))
        .finally(() => !cancelled && setLoading(false));
    }, 500);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [qs, hasData]);

  if (!hasData) return <Notice kind="info">Import the Automatic Distributors feed first; there is nothing to analyze yet.</Notice>;

  const help = VIEWS.find((v) => v.key === view)?.help;
  const partRows = data && (view === "best_bets" || view === "best_margin" || view === "best_margin_pct") ? data.lists[view] : null;

  return (
    <div className="space-y-4 text-sm">
      <div className="space-y-3 rounded border border-gray-200 bg-white p-4">
        <p className="text-xs text-gray-600">
          Where to focus first. Margin uses Automatic Distributors’ MAP price as the selling price when there is one, otherwise the list price, against your own cost. Automatic Distributors’ feed has no
          popularity data, so “popular” here means what has sold for us (see “Sells for us”).
        </p>
        <div className="flex flex-wrap items-end gap-x-4 gap-y-2 text-xs">
          <label className="block">
            <span className="text-gray-500">Min margin %</span>
            <input className={`${inputCls} !w-20`} inputMode="decimal" value={minPct} onChange={(e) => setMinPct(e.target.value)} />
          </label>
          <label className="block" title="Margins above this are usually a placeholder list price, not a real margin">
            <span className="text-gray-500">Max margin %</span>
            <input className={`${inputCls} !w-20`} inputMode="decimal" value={maxPct} onChange={(e) => setMaxPct(e.target.value)} />
          </label>
          <label className="block">
            <span className="text-gray-500">Min $ per unit</span>
            <input className={`${inputCls} !w-20`} inputMode="decimal" value={minDollars} onChange={(e) => setMinDollars(e.target.value)} />
          </label>
          <label className="block">
            <span className="text-gray-500">Brand</span>
            <input className={`${inputCls} !w-40`} value={brand} placeholder="any" onChange={(e) => setBrand(e.target.value)} />
          </label>
          <label className="block">
            <span className="text-gray-500">Parts</span>
            <select className={`${inputCls} !w-auto`} value={ours} onChange={(e) => setOurs(e.target.value)}>
              <option value="all">All</option>
              <option value="no">Not selling yet</option>
              <option value="yes">Already selling</option>
            </select>
          </label>
          <label className="block">
            <span className="text-gray-500">Show</span>
            <select className={`${inputCls} !w-auto`} value={limit} onChange={(e) => setLimit(e.target.value)}>
              <option value="25">Top 25</option>
              <option value="50">Top 50</option>
              <option value="100">Top 100</option>
            </select>
          </label>
          <label className="flex items-center gap-1.5 pb-1.5">
            <input type="checkbox" checked={inStock} onChange={(e) => setInStock(e.target.checked)} />
            <span>In stock at Automatic Distributors</span>
          </label>
          <label className="flex items-center gap-1.5 pb-1.5">
            <input type="checkbox" checked={hideSpecial} onChange={(e) => setHideSpecial(e.target.checked)} />
            <span>Hide special order</span>
          </label>
          {loading && <span className="pb-1.5 text-gray-500">Working…</span>}
        </div>
        {data && (
          <div className="text-xs text-gray-500">
            <b className="text-gray-800">{n(data.totals.qualifying)}</b> parts meet your margin settings ({n(data.totals.after_filters)} after the stock and brand filters, out of{" "}
            {n(data.totals.with_margin)} with a cost and a selling price above it).
          </div>
        )}
      </div>

      {err && <Notice kind="error">{err}</Notice>}

      <div className="rounded border border-gray-200 bg-white">
        <div className="flex flex-wrap gap-1 border-b border-gray-200 px-3 pt-2">
          {VIEWS.map((v) => (
            <button
              key={v.key}
              type="button"
              onClick={() => setView(v.key)}
              className={`rounded-t px-3 py-1.5 text-sm ${view === v.key ? "border border-b-white border-gray-200 bg-white font-medium -mb-px" : "text-gray-600 hover:text-gray-900"}`}
            >
              {v.label}
            </button>
          ))}
        </div>
        <div className="space-y-2 p-3">
          {help && <p className="text-xs text-gray-500">{help}</p>}

          {partRows && (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="text-gray-500">
                  <tr>
                    <th className="py-1 pr-2"></th>
                    <th className="whitespace-nowrap py-1 pr-3">Brand</th>
                    <th className="whitespace-nowrap py-1 pr-3">SKU</th>
                    <th className="py-1 pr-3">Name</th>
                    <th className="whitespace-nowrap py-1 pr-3 text-right">Price</th>
                    <th className="whitespace-nowrap py-1 pr-3 text-right">Cost</th>
                    <th className="whitespace-nowrap py-1 pr-3 text-right">Margin $</th>
                    <th className="whitespace-nowrap py-1 pr-3 text-right">Margin %</th>
                    <th className="whitespace-nowrap py-1 pr-3 text-right">In stock</th>
                    <th className="whitespace-nowrap py-1">Ours</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {partRows.map((r) => (
                    <tr key={r.id} className="cursor-pointer align-middle hover:bg-gray-50" onClick={() => onFind(r.sku)} title="Open in the catalog">
                      <td className="py-1 pr-2">
                        <Thumb url={r.image} />
                      </td>
                      <td className="whitespace-nowrap py-1 pr-3">{r.manufacturer ?? "—"}</td>
                      <td className="whitespace-nowrap py-1 pr-3 font-medium">{r.sku}</td>
                      <td className="min-w-[16rem] py-1 pr-3">{r.name ?? "—"}</td>
                      <td className="whitespace-nowrap py-1 pr-3 text-right">
                        {money(r.price)} <span className="text-gray-400">{r.price_basis}</span>
                      </td>
                      <td className="whitespace-nowrap py-1 pr-3 text-right">{money(r.cost)}</td>
                      <td className="whitespace-nowrap py-1 pr-3 text-right font-medium">{money(r.margin)}</td>
                      <td className="whitespace-nowrap py-1 pr-3 text-right">{pct(r.margin_pct)}</td>
                      <td className="whitespace-nowrap py-1 pr-3 text-right">
                        {n(r.qty_total)} <span className="text-gray-400">({r.warehouses} wh)</span>
                      </td>
                      <td className="whitespace-nowrap py-1">{r.is_ours ? <Badge tone="green">we sell</Badge> : ""}</td>
                    </tr>
                  ))}
                  {partRows.length === 0 && (
                    <tr>
                      <td colSpan={10} className="py-4 text-center text-gray-500">
                        Nothing meets these settings. Loosen the margin or stock filters.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          )}

          {view === "brands" && data && (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="text-gray-500">
                  <tr>
                    <th className="py-1 pr-3">Brand</th>
                    <th className="py-1 pr-3 text-right">Qualifying parts</th>
                    <th className="py-1 pr-3 text-right">In stock</th>
                    <th className="py-1 pr-3 text-right">Avg margin %</th>
                    <th className="py-1 pr-3 text-right">Avg margin $</th>
                    <th className="py-1 text-right">Profit potential</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {data.brands.map((b) => (
                    <tr key={b.manufacturer} className="cursor-pointer hover:bg-gray-50" onClick={() => setBrand(b.manufacturer)} title="Show only this brand">
                      <td className="py-1 pr-3 font-medium">{b.manufacturer}</td>
                      <td className="py-1 pr-3 text-right">{n(b.parts)}</td>
                      <td className="py-1 pr-3 text-right">{n(b.in_stock_parts)}</td>
                      <td className="py-1 pr-3 text-right">{pct(b.avg_margin_pct)}</td>
                      <td className="py-1 pr-3 text-right">{money(b.avg_margin)}</td>
                      <td className="py-1 text-right font-medium">{money0(b.potential)}</td>
                    </tr>
                  ))}
                  {data.brands.length === 0 && (
                    <tr>
                      <td colSpan={6} className="py-4 text-center text-gray-500">
                        No brand has 3 or more qualifying parts with these settings.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          )}

          {view === "sales" && data && (
            <>
              {data.sales == null ? (
                <Notice kind="info">
                  Our sales could not be read from the database, so there is no sales ranking yet. The other tabs still work. If eBay sales are being recorded, tell me the name of the table that holds them.
                </Notice>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs">
                    <thead className="text-gray-500">
                      <tr>
                        <th className="py-1 pr-2"></th>
                        <th className="py-1 pr-3">Our product</th>
                        <th className="py-1 pr-3 text-right">Units sold</th>
                        <th className="py-1 pr-3">Automatic Distributors part</th>
                        <th className="py-1 pr-3 text-right">Price</th>
                        <th className="py-1 pr-3 text-right">Cost</th>
                        <th className="py-1 pr-3 text-right">Margin $</th>
                        <th className="py-1 pr-3 text-right">Margin %</th>
                        <th className="py-1 text-right">In stock</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-100">
                      {data.sales.rows.map((r) => (
                        <tr key={r.product_id} className={`align-middle ${r.sku ? "cursor-pointer hover:bg-gray-50" : "text-gray-400"}`} onClick={() => r.sku && onFind(r.sku)}>
                          <td className="py-1 pr-2">
                            <Thumb url={r.image} />
                          </td>
                          <td className="min-w-[14rem] py-1 pr-3">
                            <span className="font-medium">{r.our_sku}</span> {r.our_title}
                          </td>
                          <td className="py-1 pr-3 text-right font-medium">{n(r.units)}</td>
                          <td className="whitespace-nowrap py-1 pr-3">{r.sku ? `${r.manufacturer ?? ""} ${r.sku}` : "not carried by Automatic Distributors"}</td>
                          <td className="whitespace-nowrap py-1 pr-3 text-right">{r.price ? `${money(r.price)} ${r.price_basis ?? ""}` : "—"}</td>
                          <td className="whitespace-nowrap py-1 pr-3 text-right">{money(r.cost)}</td>
                          <td className="whitespace-nowrap py-1 pr-3 text-right font-medium">{money(r.margin)}</td>
                          <td className="whitespace-nowrap py-1 pr-3 text-right">{pct(r.margin_pct)}</td>
                          <td className="whitespace-nowrap py-1 text-right">{r.qty_total == null ? "—" : n(r.qty_total)}</td>
                        </tr>
                      ))}
                      {data.sales.rows.length === 0 && (
                        <tr>
                          <td colSpan={9} className="py-4 text-center text-gray-500">
                            No sales are recorded against our products yet.
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                  <p className="mt-2 text-xs text-gray-500">Counts every recorded sale line matched to one of our products; it is not limited to a date range.</p>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
