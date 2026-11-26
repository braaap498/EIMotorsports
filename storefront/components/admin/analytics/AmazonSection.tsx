"use client";

// Amazon tab of the Analytics page: sales, fees and estimated margin from the Amazon order feed.
// Sits beside the eBay tab (order reports) and the QuickBooks tab (costs). Uses the page's date range.
// Data: GET /admin/amazon-analytics/overview (the API refreshes its daily rollup first when it is empty or stale).

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Notice, btn2Cls } from "../ui";
import {
  Card, Empty, HBars, Kpi, Legend, LineChart, StackedBars,
  money0, money2, num0, pct, type BarRow, type Series,
} from "./charts";

// Amazon's orange is this tab's colour (tab underline, KPI accents, charts). Extra accounts get nearby shades.
export const AMAZON_COLOR = "#ff9900";
const ACCOUNT_SHADES = [AMAZON_COLOR, "#b36b00", "#ffc266", "#7a4a00", "#ffd9a3"];
const MON3 = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

type Kpis = {
  orders: number; units: number; revenue: number; refunds: number; fees: number; cost: number; estimated_margin: number;
  lines_without_cost: number; dropship_lines: number; first_date: string | null; last_date: string | null;
};
type Money = { revenue: number; refunds: number; fees: number; cost: number; orders: number; units: number };
type Monthly = Money & { month: string; account: string };
type ByAccount = Money & { account: string };
type Item = { product_id: number | string | null; title: string | null; sku: string | null; units: number; revenue: number; orders: number };
type Overview = { kpi: Kpis; by_account: ByAccount[]; monthly: Monthly[]; top_revenue: Item[]; top_units: Item[]; refreshed_at: string | null };

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

