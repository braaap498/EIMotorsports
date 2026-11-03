"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Notice, btn2Cls, inputCls } from "../../../components/admin/ui";
import ImportPanel, { type StoreInfo } from "../../../components/admin/analytics/ImportPanel";
import QuickBooksSection from "../../../components/admin/analytics/QuickBooksSection";
import InsightsSection, { useInsights, type InsightsView } from "../../../components/admin/analytics/InsightsSection";
import {
  Card, Columns, Donut, Empty, HBars, Kpi, Legend, LineChart, PALETTE, StackedBars, TileMap,
  compactMoney, money0, money2, monthLabel, num0, pct, type BarRow, type Series,
} from "../../../components/admin/analytics/charts";

/* ------------------------------------------------------------------ types */
type Kpis = {
  orders: number; units: number; gross_sales: number; net_sales: number; refunds: number; items: number; shipping: number;
  tax: number; buyer_paid: number; buyers: number; refunded_orders: number; cancelled_orders: number; intl_orders: number;
  avg_days_to_ship: number | null; shipped_fast: number; shipped_known: number; first_date: string | null; last_date: string | null;
};
type Monthly = { month: string; store: string; orders: number; units: number; gross_sales: number; net_sales: number; refunds: number; shipping: number };
type ByStore = { store: string; orders: number; units: number; gross_sales: number; net_sales: number; refunds: number; shipping: number; buyers: number };
type Item = { item_number: string; title: string | null; custom_label: string | null; units: number; revenue: number; orders: number; stores: string };
type Summary = {
  kpi: Kpis; by_store: ByStore[]; monthly: Monthly[];
  weekday: { dow: number; orders: number; net_sales: number }[];
  states: { state: string; orders: number; net_sales: number }[];
  countries: { country: string; orders: number; net_sales: number }[];
  shipping: { service: string; orders: number; shipping_charged: number; net_sales: number }[];
  promoted: { source: string; orders: number; net_sales: number }[];
  order_sizes: { bucket: string; orders: number }[];
  repeat: { band: string; sort: number; buyers: number; net_sales: number }[];
  feedback: { feedback: string; orders: number }[];
  top_revenue: Item[]; top_units: Item[];
};

type Preset = "all" | "ytd" | "last_year" | "l12m" | "l90" | "custom";
const PRESETS: { key: Preset; label: string }[] = [
  { key: "all", label: "All time" },
  { key: "ytd", label: "This year" },
  { key: "last_year", label: "Last year" },
  { key: "l12m", label: "Last 12 months" },
  { key: "l90", label: "Last 90 days" },
  { key: "custom", label: "Custom" },
];

const iso = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

function rangeFor(p: Preset, from: string, to: string): { start?: string; end?: string } {
  const now = new Date();
  const y = now.getFullYear();
  if (p === "ytd") return { start: `${y}-01-01`, end: iso(now) };
  if (p === "last_year") return { start: `${y - 1}-01-01`, end: `${y - 1}-12-31` };
  if (p === "l12m") { const d = new Date(now); d.setFullYear(y - 1); d.setDate(d.getDate() + 1); return { start: iso(d), end: iso(now) }; }
  if (p === "l90") { const d = new Date(now); d.setDate(d.getDate() - 89); return { start: iso(d), end: iso(now) }; }
  if (p === "custom") return { start: from || undefined, end: to || undefined };
  return {};
}

type Tab = "ebay" | "quickbooks";
type Sub = "overview" | InsightsView;
const SUBS: { key: Sub; label: string; hint: string }[] = [
  { key: "overview", label: "Overview", hint: "Sales, orders, products, places" },
  { key: "odd", label: "Odd patterns", hint: "Recent drops, gaps, spikes" },
  { key: "season", label: "Seasonality & inventory", hint: "What sells when, what is growing or fading" },
  { key: "buyers", label: "Buyers & patterns", hint: "Repeat buying, baskets, rhythms" },
];

const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MON3 = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Every month from the first to the last in the data, so a quiet month shows as a gap instead of vanishing.
function monthRange(months: string[]): string[] {
  if (!months.length) return [];
  const sorted = [...months].sort();
  const out: string[] = [];
  let [y, m] = sorted[0].split("-").map(Number);
  const [ey, em] = sorted[sorted.length - 1].split("-").map(Number);
  while (y < ey || (y === ey && m <= em)) {
    out.push(`${y}-${String(m).padStart(2, "0")}`);
    m++;
    if (m > 12) { m = 1; y++; }
  }
  return out;
}

