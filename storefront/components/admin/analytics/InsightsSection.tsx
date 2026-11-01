"use client";

// eBay "insights" views: odd patterns, seasonality and inventory, buyer behaviour.
// Data comes from GET /analytics/ebay/insights (see app/analytics_insights.py and app/insights_calc.py).
// Part type, vehicle type, make and brand are guessed from item titles, so every table is a trend, not a catalogue.
import { useEffect, useMemo, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Notice } from "../ui";
import {
  Card, Columns, Empty, HBars, Heatmap, Kpi, Legend, LineChart, PALETTE, StackedBars,
  money0, money2, monthLabel, num0, pct, type BarRow, type Series,
} from "./charts";

/* ------------------------------------------------------------------ types */
type Idx = (number | null)[];
type SeasonRow = { key: string; units: number; index: Idx; peak_month: string | null; peak_index: number | null; low_month: string | null; low_index: number | null; repeat: number | null };
type Comp = { key: string; now: number; before: number; change: number | null };
type MonthRow = {
  month: string; orders: number; net_sales: number; active_days: number; days_in_month: number; orders_per_active_day: number; aov: number;
  complete: boolean; orders_yoy: number | null; net_yoy: number | null;
};
type ItemRef = { item_number: string; title: string };
export type Insights =
  | { ok: false; reason: string }
  | {
      ok: true;
      coverage: { first_date: string; last_date: string; months: number; complete_months: number; partial_month: { month: string; through: string; days: number; days_in_month: number } | null; first_month_partial: boolean };
      months: MonthRow[];
      gaps: { start: string; end: string; days: number }[];
      lines: number;
      classification?: { units: number; unknown_part_share: number; unknown_type_share: number; unknown_make_share: number };
      seasonality?: { overall: SeasonRow[]; by_part: SeasonRow[]; by_type: SeasonRow[]; by_make: SeasonRow[] };
      units_by_month?: { part: { months: string[]; series: { key: string; values: number[] }[] }; type: { months: string[]; series: { key: string; values: number[] }[] } };
      diagnosis?: {
        month: string; orders_change: number; baseline: string; baseline_months: string[];
        by_type: Comp[]; by_part: Comp[]; by_store: Comp[]; uniform: boolean;
        top_items: (ItemRef & { before: number; now: number; share_of_loss: number })[]; daily: { date: string; orders: number }[];
      } | null;
      movers?: {
        window: { from: string; to: string; has_prior_year: boolean }; by_part: Comp[]; by_type: Comp[];
        stopped: (ItemRef & { units_before: number })[]; risers: (ItemRef & { now: number; before: number })[];
      };
      concentration?: { items_sold: number; top10_share: number; top50_share: number; items_for_80pct: number; sold_once_share: number };
      spikes?: (ItemRef & { month: string; units: number; typical: number })[];
      tables?: { make: Tbl[]; brand: Tbl[]; part: Tbl[]; type: Tbl[] };
      geo?: { type: string; orders: number; states: { state: string; orders: number; share: number; overall_share: number; lq: number }[] }[];
      buyers?: {
        orders: number; buyers: number; lines_per_order: { label: string; count: number }[]; qty_per_line: { label: string; count: number }[];
        pairs: { a: string; b: string; orders: number; lift: number }[];
        repeat: {
          buyers_with_repeat: number; share: number; median_gap_days: number | null; gap_hist: { label: string; count: number }[]; pairs_total: number;
          same_make_share: number | null; same_type_share: number | null; transitions: { from: string; to: string; count: number }[];
        };
        power: { buyers: number; share_of_buyers: number; share_of_revenue: number; top: { who: string; orders: number; revenue: number; avg_order: number; state: string | null; first: string; last: string; stores: string[]; buys: string[]; make: string | null }[] };
        day_of_month: { day: number; index: number | null }[]; weekend_share: number;
      };
    };
type Tbl = { key: string; units: number; revenue: number; avg_price: number };

export type InsightsView = "odd" | "season" | "buyers";

/* ------------------------------------------------------------------ data hook */
export function useInsights(enabled: boolean, store: string, start?: string, end?: string) {
  const [data, setData] = useState<Insights | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError("");
      try {
        const q = new URLSearchParams();
        if (store !== "all") q.set("store", store);
        if (start) q.set("start", start);
        if (end) q.set("end", end);
        const r = await api<Insights>(`/analytics/ebay/insights?${q.toString()}`);
        if (!cancelled) setData(r);
      } catch (e) {
        if (!cancelled) { setData(null); setError(errMsg(e)); }
      }
      if (!cancelled) setLoading(false);
    })();
    return () => { cancelled = true; };
  }, [enabled, store, start, end]);
  return { data, loading, error };
}