const fmtDate = (d: string) => new Date(d + "T00:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });

export default function AmazonSection({ start, end }: { start?: string; end?: string }) {
  const [data, setData] = useState<Overview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [account, setAccount] = useState("all");
  const [itemBy, setItemBy] = useState<"revenue" | "units">("revenue");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const q = new URLSearchParams();
      if (account !== "all") q.set("account", account);
      if (start) q.set("start", start);
      if (end) q.set("end", end);
      setData(await api<Overview>(`/amazon-analytics/overview?${q.toString()}`));
    } catch (e) {
      setData(null);
      setError(errMsg(e));
    }
    setLoading(false);
  }, [account, start, end]);

  useEffect(() => { load(); }, [load]);

  const accountNames = useMemo(() => (data ? data.by_account.map((a) => a.account).sort() : []), [data]);
  const colorOf = (a: string) => ACCOUNT_SHADES[Math.max(0, accountNames.indexOf(a)) % ACCOUNT_SHADES.length];

  const view = useMemo(() => {
    if (!data) return null;
    const months = monthRange(data.monthly.map((m) => m.month));
    const present = Array.from(new Set(data.monthly.map((m) => m.account))).sort();
    const pick = (acct: string, f: (m: Monthly) => number): number[] => {
      const by = new Map(data.monthly.filter((m) => m.account === acct).map((m) => [m.month, f(m)]));
      return months.map((mo) => by.get(mo) ?? 0);
    };
    const net = (m: Monthly) => m.revenue - m.refunds;
    const netSeries: Series[] = present.map((a) => ({ key: a, label: a, color: colorOf(a), values: pick(a, net) }));
    const orderTotals = months.map((mo) => data.monthly.filter((m) => m.month === mo).reduce((s, m) => s + m.orders, 0));
    const orderSeries: Series[] = [{ key: "orders", label: "Orders", color: AMAZON_COLOR, values: orderTotals }];
    return { months, netSeries, orderSeries };
  }, [data, accountNames]); // eslint-disable-line react-hooks/exhaustive-deps

  const k = data?.kpi;
  const net = k ? k.revenue - k.refunds : 0;
  const items = data ? (itemBy === "revenue" ? data.top_revenue : data.top_units) : [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        {accountNames.length > 1 && (
          <div className="flex flex-wrap gap-1 rounded-lg bg-gray-100 p-1">
            {["all", ...accountNames].map((a) => (
              <button
                key={a} type="button" onClick={() => setAccount(a)}
                className={`rounded-md px-3 py-1.5 text-sm font-medium ${account === a ? "bg-white text-black shadow-sm" : "text-gray-600 hover:text-black"}`}
              >
                {a !== "all" && <span className="mr-1.5 inline-block h-2 w-2 rounded-full" style={{ background: colorOf(a) }} />}
                {a === "all" ? "All accounts" : a}
              </button>
            ))}
          </div>
        )}
        {k?.first_date && k.last_date && <span className="text-xs text-gray-500">{fmtDate(k.first_date)} – {fmtDate(k.last_date)}</span>}
        {loading && <span className="text-xs text-gray-400">Loading…</span>}
        <button type="button" className={`${btn2Cls} ml-auto`} onClick={load} disabled={loading}>Reload</button>
      </div>

      {error && <Notice kind="error">{error}</Notice>}
      {data && k && k.orders === 0 && <Empty text="No Amazon orders in this account and date range." />}

      {data && k && k.orders > 0 && view && (
        <div className={loading ? "space-y-4 opacity-60 transition-opacity" : "space-y-4 transition-opacity"}>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Kpi label="Net sales" value={money0(net)} sub={`${money0(k.revenue)} before refunds`} accent={AMAZON_COLOR} />
            <Kpi label="Orders" value={num0(k.orders)} sub={`${num0(k.units)} units sold`} accent={AMAZON_COLOR} />
            <Kpi label="Average order" value={money2(k.orders ? net / k.orders : 0)} sub="net sales per order" accent={AMAZON_COLOR} />
            <Kpi label="Refunded" value={money0(k.refunds)} sub={`${pct(k.revenue ? k.refunds / k.revenue : 0)} of sales`} accent={AMAZON_COLOR} />
            <Kpi label="Amazon fees" value={money0(k.fees)} sub={`${pct(k.revenue ? k.fees / k.revenue : 0)} of sales (when Amazon reports them)`} />
            <Kpi label="Product cost" value={money0(k.cost)} sub={k.lines_without_cost ? `${num0(k.lines_without_cost)} lines have no cost yet` : "from product costs"} />
            <Kpi label="Estimated margin" value={money0(k.estimated_margin)} sub={`${pct(k.revenue ? k.estimated_margin / k.revenue : 0)} of sales`} />
            <Kpi label="Dropship lines" value={num0(k.dropship_lines)} sub="order lines fulfilled by a distributor" />
          </div>

          <Card title="Net sales by month" sub={account === "all" && view.netSeries.length > 1 ? "Stacked by account, after refunds. Hover a month for the breakdown." : "After refunds."}>
            <StackedBars labels={view.months} series={view.netSeries} />
            {view.netSeries.length > 1 && <Legend items={view.netSeries.map((s) => ({ label: s.label, color: s.color }))} />}
          </Card>

          <div className="grid gap-4 lg:grid-cols-2">
            <Card title="Orders per month">
              <LineChart
                labels={view.months.map((m) => `${MON3[Number(m.slice(5)) - 1]} ’${m.slice(2, 4)}`)}
                series={view.orderSeries} fmt={num0} tipFmt={num0} area
              />
            </Card>

            <Card title="Where the money goes" sub="Share of Amazon sales after refunds, fees and product cost.">
              <HBars
                labelWidth="w-28" color={AMAZON_COLOR} fmt={money0}
                rows={[
                  { label: "Refunds", value: k.refunds, sub: pct(k.revenue ? k.refunds / k.revenue : 0) },
                  { label: "Amazon fees", value: k.fees, sub: pct(k.revenue ? k.fees / k.revenue : 0) },
                  { label: "Product cost", value: k.cost, sub: pct(k.revenue ? k.cost / k.revenue : 0) },
                  { label: "Estimated margin", value: Math.max(k.estimated_margin, 0), sub: pct(k.revenue ? k.estimated_margin / k.revenue : 0) },
                ] as BarRow[]}
              />
            </Card>
          </div>

          {data.by_account.length > 1 && (
            <Card title="By account">
              <div className="overflow-x-auto">
                <table className="w-full min-w-[32rem] text-left text-sm">
                  <thead className="text-xs text-gray-500">
                    <tr><th className="py-1.5 pr-3 font-medium">Account</th><th className="pr-3 text-right font-medium">Net sales</th><th className="pr-3 text-right font-medium">Orders</th><th className="pr-3 text-right font-medium">Avg order</th><th className="text-right font-medium">Fees</th></tr>
                  </thead>
                  <tbody>
                    {data.by_account.map((a) => (
                      <tr key={a.account} className="border-t border-gray-100">
                        <td className="py-1.5 pr-3 font-medium"><span className="mr-1.5 inline-block h-2 w-2 rounded-full" style={{ background: colorOf(a.account) }} />{a.account}</td>
                        <td className="pr-3 text-right tabular-nums">{money0(a.revenue - a.refunds)}</td>
                        <td className="pr-3 text-right tabular-nums">{num0(a.orders)}</td>
                        <td className="pr-3 text-right tabular-nums">{money2(a.orders ? (a.revenue - a.refunds) / a.orders : 0)}</td>
                        <td className="text-right tabular-nums">{money0(a.fees)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          )}

          <Card title={itemBy === "revenue" ? "Top items by sales" : "Top items by units sold"} sub="Price times quantity, before refunds.">
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
                  <tr><th className="py-1.5 pr-3 font-medium">Product</th><th className="pr-3 text-right font-medium">Units</th><th className="pr-3 text-right font-medium">Orders</th><th className="text-right font-medium">Revenue</th></tr>
                </thead>
                <tbody>
                  {items.map((it, i) => (
                    <tr key={`${it.product_id ?? it.sku ?? i}`} className="border-t border-gray-100">
                      <td className="py-1.5 pr-3"><span className="line-clamp-2">{it.title ?? it.sku ?? "Unknown item"}</span>{it.sku && <span className="text-xs text-gray-400">{it.sku}</span>}</td>
                      <td className="pr-3 text-right tabular-nums">{num0(it.units)}</td>
                      <td className="pr-3 text-right tabular-nums">{num0(it.orders)}</td>
                      <td className="text-right font-medium tabular-nums">{money0(it.revenue)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          <p className="text-xs text-gray-500">
            Notes: figures come from Amazon&rsquo;s order feed (cancelled orders left out). Fees are included only where Amazon reported them, and margin is an estimate
            (sales &minus; refunds &minus; fees &minus; product cost); lines without a product cost count in sales but not in cost.
            {data.refreshed_at ? ` Rollup refreshed ${new Date(data.refreshed_at).toLocaleString("en-US")}.` : ""}
          </p>
        </div>
      )}
    </div>
  );
}