/* ------------------------------------------------------------------ page */
export default function AnalyticsPage() {
  const [stores, setStores] = useState<StoreInfo[]>([]);
  const [tablesReady, setTablesReady] = useState(true);
  const [store, setStore] = useState<string>("all");
  const [preset, setPreset] = useState<Preset>("all");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [data, setData] = useState<Summary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [showImport, setShowImport] = useState(false);
  const [itemBy, setItemBy] = useState<"revenue" | "units">("revenue");
  const [tabState, setTabState] = useState<Tab>("ebay");
  const [connected, setConnected] = useState<string[]>([]);
  const [storesLoaded, setStoresLoaded] = useState(false);
  // The eBay tab only exists while at least one store is connected on the Live listings screen.
  const ebayAvailable = connected.length > 0;
  const tab: Tab = storesLoaded && !ebayAvailable ? "quickbooks" : tabState;
  const [sub, setSub] = useState<Sub>("overview");
  const range = useMemo(() => rangeFor(preset, from, to), [preset, from, to]);

  const loadStores = useCallback(async () => {
    try {
      const r = await api<{ tables_ready: boolean; stores: StoreInfo[]; connected?: string[] }>("/analytics/ebay/stores");
      setTablesReady(r.tables_ready);
      setStores(r.stores);
      setConnected(r.connected ?? r.stores.map((x) => x.store));
      if (r.tables_ready && (r.connected ?? r.stores).length > 0 && r.stores.length === 0) setShowImport(true);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setStoresLoaded(true);
    }
  }, []);

  const loadSummary = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const { start, end } = rangeFor(preset, from, to);
      const q = new URLSearchParams();
      if (store !== "all") q.set("store", store);
      if (start) q.set("start", start);
      if (end) q.set("end", end);
      setData(await api<Summary>(`/analytics/ebay/summary?${q.toString()}`));
    } catch (e) {
      setData(null);
      setError(errMsg(e));
    }
    setLoading(false);
  }, [store, preset, from, to]);

  // Remember the tab in the address (#ebay, #ebay/season, #quickbooks) so a reload or a shared link lands in the same place.
  useEffect(() => {
    const [t, sb] = window.location.hash.replace("#", "").split("/");
    if (t === "quickbooks") setTabState("quickbooks");
    if (sb === "odd" || sb === "season" || sb === "buyers") setSub(sb);
  }, []);
  const go = (t: Tab, sb: Sub) => {
    setTabState(t);
    setSub(sb);
    window.history.replaceState(null, "", `#${t}${t === "ebay" && sb !== "overview" ? `/${sb}` : ""}`);
  };

  useEffect(() => { loadStores(); }, [loadStores]);
  useEffect(() => { if (tablesReady) loadSummary(); else setLoading(false); }, [loadSummary, tablesReady]);

  const storeColor = useMemo(() => {
    const m = new Map<string, string>();
    stores.forEach((s, i) => m.set(s.store, PALETTE[i % PALETTE.length]));
    return m;
  }, [stores]);
  const colorOf = (s: string) => storeColor.get(s) ?? PALETTE[7];
  const insights = useInsights(tab === "ebay" && sub !== "overview" && tablesReady && stores.length > 0, store, range.start, range.end);

  /* ---- derived chart data */
  const view = useMemo(() => {
    if (!data) return null;
    const months = monthRange(data.monthly.map((m) => m.month));
    // The newest month is "month to date" when the last order is before the month's end. Drawing it as a normal
    // month makes a lines chart plunge to nearly zero, so lines leave it out and the bars are labelled instead.
    let partialKey: string | null = null;
    let partialThrough: string | null = null;
    if (data.kpi.last_date) {
      const [yy, mm, dd] = data.kpi.last_date.split("-").map(Number);
      if (dd < new Date(yy, mm, 0).getDate()) { partialKey = `${yy}-${String(mm).padStart(2, "0")}`; partialThrough = data.kpi.last_date; }
    }
    const present = Array.from(new Set(data.monthly.map((m) => m.store))).sort();
    const pick = (store: string, f: (m: Monthly) => number): number[] => {
      const by = new Map(data.monthly.filter((m) => m.store === store).map((m) => [m.month, f(m)]));
      return months.map((mo) => by.get(mo) ?? 0);
    };
    const netSeries: Series[] = present.map((s) => ({ key: s, label: s, color: colorOf(s), values: pick(s, (m) => m.net_sales) }));
    const orderSeries: Series[] = present.map((s) => ({
      key: s, label: s, color: colorOf(s),
      values: pick(s, (m) => m.orders).map((v, i) => (months[i] === partialKey ? (null as unknown as number) : v)),
    }));

    // Year over year: one line per calendar year, Jan to Dec. Months outside the data stay empty.
    const years = Array.from(new Set(months.filter((m) => m !== partialKey).map((m) => m.slice(0, 4)))).sort();
    const totalBy = new Map<string, number>();
    data.monthly.forEach((m) => totalBy.set(m.month, (totalBy.get(m.month) ?? 0) + m.net_sales));
    const yoy: Series[] = years.map((yr, i) => {
      const have = months.filter((m) => m.startsWith(yr) && m !== partialKey);
      const first = Number(have[0].slice(5)), last = Number(have[have.length - 1].slice(5));
      return {
        key: yr, label: yr, color: PALETTE[(i + 2) % PALETTE.length],
        values: MON3.map((_, k) => (k + 1 < first || k + 1 > last ? (null as unknown as number) : totalBy.get(`${yr}-${String(k + 1).padStart(2, "0")}`) ?? 0)),
      };
    });
    return { months, netSeries, orderSeries, yoy, years, partialKey, partialThrough };
  }, [data, storeColor]); // eslint-disable-line react-hooks/exhaustive-deps

  const k = data?.kpi;
  const live = k ? k.orders - k.cancelled_orders : 0;
  const repeatBuyers = data ? data.repeat.filter((r) => r.sort > 1).reduce((a, r) => a + r.buyers, 0) : 0;

  const itemRows: Item[] = data ? (itemBy === "revenue" ? data.top_revenue : data.top_units) : [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Analytics</h1>
          <p className="text-sm text-gray-500">
            {tab === "ebay" ? "What sold, when, and to whom, from the eBay order reports of your stores." : "What you spent and kept, from QuickBooks."}
          </p>
        </div>
        {tab === "ebay" && <button type="button" className={btn2Cls} onClick={() => setShowImport((v) => !v)}>{showImport ? "Hide data import" : "Add / update data"}</button>}
      </div>

      {/* ---- the two top tabs */}
      <div role="tablist" className="flex gap-1 border-b border-gray-200">
        {([["ebay", "eBay"], ["quickbooks", "QuickBooks"]] as [Tab, string][]).filter(([key]) => key !== "ebay" || !storesLoaded || ebayAvailable).map(([key, label]) => (
          <button
            key={key} type="button" role="tab" aria-selected={tab === key}
            onClick={() => go(key, key === "ebay" ? sub : "overview")}
            className={`-mb-px border-b-2 px-4 py-2 text-sm font-semibold ${tab === key ? "border-black text-black" : "border-transparent text-gray-500 hover:text-black"}`}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === "ebay" && !tablesReady && (
        <Notice kind="error">
          The analytics tables haven’t been created yet. Run <code>analytics_ebay_tables.sql</code> in the Supabase SQL editor, then reload this page.
        </Notice>
      )}
      {tab === "ebay" && error && <Notice kind="error">{error}</Notice>}

      {tab === "ebay" && tablesReady && showImport && <ImportPanel stores={stores} onDone={() => { loadStores(); loadSummary(); }} />}

      {/* ---- filters (the date range applies to both tabs, the store buttons only to eBay) */}
      {(tab === "quickbooks" || (tablesReady && stores.length > 0)) && (
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
            {tab === "ebay" && (
            <div className="flex flex-wrap gap-1 rounded-lg bg-gray-100 p-1">
              {[{ store: "all", label: "All stores" }, ...stores.map((s) => ({ store: s.store, label: s.store }))].map((s) => (
                <button
                  key={s.store}
                  type="button"
                  onClick={() => setStore(s.store)}
                  className={`rounded-md px-3 py-1.5 text-sm font-medium ${store === s.store ? "bg-white text-black shadow-sm" : "text-gray-600 hover:text-black"}`}
                >
                  {s.store !== "all" && <span className="mr-1.5 inline-block h-2 w-2 rounded-full" style={{ background: colorOf(s.store) }} />}
                  {s.label}
                </button>
              ))}
            </div>
            )}
            <div className="flex flex-wrap items-center gap-2">
              <select className={`${inputCls} !w-auto`} value={preset} onChange={(e) => setPreset(e.target.value as Preset)}>
                {PRESETS.map((p) => <option key={p.key} value={p.key}>{p.label}</option>)}
              </select>
              {preset === "custom" && (
                <>
                  <input type="date" className={`${inputCls} !w-auto`} value={from} onChange={(e) => setFrom(e.target.value)} />
                  <span className="text-sm text-gray-500">to</span>
                  <input type="date" className={`${inputCls} !w-auto`} value={to} onChange={(e) => setTo(e.target.value)} />
                </>
              )}
              {tab === "ebay" && k?.first_date && k.last_date && (
                <span className="text-xs text-gray-500">
                  {new Date(k.first_date + "T00:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })} –{" "}
                  {new Date(k.last_date + "T00:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })}
                </span>
              )}
              {tab === "ebay" && (sub === "overview" ? loading : insights.loading) && <span className="text-xs text-gray-400">Loading…</span>}
            </div>
        </div>
      )}

      {tab === "ebay" && tablesReady && stores.length > 0 && (
        <>
          <div className="flex flex-wrap gap-1 rounded-lg bg-gray-100 p-1 text-sm">
            {SUBS.map((x) => (
              <button
                key={x.key} type="button" title={x.hint} onClick={() => go("ebay", x.key)}
                className={`rounded-md px-3 py-1.5 font-medium ${sub === x.key ? "bg-white text-black shadow-sm" : "text-gray-600 hover:text-black"}`}
              >
                {x.label}
              </button>
            ))}
          </div>

          {sub !== "overview" && <InsightsSection view={sub} data={insights.data} loading={insights.loading} error={insights.error} />}

          {sub === "overview" && (
            <>
              {data && k && k.orders === 0 && <Empty text="No orders in this store and date range." />}

              {data && k && k.orders > 0 && view && (
                <div className={loading ? "space-y-4 opacity-60 transition-opacity" : "space-y-4 transition-opacity"}>
                  {/* ---- KPIs */}
                  <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                    <Kpi label="Net sales" value={money0(k.net_sales)} sub={`${money0(k.gross_sales)} before refunds`} accent={PALETTE[0]} />
                    <Kpi label="Orders" value={num0(k.orders)} sub={`${num0(k.units)} units sold`} accent={PALETTE[1]} />
                    <Kpi label="Average order" value={money2(live ? k.net_sales / live : 0)} sub="net sales per paid order" accent={PALETTE[2]} />
                    <Kpi label="Refunded (est.)" value={money0(k.refunds)} sub={`${pct(k.gross_sales ? k.refunds / k.gross_sales : 0)} of sales, ${num0(k.refunded_orders)} orders`} accent={PALETTE[3]} />
                    <Kpi label="Buyers" value={num0(k.buyers)} sub={`${(k.buyers ? k.orders / k.buyers : 0).toFixed(2)} orders each`} />
                    <Kpi label="Repeat buyers" value={pct(k.buyers ? repeatBuyers / k.buyers : 0)} sub={`${num0(repeatBuyers)} bought more than once`} />
                    <Kpi label="Shipping collected" value={money0(k.shipping)} sub={`${pct(k.orders ? k.intl_orders / k.orders : 0)} of orders ship outside the US`} />
                    <Kpi
                      label="Time to ship"
                      value={k.avg_days_to_ship === null ? "—" : `${Number(k.avg_days_to_ship).toFixed(1)} days`}
                      sub={k.shipped_known ? `${pct(k.shipped_fast / k.shipped_known, 0)} ship within a day` : undefined}
                    />
                  </div>

                  {/* ---- trend */}
                  <Card
                    title="Net sales by month"
                    sub={`${store === "all" ? "Stacked by store. Hover a month for the breakdown." : "After estimated refunds, without sales tax."}${view.partialKey ? ` ${monthLabel(view.partialKey)} is month-to-date (through ${view.partialThrough}).` : ""}`}
                  >
                    <StackedBars labels={view.months} series={view.netSeries} />
                    {view.netSeries.length > 1 && <Legend items={view.netSeries.map((s) => ({ label: s.label, color: s.color }))} />}
                  </Card>

                  <div className="grid gap-4 lg:grid-cols-2">
                    <Card title="Orders per month" sub={view.partialKey ? `${monthLabel(view.partialKey)} is month-to-date and left off the lines.` : undefined}>
                      <LineChart labels={view.months.map((m) => `${MON3[Number(m.slice(5)) - 1]} ’${m.slice(2, 4)}`)} series={view.orderSeries} fmt={num0} tipFmt={num0} area={view.orderSeries.length === 1} />
                      {view.orderSeries.length > 1 && <Legend items={view.orderSeries.map((s) => ({ label: s.label, color: s.color }))} />}
                    </Card>
                    <Card title="Year over year" sub={`Net sales per month, one line per year.${view.partialKey ? ` ${monthLabel(view.partialKey)} is month-to-date and left off.` : ""}`}>
                      {view.years.length > 1 ? (
                        <>
                          <LineChart labels={MON3} series={view.yoy} />
                          <Legend items={view.yoy.map((s) => ({ label: s.label, color: s.color }))} />
                        </>
                      ) : (
                        <Empty text="Needs data from more than one calendar year." />
                      )}
                    </Card>
                  </div>

                  {/* ---- store comparison */}
                  {store === "all" && data.by_store.length > 1 && (
                    <Card title="Store comparison">
                      <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_minmax(0,18rem)]">
                        <div className="overflow-x-auto">
                          <table className="w-full min-w-[32rem] text-left text-sm">
                            <thead className="text-xs text-gray-500">
                              <tr>
                                <th className="py-1 pr-3 font-medium">Store</th><th className="pr-3 text-right font-medium">Net sales</th><th className="pr-3 text-right font-medium">Orders</th>
                                <th className="pr-3 text-right font-medium">Avg order</th><th className="pr-3 text-right font-medium">Refunds</th><th className="text-right font-medium">Buyers</th>
                              </tr>
                            </thead>
                            <tbody>
                              {data.by_store.map((s) => (
                                <tr key={s.store} className="border-t border-gray-100">
                                  <td className="py-1.5 pr-3 font-medium"><span className="mr-1.5 inline-block h-2 w-2 rounded-full" style={{ background: colorOf(s.store) }} />{s.store}</td>
                                  <td className="pr-3 text-right tabular-nums">{money0(s.net_sales)}</td>
                                  <td className="pr-3 text-right tabular-nums">{num0(s.orders)}</td>
                                  <td className="pr-3 text-right tabular-nums">{money2(s.orders ? s.net_sales / s.orders : 0)}</td>
                                  <td className="pr-3 text-right tabular-nums">{pct(s.gross_sales ? s.refunds / s.gross_sales : 0)}</td>
                                  <td className="text-right tabular-nums">{num0(s.buyers)}</td>
                                </tr>
                              ))}
                              <tr className="border-t-2 border-gray-200 font-semibold">
                                <td className="py-1.5 pr-3">Both</td>
                                <td className="pr-3 text-right tabular-nums">{money0(k.net_sales)}</td>
                                <td className="pr-3 text-right tabular-nums">{num0(k.orders)}</td>
                                <td className="pr-3 text-right tabular-nums">{money2(live ? k.net_sales / live : 0)}</td>
                                <td className="pr-3 text-right tabular-nums">{pct(k.gross_sales ? k.refunds / k.gross_sales : 0)}</td>
                                <td className="text-right tabular-nums">{num0(k.buyers)}</td>
                              </tr>
                            </tbody>
                          </table>
                          <p className="mt-1 text-[11px] text-gray-400">“Buyers” counts a person once per store, so the two stores can add up to more than the combined figure.</p>
                        </div>
                        <Donut
                          rows={data.by_store.map((s) => ({ label: s.store, value: s.net_sales, color: colorOf(s.store) }))}
                          fmt={money0}
                          center={{ big: compactMoney(k.net_sales), small: "net sales" }}
                        />
                      </div>
                    </Card>
                  )}

                  {/* ---- products */}
                  <Card
                    title={itemBy === "revenue" ? "Top items by sales" : "Top items by units sold"}
                    sub="By eBay item number. Revenue is price times quantity, before refunds."
                  >
                    <div className="mb-2 flex gap-1 text-xs">
                      {(["revenue", "units"] as const).map((v) => (
                        <button key={v} type="button" onClick={() => setItemBy(v)} className={`rounded px-2.5 py-1 font-medium ${itemBy === v ? "bg-black text-white" : "bg-gray-100 text-gray-700 hover:bg-gray-200"}`}>
                          {v === "revenue" ? "By sales" : "By units"}
                        </button>
                      ))}
                    </div>
                    <div className="overflow-x-auto">
                      <table className="w-full min-w-[34rem] text-left text-sm">
                        <thead className="text-xs text-gray-500">
                          <tr><th className="py-1 pr-3 font-medium">Item</th><th className="pr-3 text-right font-medium">Units</th><th className="pr-3 text-right font-medium">Orders</th><th className="text-right font-medium">Sales</th></tr>
                        </thead>
                        <tbody>
                          {itemRows.map((it) => (
                            <tr key={it.item_number} className="border-t border-gray-100 align-top">
                              <td className="py-1.5 pr-3">
                                <a href={`https://www.ebay.com/itm/${it.item_number}`} target="_blank" rel="noreferrer" className="line-clamp-2 hover:underline">{it.title ?? it.item_number}</a>
                                <span className="text-[11px] text-gray-400">{it.item_number}{it.custom_label ? ` · ${it.custom_label}` : ""}{store === "all" ? ` · ${it.stores}` : ""}</span>
                              </td>
                              <td className="pr-3 text-right tabular-nums">{num0(it.units)}</td>
                              <td className="pr-3 text-right tabular-nums">{num0(it.orders)}</td>
                              <td className="text-right font-medium tabular-nums">{money0(it.revenue)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </Card>

                  {/* ---- where */}
                  <div className="grid gap-4 lg:grid-cols-2">
                    <Card title="Where your buyers are" sub="US states by net sales. Hover a tile.">
                      <TileMap data={data.states.map((s) => ({ state: s.state, value: s.net_sales, orders: s.orders }))} />
                    </Card>
                    <div className="grid gap-4">
                      <Card title="Top states">
                        <HBars labelWidth="w-10" rows={data.states.slice(0, 8).map((s): BarRow => ({ label: s.state, value: s.net_sales, sub: `${num0(s.orders)} orders` }))} />
                      </Card>
                      <Card title="Countries" sub="By number of orders.">
                        <HBars labelWidth="w-28" color={PALETTE[4]} fmt={num0} rows={data.countries.slice(0, 6).map((c): BarRow => ({ label: c.country, value: c.orders, sub: money0(c.net_sales) }))} />
                      </Card>
                    </div>
                  </div>

                  {/* ---- behaviour */}
                  <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                    <Card title="Orders by day of week">
                      <Columns rows={DOW.map((d, i): BarRow => ({ label: d, value: data.weekday.find((w) => w.dow === i)?.orders ?? 0 }))} color={PALETTE[0]} />
                    </Card>
                    <Card title="Order size" sub="Items plus shipping, before refunds.">
                      <Columns rows={data.order_sizes.map((o): BarRow => ({ label: o.bucket, value: o.orders }))} color={PALETTE[2]} />
                    </Card>
                    <Card title="Promoted Listings vs organic" sub="Orders that came through promoted ads.">
                      <Donut
                        rows={data.promoted.map((p) => ({ label: p.source, value: p.orders, color: p.source === "Promoted Listings" ? PALETTE[1] : p.source === "Organic" ? PALETTE[0] : PALETTE[7] }))}
                      />
                    </Card>
                    <Card title="Shipping services" sub="Orders by service.">
                      <HBars labelWidth="w-36" color={PALETTE[5]} fmt={num0} rows={data.shipping.map((s): BarRow => ({ label: s.service.replace(/^USPS /, ""), value: s.orders, sub: `${money0(s.shipping_charged)} charged` }))} />
                    </Card>
                    <Card title="Repeat buyers" sub="How many orders each buyer placed.">
                      <HBars labelWidth="w-24" color={PALETTE[6]} fmt={num0} rows={data.repeat.map((r): BarRow => ({ label: r.band, value: r.buyers, sub: money0(r.net_sales) }))} />
                    </Card>
                    <Card title="Feedback received">
                      <Donut
                        rows={data.feedback.map((f) => ({
                          label: f.feedback[0].toUpperCase() + f.feedback.slice(1),
                          value: f.orders,
                          color: f.feedback === "positive" ? "#10b981" : f.feedback === "negative" ? "#ef4444" : f.feedback === "neutral" ? "#f59e0b" : "#cbd5e1",
                        }))}
                      />
                    </Card>
                  </div>

                  <p className="pb-4 text-xs text-gray-500">
                    Notes: eBay’s order report has no fee or refund columns. Refunds here are estimated from an order’s Total Price falling short of items + shipping + tax
                    (a $0.00 order is counted as fully refunded or cancelled). Sales tax collected by eBay is left out of every sales figure. Costs, fees and profit are in the QuickBooks section below.
                  </p>
                </div>
              )}
            </>
          )}
        </>
      )}

      {tab === "quickbooks" && <QuickBooksSection start={range.start} end={range.end} />}
    </div>
  );
}