/* ------------------------------------------------------------------ helpers */
const dayLabel = (iso: string) => new Date(iso + "T00:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" });
const dayLabelY = (iso: string) => new Date(iso + "T00:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
const chg = (c: number | null) => (c === null ? "new" : `${c > 0 ? "+" : ""}${Math.round(c * 100)}%`);
const chgCls = (c: number | null) => (c === null ? "text-gray-400" : c <= -0.25 ? "font-medium text-red-600" : c >= 0.25 ? "font-medium text-emerald-600" : "text-gray-600");
const seasonRight = (r: SeasonRow) =>
  `peak ${r.peak_month} ${r.peak_index?.toFixed(1)}x${r.repeat === null ? "" : r.repeat >= 0.6 ? " · repeats" : r.repeat >= 0.3 ? " · weakly repeats" : " · did not repeat"}`;

function ItemLink({ item }: { item: ItemRef }) {
  return (
    <a href={`https://www.ebay.com/itm/${item.item_number}`} target="_blank" rel="noreferrer" className="line-clamp-2 hover:underline">
      {item.title || item.item_number}
    </a>
  );
}

function CompTable({ rows, nowLabel, beforeLabel }: { rows: Comp[]; nowLabel: string; beforeLabel: string }) {
  if (!rows.length) return <Empty text="Not enough data." />;
  return (
    <table className="w-full text-left text-sm">
      <thead className="text-xs text-gray-500">
        <tr><th className="py-1 pr-3 font-medium" /><th className="pr-3 text-right font-medium">{beforeLabel}</th><th className="pr-3 text-right font-medium">{nowLabel}</th><th className="text-right font-medium">Change</th></tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.key} className="border-t border-gray-100">
            <td className="py-1 pr-3">{r.key}</td>
            <td className="pr-3 text-right tabular-nums">{num0(r.before)}</td>
            <td className="pr-3 text-right tabular-nums">{num0(r.now)}</td>
            <td className={`text-right tabular-nums ${chgCls(r.change)}`}>{chg(r.change)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function MixTable({ rows, label }: { rows: Tbl[]; label: string }) {
  return (
    <table className="w-full text-left text-sm">
      <thead className="text-xs text-gray-500">
        <tr><th className="py-1 pr-3 font-medium">{label}</th><th className="pr-3 text-right font-medium">Units</th><th className="pr-3 text-right font-medium">Avg price</th><th className="text-right font-medium">Sales</th></tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.key} className="border-t border-gray-100">
            <td className="py-1 pr-3">{r.key}</td>
            <td className="pr-3 text-right tabular-nums">{num0(r.units)}</td>
            <td className="pr-3 text-right tabular-nums">{money2(r.avg_price)}</td>
            <td className="text-right font-medium tabular-nums">{money0(r.revenue)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/* ------------------------------------------------------------------ view switch */
export default function InsightsSection({ view, data, loading, error }: { view: InsightsView; data: Insights | null; loading: boolean; error: string }) {
  return (
    <div className={loading ? "space-y-4 opacity-60 transition-opacity" : "space-y-4 transition-opacity"}>
      {loading && !data && <p className="text-sm text-gray-400">Crunching the order history…</p>}
      {error && <Notice kind="error">{error}</Notice>}
      {data && !data.ok && <Notice>{data.reason}</Notice>}
      {data && data.ok && (
        <>
          {view === "odd" && <Odd d={data} />}
          {view === "season" && <Season d={data} />}
          {view === "buyers" && <Buyers d={data} />}
          <p className="pb-4 text-xs text-gray-500">
            Part type, vehicle type, make and brand are guessed from item titles with keyword rules, so treat them as trends rather than a catalogue
            {data.classification ? ` (${pct(data.classification.unknown_type_share, 0)} of units could not be given a vehicle type, ${pct(data.classification.unknown_part_share, 0)} no part type)` : ""}.
            Cancelled and fully refunded orders are left out. Based on {num0(data.lines)} order lines.
          </p>
        </>
      )}
    </div>
  );
}

type Ok = Extract<Insights, { ok: true }>;

/* ================================================================== ODD PATTERNS */
function Odd({ d }: { d: Ok }) {
  const diag = d.diagnosis;
  const dm = diag ? d.months.find((m) => m.month === diag.month) : undefined;
  const baseM = diag && diag.baseline_months.length === 1 ? d.months.find((m) => m.month === diag.baseline_months[0]) : undefined;
  const flagged = d.months.filter((m) => m.complete && ((m.orders_yoy !== null && Math.abs(m.orders_yoy) >= 0.25) || (m.net_yoy !== null && Math.abs(m.net_yoy) >= 0.25))).map((m) => m.month);
  const p = d.coverage.partial_month;

  return (
    <>
      {p && (
        <Notice>
          <strong>{monthLabel(p.month)} is not a real drop.</strong> The data only runs through {dayLabelY(p.through)} ({p.days} of {p.days_in_month} days), so that month looks like it collapsed to nearly zero.
          The seasonality numbers below leave it out, and the charts on the Overview tab draw it as month-to-date.
        </Notice>
      )}

      {diag && (
        <Card
          title={`The biggest real drop: ${monthLabel(diag.month)}`}
          sub={`Orders were ${Math.abs(Math.round(diag.orders_change * 100))}% ${diag.orders_change < 0 ? "below" : "above"} ${diag.baseline}.`}
        >
          <div className="mb-3 grid grid-cols-2 gap-3 md:grid-cols-4">
            <Kpi label="Orders" value={num0(dm?.orders ?? 0)} sub={baseM ? `${num0(baseM.orders)} a year earlier` : undefined} accent={PALETTE[3]} />
            <Kpi label="Days with a sale" value={dm ? `${dm.active_days} of ${dm.days_in_month}` : "—"} sub="a store that closes shows zero-order days" accent={PALETTE[1]} />
            <Kpi label="Orders per selling day" value={dm ? dm.orders_per_active_day.toFixed(1) : "—"} sub={baseM ? `${baseM.orders_per_active_day.toFixed(1)} a year earlier` : undefined} accent={PALETTE[0]} />
            <Kpi label="Average order" value={dm ? money2(dm.aov) : "—"} sub={baseM ? `${money2(baseM.aov)} a year earlier` : undefined} accent={PALETTE[2]} />
          </div>

          <p className="mb-3 text-sm text-gray-700">
            {diag.uniform
              ? "Almost every part type fell by a similar amount. When the drop is this even, the cause is usually something that touched the whole store (listings ended or paused, vacation / away mode, an account or payment hold, a changed handling time, a promoted-listing switch, an order report that was not fully imported) rather than less demand for one product."
              : "The drop was uneven: some part types fell much further than others. That points to a product or stock cause (stock-outs, ended listings, a supplier problem) more than something that hit the whole store."}
          </p>

          <div className="grid gap-4 lg:grid-cols-3">
            <div><h3 className="mb-1 text-xs font-semibold text-gray-600">By vehicle type (units)</h3><CompTable rows={diag.by_type} beforeLabel="Normal" nowLabel={monthLabel(diag.month)} /></div>
            <div><h3 className="mb-1 text-xs font-semibold text-gray-600">By part type (units)</h3><CompTable rows={diag.by_part} beforeLabel="Normal" nowLabel={monthLabel(diag.month)} /></div>
            <div><h3 className="mb-1 text-xs font-semibold text-gray-600">By store (units)</h3><CompTable rows={diag.by_store} beforeLabel="Normal" nowLabel={monthLabel(diag.month)} />
              <p className="mt-2 text-[11px] text-gray-400">“Normal” is {diag.baseline}.</p>
            </div>
          </div>

          <h3 className="mb-1 mt-4 text-xs font-semibold text-gray-600">Orders per day around it: a cliff on one date means an event, a slow slide means demand</h3>
          <LineChart
            labels={diag.daily.map((x) => dayLabel(x.date))}
            series={[{ key: "o", label: "Orders", color: PALETTE[0], values: diag.daily.map((x) => x.orders) }]}
            fmt={num0} tipFmt={num0} height={200} area
          />

          {diag.top_items.length > 0 && (
            <>
              <h3 className="mb-1 mt-4 text-xs font-semibold text-gray-600">Listings that lost the most units</h3>
              <table className="w-full text-left text-sm">
                <thead className="text-xs text-gray-500"><tr><th className="py-1 pr-3 font-medium">Item</th><th className="pr-3 text-right font-medium">Normal</th><th className="pr-3 text-right font-medium">That month</th><th className="text-right font-medium">Share of the loss</th></tr></thead>
                <tbody>
                  {diag.top_items.map((it) => (
                    <tr key={it.item_number} className="border-t border-gray-100 align-top">
                      <td className="py-1 pr-3"><ItemLink item={it} /></td>
                      <td className="pr-3 text-right tabular-nums">{num0(it.before)}</td>
                      <td className="pr-3 text-right tabular-nums">{num0(it.now)}</td>
                      <td className="text-right tabular-nums">{pct(it.share_of_loss, 0)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </Card>
      )}

      {d.gaps.length > 0 && (
        <Card title="Stretches with no orders at all" sub="Three or more days in a row where not a single order came in, inside the data you have. Worth checking against away mode, outages or a missing export.">
          <ul className="space-y-1 text-sm">
            {d.gaps.map((g) => (
              <li key={g.start} className="flex justify-between border-t border-gray-100 py-1 first:border-t-0">
                <span>{dayLabelY(g.start)} to {dayLabelY(g.end)}</span><span className="font-medium tabular-nums">{g.days} days</span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card title="Month by month against a year earlier" sub="Red or green means orders or sales moved 25% or more against the same month last year.">
        <div className="max-h-96 overflow-auto">
          <table className="w-full min-w-[40rem] text-left text-sm">
            <thead className="sticky top-0 bg-white text-xs text-gray-500">
              <tr>
                <th className="py-1 pr-3 font-medium">Month</th><th className="pr-3 text-right font-medium">Orders</th><th className="pr-3 text-right font-medium">vs last year</th>
                <th className="pr-3 text-right font-medium">Net sales</th><th className="pr-3 text-right font-medium">vs last year</th>
                <th className="pr-3 text-right font-medium">Days with sales</th><th className="pr-3 text-right font-medium">Orders / day</th><th className="text-right font-medium">Avg order</th>
              </tr>
            </thead>
            <tbody>
              {[...d.months].reverse().map((m) => (
                <tr key={m.month} className={`border-t border-gray-100 ${m.complete ? "" : "text-gray-400"}`}>
                  <td className="py-1 pr-3">{monthLabel(m.month)}{!m.complete && <span className="ml-1 text-[10px]">(partial)</span>}</td>
                  <td className="pr-3 text-right tabular-nums">{num0(m.orders)}</td>
                  <td className={`pr-3 text-right tabular-nums ${m.complete ? chgCls(m.orders_yoy) : ""}`}>{m.orders_yoy === null ? "—" : chg(m.orders_yoy)}</td>
                  <td className="pr-3 text-right tabular-nums">{money0(m.net_sales)}</td>
                  <td className={`pr-3 text-right tabular-nums ${m.complete ? chgCls(m.net_yoy) : ""}`}>{m.net_yoy === null ? "—" : chg(m.net_yoy)}</td>
                  <td className="pr-3 text-right tabular-nums">{m.active_days}/{m.days_in_month}</td>
                  <td className="pr-3 text-right tabular-nums">{m.orders_per_active_day.toFixed(1)}</td>
                  <td className="text-right tabular-nums">{money2(m.aov)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {flagged.length > 0 && <p className="mt-2 text-xs text-gray-500">Months that moved 25% or more: {flagged.map(monthLabel).join(", ")}.</p>}
      </Card>

      {d.spikes && d.spikes.length > 0 && (
        <Card title="Listings with a one-off spike" sub="Sold at least three times their usual month, and not at the same time the year before. Often a restock, a price change, a promoted-listing campaign or a viral build.">
          <table className="w-full text-left text-sm">
            <thead className="text-xs text-gray-500"><tr><th className="py-1 pr-3 font-medium">Item</th><th className="pr-3 font-medium">Month</th><th className="pr-3 text-right font-medium">Units</th><th className="text-right font-medium">Usual month</th></tr></thead>
            <tbody>
              {d.spikes.map((s) => (
                <tr key={s.item_number + s.month} className="border-t border-gray-100 align-top">
                  <td className="py-1 pr-3"><ItemLink item={s} /></td>
                  <td className="pr-3 whitespace-nowrap">{monthLabel(s.month)}</td>
                  <td className="pr-3 text-right tabular-nums">{num0(s.units)}</td>
                  <td className="text-right tabular-nums">{num0(s.typical)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </>
  );
}

/* ================================================================== SEASONALITY & INVENTORY */
function Season({ d }: { d: Ok }) {
  const [stackBy, setStackBy] = useState<"part" | "type">("part");
  const s = d.seasonality;
  const takeaways = useMemo(() => {
    if (!s) return [] as string[];
    const rows = [...s.by_type, ...s.by_part].filter((r) => (r.peak_index ?? 0) >= 1.35 && (r.repeat === null || r.repeat >= 0.5));
    return rows.sort((a, b) => (b.peak_index ?? 0) - (a.peak_index ?? 0)).slice(0, 5)
      .map((r) => `${r.key} peaks in ${r.peak_month} (${r.peak_index?.toFixed(1)}x a normal month) and bottoms out in ${r.low_month} (${r.low_index?.toFixed(1)}x).`);
  }, [s]);
  const ub = d.units_by_month ? d.units_by_month[stackBy] : null;
  const series: Series[] = ub ? ub.series.map((x, i) => ({ key: x.key, label: x.key, color: x.key === "Everything else" ? PALETTE[7] : PALETTE[i % 7], values: x.values })) : [];
  const toRows = (rows: SeasonRow[]) => rows.map((r) => ({ label: r.key, values: r.index, right: seasonRight(r) }));
  const mv = d.movers;
  const c = d.concentration;

  return (
    <>
      {!s || s.overall.length === 0 ? (
        <Notice>Not enough complete months yet to talk about seasons. This needs a year or more of orders.</Notice>
      ) : (
        <>
          <Card title="Seasonality" sub="1.0 is a normal month for that row. Indigo is busier than normal, amber is quieter. “Repeats” means the same months were busy in both of the last two years, so it is a real season and not a one-off.">
            {takeaways.length > 0 && (
              <ul className="mb-3 list-disc space-y-0.5 pl-5 text-sm text-gray-700">{takeaways.map((t) => <li key={t}>{t}</li>)}</ul>
            )}
            <h3 className="mb-1 text-xs font-semibold text-gray-600">All parts together</h3>
            <Heatmap rows={toRows(s.overall)} />
            <h3 className="mb-1 mt-4 text-xs font-semibold text-gray-600">By vehicle type</h3>
            <Heatmap rows={toRows(s.by_type)} />
            <h3 className="mb-1 mt-4 text-xs font-semibold text-gray-600">By part type</h3>
            <Heatmap rows={toRows(s.by_part)} />
            <h3 className="mb-1 mt-4 text-xs font-semibold text-gray-600">By make</h3>
            <Heatmap rows={toRows(s.by_make)} note="Each month is averaged over the years you have. With only two years of data one odd month moves a row noticeably." />
          </Card>

          {ub && (
            <Card title="Units sold by month" sub="Units, not dollars. Switch what the colours mean.">
              <div className="mb-2 flex gap-1 text-xs">
                {([["part", "Part type"], ["type", "Vehicle type"]] as const).map(([k, label]) => (
                  <button key={k} type="button" onClick={() => setStackBy(k)} className={`rounded px-2.5 py-1 font-medium ${stackBy === k ? "bg-black text-white" : "bg-gray-100 text-gray-700 hover:bg-gray-200"}`}>{label}</button>
                ))}
              </div>
              <StackedBars labels={ub.months} series={series} fmt={num0} tipFmt={num0} />
              <Legend items={series.map((x) => ({ label: x.label, color: x.color }))} />
            </Card>
          )}
        </>
      )}

      {mv && mv.window.has_prior_year && (
        <Card title="What is growing and what is shrinking" sub={`Units in the last 90 days (${dayLabel(mv.window.from)} to ${dayLabel(mv.window.to)}) against the same 90 days a year earlier, so the season cancels out.`}>
          <div className="grid gap-4 lg:grid-cols-2">
            <div><h3 className="mb-1 text-xs font-semibold text-gray-600">Part types</h3><CompTable rows={mv.by_part} beforeLabel="Last year" nowLabel="Now" /></div>
            <div><h3 className="mb-1 text-xs font-semibold text-gray-600">Vehicle types</h3><CompTable rows={mv.by_type} beforeLabel="Last year" nowLabel="Now" /></div>
          </div>
        </Card>
      )}

      {mv && (mv.stopped.length > 0 || mv.risers.length > 0) && (
        <div className="grid gap-4 lg:grid-cols-2">
          <Card title="Best sellers that went quiet" sub="Sold 12+ units in the year before, and nothing in the last 60 days. Out of stock, ended, or priced out?">
            {mv.stopped.length ? (
              <table className="w-full text-left text-sm">
                <tbody>
                  {mv.stopped.map((it) => (
                    <tr key={it.item_number} className="border-t border-gray-100 align-top first:border-t-0">
                      <td className="py-1 pr-3"><ItemLink item={it} /></td>
                      <td className="text-right tabular-nums text-gray-600">{num0(it.units_before)} units</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : <Empty text="None." />}
          </Card>
          <Card title="Listings taking off" sub="At least double their units from the same 90 days a year ago (10+ units now).">
            {mv.risers.length ? (
              <table className="w-full text-left text-sm">
                <tbody>
                  {mv.risers.map((it) => (
                    <tr key={it.item_number} className="border-t border-gray-100 align-top first:border-t-0">
                      <td className="py-1 pr-3"><ItemLink item={it} /></td>
                      <td className="whitespace-nowrap text-right tabular-nums text-gray-600">{num0(it.before)} → {num0(it.now)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : <Empty text="None." />}
          </Card>
        </div>
      )}

      {c && (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <Kpi label="Listings that sold" value={num0(c.items_sold)} sub="distinct eBay item numbers" accent={PALETTE[0]} />
          <Kpi label="Top 10 listings" value={pct(c.top10_share, 0)} sub="share of sales" accent={PALETTE[1]} />
          <Kpi label="Listings for 80% of sales" value={num0(c.items_for_80pct)} sub={`top 50 make ${pct(c.top50_share, 0)}`} accent={PALETTE[2]} />
          <Kpi label="Sold only once" value={pct(c.sold_once_share, 0)} sub="of listings that sold at all" accent={PALETTE[3]} />
        </div>
      )}

      {d.tables && (
        <div className="grid gap-4 lg:grid-cols-2">
          <Card title="Part types by sales"><MixTable rows={d.tables.part} label="Part type" /></Card>
          <Card title="Vehicle types by sales"><MixTable rows={d.tables.type} label="Vehicle type" /></Card>
          <Card title="Makes by sales"><MixTable rows={d.tables.make} label="Make" /></Card>
          <Card title="Brands by sales" sub="The brand named in the title."><MixTable rows={d.tables.brand} label="Brand" /></Card>
        </div>
      )}
    </>
  );
}

/* ================================================================== BUYERS */
function Buyers({ d }: { d: Ok }) {
  const b = d.buyers;
  if (!b) return <Empty text="No buyer data." />;
  const rp = b.repeat;
  const bars = (rows: { label: string; count: number }[]): BarRow[] => rows.map((r) => ({ label: r.label, value: r.count }));
  return (
    <>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Kpi label="Buyers" value={num0(b.buyers)} sub={`${num0(b.orders)} orders`} accent={PALETTE[0]} />
        <Kpi label="Came back" value={pct(rp.share, 0)} sub={`${num0(rp.buyers_with_repeat)} bought more than once`} accent={PALETTE[1]} />
        <Kpi label="Typical wait to rebuy" value={rp.median_gap_days === null ? "—" : `${Math.round(rp.median_gap_days)} days`} sub="median between a buyer's orders" accent={PALETTE[2]} />
        <Kpi label="Heavy buyers" value={pct(b.power.share_of_revenue, 0)} sub={`of sales from ${num0(b.power.buyers)} buyers with 5+ orders`} accent={PALETTE[3]} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="How long until they buy again" sub="Gap between one order and the same buyer's next one.">
          <Columns rows={bars(rp.gap_hist)} color={PALETTE[1]} />
          {(rp.same_make_share !== null || rp.same_type_share !== null) && (
            <p className="mt-2 text-xs text-gray-600">
              {rp.same_make_share !== null && <>On a return order, {pct(rp.same_make_share, 0)} stay with the same make. </>}
              {rp.same_type_share !== null && <>{pct(rp.same_type_share, 0)} stay with the same type of vehicle.</>}
            </p>
          )}
        </Card>
        <Card title="What they buy next" sub="Main item of one order, then main item of that buyer's next order.">
          {rp.transitions.length ? (
            <table className="w-full text-left text-sm">
              <tbody>
                {rp.transitions.map((t) => (
                  <tr key={t.from + t.to} className="border-t border-gray-100 first:border-t-0">
                    <td className="py-1 pr-2">{t.from}</td><td className="pr-2 text-gray-400">→</td><td className="pr-2">{t.to}</td>
                    <td className="text-right tabular-nums text-gray-600">{num0(t.count)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <Empty text="Not enough repeat buyers." />}
        </Card>
      </div>

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        <Card title="Items per order" sub="Lines in one order."><Columns rows={bars(b.lines_per_order)} color={PALETTE[0]} /></Card>
        <Card title="Quantity per line" sub="How many of one listing a buyer takes."><Columns rows={bars(b.qty_per_line)} color={PALETTE[2]} /></Card>
        <Card title="Bought together" sub="Part types that land in the same order more often than chance (lift above 1).">
          {b.pairs.length ? (
            <table className="w-full text-left text-sm">
              <tbody>
                {b.pairs.map((p) => (
                  <tr key={p.a + p.b} className="border-t border-gray-100 first:border-t-0">
                    <td className="py-1 pr-2">{p.a} + {p.b}</td>
                    <td className="whitespace-nowrap text-right tabular-nums text-gray-600">{num0(p.orders)} · {p.lift.toFixed(1)}x</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <Empty text="Not enough multi-part orders." />}
        </Card>
      </div>

      <Card title="Orders by day of the month" sub={`100 = an average day. Looks for paydays and the start-of-month bump. ${pct(b.weekend_share, 0)} of orders land on a weekend.`}>
        <Columns rows={b.day_of_month.map((x): BarRow => ({ label: String(x.day), value: Math.round((x.index ?? 0) * 100) }))} color={PALETTE[4]} />
      </Card>

      {d.geo && d.geo.length > 0 && (
        <Card title="Where each kind of vehicle sells" sub="States that buy more of that vehicle type than the store's overall mix would predict (2.0x means twice as many as expected).">
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {d.geo.map((g) => (
              <div key={g.type}>
                <h3 className="mb-1 text-xs font-semibold text-gray-600">{g.type} <span className="font-normal text-gray-400">({num0(g.orders)} orders)</span></h3>
                <HBars labelWidth="w-10" color={PALETTE[5]} fmt={(n) => `${n.toFixed(1)}x`} rows={g.states.map((s): BarRow => ({ label: s.state, value: s.lq, sub: `${num0(s.orders)} orders` }))} />
              </div>
            ))}
          </div>
        </Card>
      )}

      {b.power.top.length > 0 && (
        <Card title="Heaviest buyers" sub="Top 10 by revenue among buyers with 5+ orders. Many repeat orders can mean a shop or a reseller.">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-left text-sm">
              <thead>
                <tr className="text-xs text-gray-500">
                  <th className="py-1 pr-3 font-medium">Buyer</th>
                  <th className="pr-3 text-right font-medium">Orders</th>
                  <th className="pr-3 text-right font-medium">Revenue</th>
                  <th className="pr-3 text-right font-medium">Avg order</th>
                  <th className="pr-3 font-medium">State</th>
                  <th className="pr-3 font-medium">First – last order</th>
                  <th className="pr-3 font-medium">Mostly buys</th>
                  <th className="font-medium">Store</th>
                </tr>
              </thead>
              <tbody>
                {b.power.top.map((t, i) => (
                  <tr key={i} className="border-t border-gray-100 align-top">
                    <td className="py-1 pr-3 font-mono text-xs">{t.who}</td>
                    <td className="pr-3 text-right tabular-nums">{num0(t.orders)}</td>
                    <td className="pr-3 text-right tabular-nums">{money0(t.revenue)}</td>
                    <td className="pr-3 text-right tabular-nums">{money0(t.avg_order)}</td>
                    <td className="pr-3">{t.state ?? "—"}</td>
                    <td className="whitespace-nowrap pr-3 text-xs text-gray-600">{t.first} – {t.last}</td>
                    <td className="pr-3 text-xs text-gray-700">{[...t.buys, ...(t.make ? [t.make] : [])].join(", ") || "—"}</td>
                    <td className="text-xs text-gray-600">{t.stores.join(", ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </>
  );
}
