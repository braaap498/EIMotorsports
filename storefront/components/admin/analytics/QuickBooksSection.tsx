"use client";

// QuickBooks section of the Analytics page: profit by month, expenses by category, marketplace fees,
// vendor spend, and a check of the eBay export against what QuickBooks recorded.
// Data comes from GET /analytics/quickbooks/summary (see app/analytics_qb.py for how the numbers are worked out).
import { useEffect, useMemo, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Notice } from "../ui";
import {
  Card, Columns, Donut, Empty, HBars, Kpi, Legend, LineChart, PALETTE, StackedBars,
  compactMoney, money0, monthLabel, num0, pct, type BarRow, type Series,
} from "./charts";

/* ------------------------------------------------------------------ types */
type Coverage = {
  sales_first: string | null; sales_last: string | null; sales_records: number;
  costs_first: string | null; costs_last: string | null; cost_records: number;
};
type SaleRow = { month: string; channel: string; bucket: "product" | "shipping" | "refunds"; amount: number; receipts: number };
type CostRow = { month: string; kind: "inventory" | "cogs" | "opex"; category: string; amount: number; source: "receipt" | "purchase" };
type VendorRow = { vendor: string; amount: number; inventory: number; txns: number };
type QbSummary =
  | { ready: false }
  | { ready: true; empty: true; coverage: Coverage }
  | {
      ready: true; empty: false; coverage: Coverage; sales: SaleRow[]; costs: CostRow[]; vendors: VendorRow[];
      ebay_monthly: { month: string; net_sales: number; orders: number }[];
      left_out: { sales_other: number; purchase_non_costs: number; purchase_non_cost_lines: number };
    };

type SalesSource = "qb" | "ebay";

/* ------------------------------------------------------------------ helpers */
const signedMoney0 = (n: number) => (n < 0 ? `-${money0(-n)}` : money0(n));
const signedCompact = (n: number) => (n < 0 ? `-${compactMoney(-n)}` : compactMoney(n));
const dayLabel = (d: string | null) => (d ? monthLabel(d.slice(0, 7)) : "—");

// Every month from the first to the last given, so a quiet month shows as a gap instead of vanishing.
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

function add(map: Map<string, number>, key: string, v: number) {
  map.set(key, (map.get(key) ?? 0) + v);
}

function niceStep(x: number): number {
  const p = Math.pow(10, Math.floor(Math.log10(x)));
  const f = x / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
}

/* ------------------------------------------------------------------ sales vs costs, with a profit line (can go below zero) */
function ProfitChart({
  labels, sales, costs, salesLabel, smooth = false, selected = null, onPick,
}: { labels: string[]; sales: number[]; costs: number[]; salesLabel: string; smooth?: boolean; selected?: number | null; onPick?: (i: number) => void }) {
  const [hover, setHover] = useState<number | null>(null);
  if (!labels.length) return <Empty />;
  const profit = labels.map((_, i) => sales[i] - costs[i]);
  // The line: each month's profit, or (smoothed) the average of that month and the two before it.
  const line = smooth ? profit.map((_, i) => { const w = profit.slice(Math.max(0, i - 2), i + 1); return w.reduce((a, b) => a + b, 0) / w.length; }) : profit;
  const W = 820, H = 280, L = 56, R = 8, T = 10, B = 34;
  const hi = Math.max(0, ...sales, ...costs, ...line);
  const lo = Math.min(0, ...line, ...profit);
  const step = niceStep((hi - lo) / 4 || 1);
  const top = Math.ceil(hi / step) * step || step;
  const bottom = Math.floor(lo / step) * step;
  const ticks: number[] = [];
  for (let t = bottom; t <= top + step / 2; t += step) ticks.push(t);
  const iw = W - L - R, ih = H - T - B;
  const bw = iw / labels.length;
  const y = (v: number) => T + ih - ((v - bottom) / (top - bottom || 1)) * ih;
  const every = Math.ceil(labels.length / 14);
  const cx = (i: number) => L + i * bw + bw / 2;
  const bar = (v: number) => ({ y: Math.min(y(v), y(0)), h: Math.abs(y(v) - y(0)) });
  return (
    <div className="relative">
      <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" role="img" onMouseLeave={() => setHover(null)}>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={L} x2={W - R} y1={y(t)} y2={y(t)} stroke={t === 0 ? "#9ca3af" : "#e5e7eb"} />
            <text x={L - 6} y={y(t) + 4} textAnchor="end" fontSize="11" fill="#6b7280">{signedCompact(t)}</text>
          </g>
        ))}
        {labels.map((lab, i) => (
          <g key={lab} onMouseEnter={() => setHover(i)} onClick={() => onPick?.(i)} style={onPick ? { cursor: "pointer" } : undefined}>
            <rect x={L + i * bw} y={T} width={bw} height={ih} fill={selected === i ? "#e0e7ff" : hover === i ? "#f3f4f6" : "transparent"} />
            <rect x={L + i * bw + bw * 0.1} y={bar(sales[i]).y} width={bw * 0.38} height={bar(sales[i]).h} fill={PALETTE[0]} rx="2" />
            <rect x={L + i * bw + bw * 0.52} y={bar(costs[i]).y} width={bw * 0.38} height={bar(costs[i]).h} fill={PALETTE[1]} rx="2" />
            {i % every === 0 && <text x={cx(i)} y={H - 12} textAnchor="middle" fontSize="11" fill="#6b7280">{monthLabel(lab)}</text>}
          </g>
        ))}
        <polyline fill="none" stroke="#111827" strokeWidth="1.5" points={labels.map((_, i) => `${cx(i)},${y(line[i])}`).join(" ")} style={{ pointerEvents: "none" }} />
        {labels.length <= 40 && labels.map((_, i) => <circle key={i} cx={cx(i)} cy={y(line[i])} r="2.5" fill={line[i] >= 0 ? PALETTE[2] : PALETTE[3]} style={{ pointerEvents: "none" }} />)}
      </svg>
      {hover !== null && (
        <div className="pointer-events-none absolute right-2 top-2 rounded border border-gray-200 bg-white/95 px-3 py-2 text-xs shadow">
          <div className="font-semibold">{monthLabel(labels[hover])}</div>
          <div className="flex items-center gap-1.5"><span className="inline-block h-2 w-2 rounded-sm" style={{ background: PALETTE[0] }} />{salesLabel}: {signedMoney0(sales[hover])}</div>
          <div className="flex items-center gap-1.5"><span className="inline-block h-2 w-2 rounded-sm" style={{ background: PALETTE[1] }} />Costs: {signedMoney0(costs[hover])}</div>
          <div className="mt-0.5 border-t border-gray-100 pt-0.5 font-medium">Profit: {signedMoney0(profit[hover])}</div>
        </div>
      )}
    </div>
  );
}


/* ------------------------------------------------------------------ one month, opened up */
type MonthDetail =
  | { ready: false }
  | {
      ready: true; month: string; total_cost: number; sales_total: number; receipts: number; days_with_sales: number;
      transactions: { entity: string; qb_id: string; date: string; party: string | null; amount: number; biggest_line: number; lines: number; categories: string | null; kinds: string | null; memo: string | null }[];
      categories: { category: string; kind: string; amount: number; typical: number }[];
      sales_days: { date: string; receipts: number }[];
    };
const ENTITY: Record<string, string> = {
  Bill: "Bill", Check: "Check", CreditCardCharge: "Card charge", CreditCardCredit: "Card credit", VendorCredit: "Vendor credit", JournalEntry: "Journal entry",
};
const KIND: Record<string, string> = { inventory: "Inventory", cogs: "Cost of goods", opex: "Operating" };

function MonthPanel({ month, onClose }: { month: string; onClose: () => void }) {
  const [d, setD] = useState<MonthDetail | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let cancelled = false;
    setD(null);
    setErr("");
    api<MonthDetail>(`/analytics/quickbooks/month?month=${month}`)
      .then((r) => { if (!cancelled) setD(r); })
      .catch((e) => { if (!cancelled) setErr(errMsg(e)); });
    return () => { cancelled = true; };
  }, [month]);

  const [y, m] = month.split("-").map(Number);
  const dim = new Date(y, m, 0).getDate();
  const perDay = d && d.ready ? new Map(d.sales_days.map((x) => [Number(x.date.slice(8)), x.receipts] as [number, number])) : new Map<number, number>();
  const top = d && d.ready && d.transactions[0];
  const topShare = top && d && d.ready && d.total_cost > 0 ? top.amount / d.total_cost : 0;

  return (
    <Card title={`Inside ${monthLabel(month)}`} sub="The cost transactions that make up the month, how each category compares with a normal month, and how many sales receipts were entered each day.">
      <button type="button" onClick={onClose} className="mb-2 text-xs text-gray-500 underline hover:text-black">Close</button>
      {err && <Notice kind="error">{err}</Notice>}
      {!d && !err && <p className="text-sm text-gray-400">Loading…</p>}
      {d && d.ready && (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Kpi label="Costs" value={money0(d.total_cost)} accent={PALETTE[1]} />
            <Kpi label="Sales" value={money0(d.sales_total)} accent={PALETTE[0]} />
            <Kpi label="Sales receipts" value={num0(d.receipts)} sub="product lines entered" />
            <Kpi label="Days with a receipt" value={`${d.days_with_sales} of ${dim}`} sub="a month with a long gap usually means entries are missing" />
          </div>
          {top && topShare >= 0.3 && (
            <Notice>
              One transaction accounts for {pct(topShare, 0)} of this month’s costs: {money0(top.amount)} on {top.date}{top.party ? ` to ${top.party}` : ""} ({ENTITY[top.entity] ?? top.entity}{top.categories ? `, ${top.categories}` : ""}).
              If that was a one-off bulk buy it is a timing effect, not a loss. If it looks wrong, it may have been entered twice or to the wrong account in QuickBooks.
            </Notice>
          )}

          <div>
            <h3 className="mb-1 text-xs font-semibold text-gray-600">Biggest cost transactions</h3>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[40rem] text-left text-sm">
                <thead className="text-xs text-gray-500"><tr><th className="py-1 pr-3 font-medium">Date</th><th className="pr-3 font-medium">Who / what</th><th className="pr-3 font-medium">Account</th><th className="pr-3 font-medium">Kind</th><th className="text-right font-medium">Amount</th></tr></thead>
                <tbody>
                  {d.transactions.map((t) => (
                    <tr key={t.entity + t.qb_id} className="border-t border-gray-100 align-top">
                      <td className="whitespace-nowrap py-1 pr-3">{t.date}</td>
                      <td className="pr-3">
                        <div>{t.party ?? ENTITY[t.entity] ?? t.entity} <span className="text-[11px] text-gray-400">{ENTITY[t.entity] ?? t.entity}, {t.lines} line{t.lines === 1 ? "" : "s"}</span></div>
                        {t.memo && <div className="line-clamp-1 text-[11px] text-gray-400">{t.memo}</div>}
                      </td>
                      <td className="pr-3 text-xs text-gray-600">{t.categories}</td>
                      <td className="pr-3 text-xs text-gray-600">{t.kinds ? t.kinds.split(", ").map((k) => KIND[k] ?? k).join(", ") : ""}</td>
                      <td className={`text-right font-medium tabular-nums ${t.amount < 0 ? "text-emerald-600" : ""}`}>{signedMoney0(t.amount)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <div>
              <h3 className="mb-1 text-xs font-semibold text-gray-600">Categories against a normal month</h3>
              <table className="w-full text-left text-sm">
                <thead className="text-xs text-gray-500"><tr><th className="py-1 pr-3 font-medium" /><th className="pr-3 text-right font-medium">This month</th><th className="pr-3 text-right font-medium">Normal</th><th className="text-right font-medium">Times</th></tr></thead>
                <tbody>
                  {d.categories.map((c) => {
                    const ratio = c.typical > 0 ? c.amount / c.typical : null;
                    return (
                      <tr key={c.category} className="border-t border-gray-100">
                        <td className="py-1 pr-3">{c.category}</td>
                        <td className="pr-3 text-right tabular-nums">{money0(c.amount)}</td>
                        <td className="pr-3 text-right tabular-nums text-gray-500">{money0(c.typical)}</td>
                        <td className={`text-right tabular-nums ${ratio !== null && ratio >= 2 ? "font-medium text-red-600" : ""}`}>{ratio === null ? "new" : `${ratio.toFixed(1)}x`}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <div>
              <h3 className="mb-1 text-xs font-semibold text-gray-600">Sales receipts entered per day</h3>
              <Columns rows={Array.from({ length: dim }, (_, i): BarRow => ({ label: String(i + 1), value: perDay.get(i + 1) ?? 0 }))} color={PALETTE[0]} height={150} />
            </div>
          </div>
        </div>
      )}
      {d && !d.ready && <Notice>QuickBooks data isn’t in the database yet.</Notice>}
    </Card>
  );
}

/* ------------------------------------------------------------------ section */
export default function QuickBooksSection({ start, end }: { start?: string; end?: string }) {
  const [data, setData] = useState<QbSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [countInventory, setCountInventory] = useState(true);
  const [source, setSource] = useState<SalesSource | null>(null); // null = pick automatically
  const [smooth, setSmooth] = useState(false);
  const [picked, setPicked] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError("");
      try {
        const q = new URLSearchParams();
        if (start) q.set("start", start);
        if (end) q.set("end", end);
        const r = await api<QbSummary>(`/analytics/quickbooks/summary?${q.toString()}`);
        if (!cancelled) setData(r);
      } catch (e) {
        if (!cancelled) { setData(null); setError(errMsg(e)); }
      }
      if (!cancelled) setLoading(false);
    })();
    return () => { cancelled = true; };
  }, [start, end]);

  const v = useMemo(() => {
    if (!data || !data.ready || data.empty) return null;

    const costRows = data.costs.filter((c) => countInventory || c.kind !== "inventory");
    const qbSalesBy = new Map<string, number>();
    const qbEbayBy = new Map<string, number>();
    data.sales.forEach((s) => {
      add(qbSalesBy, s.month, s.amount);
      if (s.channel === "eBay") add(qbEbayBy, s.month, s.amount);
    });
    const exportBy = new Map(data.ebay_monthly.map((e) => [e.month, e.net_sales] as [string, number]));
    const costBy = new Map<string, number>();
    costRows.forEach((c) => add(costBy, c.month, c.amount));

    // Which months can be called "profit": only where the chosen sales source has sales.
    const qbMonths = monthRange(data.sales.map((s) => s.month));
    const exportMonths = monthRange(data.ebay_monthly.map((e) => e.month));
    const src: SalesSource = source ?? (qbMonths.length ? "qb" : exportMonths.length ? "ebay" : "qb");
    const span = src === "qb" ? qbMonths : exportMonths;
    const salesBy = src === "qb" ? qbSalesBy : exportBy;
    const wSales = span.map((m) => salesBy.get(m) ?? 0);
    const wCosts = span.map((m) => costBy.get(m) ?? 0);
    const totalSales = wSales.reduce((a, b) => a + b, 0);
    const totalCosts = wCosts.reduce((a, b) => a + b, 0);

    // Expenses by category over every month that has a cost.
    const costMonths = monthRange(costRows.map((c) => c.month));
    const catTotal = new Map<string, number>();
    costRows.forEach((c) => add(catTotal, c.category, c.amount));
    const cats = Array.from(catTotal.entries()).filter(([, t]) => t > 0).sort((a, b) => b[1] - a[1]);
    const topCats = cats.slice(0, 7).map(([k]) => k);
    const catColor = (k: string) => { const i = topCats.indexOf(k); return i >= 0 ? PALETTE[i] : PALETTE[7]; };
    const byCatMonth = new Map<string, Map<string, number>>();
    costRows.forEach((c) => {
      const key = topCats.includes(c.category) ? c.category : "Everything else";
      if (!byCatMonth.has(key)) byCatMonth.set(key, new Map());
      add(byCatMonth.get(key)!, c.month, c.amount);
    });
    const catKeys = [...topCats, ...(byCatMonth.has("Everything else") ? ["Everything else"] : [])];
    const catSeries: Series[] = catKeys.map((k) => ({
      key: k, label: k, color: k === "Everything else" ? PALETTE[7] : catColor(k),
      values: costMonths.map((m) => Math.max(0, byCatMonth.get(k)?.get(m) ?? 0)), // a month with a net credit draws as nothing
    }));
    const catTotals = cats.reduce((a, [, t]) => a + t, 0);
    const restTotal = cats.slice(7).reduce((a, [, t]) => a + t, 0);
    const donutRows = [...cats.slice(0, 7).map(([k, t]) => ({ label: k, value: t, color: catColor(k) })), ...(restTotal > 0 ? [{ label: "Everything else", value: restTotal, color: PALETTE[7] }] : [])];

    // Fees: the "... Transaction Fee(s)" accounts (eBay, Amazon, PayPal). "Professional Fees" is not a marketplace fee.
    const feeRows = data.costs.filter((c) => /transaction fee/i.test(c.category));
    const feeMonths = monthRange(feeRows.map((c) => c.month));
    const feeTotal = new Map<string, number>();
    feeRows.forEach((c) => add(feeTotal, c.category, c.amount));
    const feeCats = Array.from(feeTotal.entries()).sort((a, b) => b[1] - a[1]).map(([k]) => k);
    const feeSeries: Series[] = feeCats.slice(0, 6).map((k, i) => ({
      key: k, label: k, color: PALETTE[(i + 3) % PALETTE.length],
      values: feeMonths.map((m) => Math.max(0, feeRows.filter((c) => c.category === k && c.month === m).reduce((a, c) => a + c.amount, 0))),
    }));
    const ebayFeeBy = new Map<string, number>();
    feeRows.filter((c) => /ebay/i.test(c.category)).forEach((c) => add(ebayFeeBy, c.month, c.amount));
    const rateBase = src === "qb" ? qbEbayBy : exportBy; // eBay sales from the same source as the profit chart
    // Fees are booked in lumps (a card charge now and then), so a per-month rate would swing wildly.
    // The rate is all eBay fees in the profit window over all eBay sales in the same window.
    const inSpan = new Set(span);
    let rateFees = 0, rateSales = 0;
    ebayFeeBy.forEach((fee, m) => { if (inSpan.has(m)) rateFees += fee; });
    span.forEach((m) => { rateSales += rateBase.get(m) ?? 0; });

    // Check against eBay: months where both the export and QuickBooks (eBay-tagged receipts) have sales.
    const checkMonths = exportMonths.filter((m) => exportBy.has(m) && qbEbayBy.has(m));
    const check = checkMonths.map((m) => {
      const e = exportBy.get(m) ?? 0, q = qbEbayBy.get(m) ?? 0;
      return { month: m, ebay: e, qb: q, diff: q - e, pct: e ? (q - e) / e : 0 };
    });
    const unassigned = data.sales.filter((s) => s.channel === "Unassigned" || s.channel === "Other").reduce((a, s) => a + s.amount, 0);

    // Months that stand out: costs far above a normal month, or sales far below one.
    const med = (a: number[]) => { const b = a.filter((x) => x > 0).sort((x, y) => x - y); return b.length ? b[Math.floor(b.length / 2)] : 0; };
    const medCost = med(wCosts), medSales = med(wSales);
    const unusual = span
      .map((mo, i) => {
        const why: string[] = [];
        if (medCost && wCosts[i] >= 1.8 * medCost && wCosts[i] - medCost >= 20000) why.push(`costs ${(wCosts[i] / medCost).toFixed(1)}x normal`);
        if (medSales && wSales[i] <= 0.4 * medSales) why.push(`sales only ${Math.round((wSales[i] / medSales) * 100)}% of normal`);
        return { month: mo, why };
      })
      .filter((x) => x.why.length);

    return {
      src, qbMonths, exportMonths, span, wSales, wCosts, totalSales, totalCosts, costMonths, catSeries, donutRows, catTotals, unusual,
      feeMonths, feeSeries, feeTotalAll: feeRows.reduce((a, c) => a + c.amount, 0), rateFees, rateSales,
      check, unassigned, qbEbayHas: qbEbayBy.size > 0,
    };
  }, [data, countInventory, source]);

  const cov = data && data.ready ? data.coverage : null;
  const sourceLabel = v?.src === "ebay" ? "eBay net sales" : "QuickBooks sales";

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-xl font-semibold">QuickBooks</h2>
          <p className="text-sm text-gray-500">
            What you spent and kept. Costs, fees and vendors come from QuickBooks; eBay sales still come from the eBay export.
            {loading && <span className="ml-2 text-xs text-gray-400">Loading…</span>}
          </p>
        </div>
        {v && (
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm">
            {v.qbMonths.length > 0 && v.exportMonths.length > 0 && (
              <div className="flex items-center gap-1.5">
                <span className="text-xs text-gray-500">Profit sales from</span>
                <div className="flex gap-1 rounded-lg bg-gray-100 p-1">
                  {([["qb", "QuickBooks"], ["ebay", "eBay export"]] as [SalesSource, string][]).map(([k, label]) => (
                    <button key={k} type="button" onClick={() => setSource(k)} className={`rounded-md px-2.5 py-1 text-xs font-medium ${v.src === k ? "bg-white text-black shadow-sm" : "text-gray-600 hover:text-black"}`}>{label}</button>
                  ))}
                </div>
              </div>
            )}
            <label className="flex items-center gap-1.5 text-xs text-gray-700">
              <input type="checkbox" checked={countInventory} onChange={(e) => setCountInventory(e.target.checked)} />
              Count inventory purchases as a cost
            </label>
          </div>
        )}
      </div>

      {error && <Notice kind="error">{error}</Notice>}
      {data && !data.ready && <Notice>QuickBooks data isn’t in the database yet. Connect and run a sync on the QuickBooks page first.</Notice>}
      {data && data.ready && data.empty && <Notice>The QuickBooks sync hasn’t saved any sales or costs yet.</Notice>}

      {cov && !(data && data.ready && data.empty) && (
        <p className="text-xs text-gray-500">
          QuickBooks has sales from {dayLabel(cov.sales_first)} to {dayLabel(cov.sales_last)} and costs from {dayLabel(cov.costs_first)} to {dayLabel(cov.costs_last)}.
          Profit is only worked out for months that have sales in the chosen source.
        </p>
      )}

      {data && data.ready && !data.empty && v && (
        <div className={loading ? "space-y-4 opacity-60 transition-opacity" : "space-y-4 transition-opacity"}>
          {/* ---- KPIs */}
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Kpi label="Sales" value={money0(v.totalSales)} sub={v.span.length ? `${sourceLabel}, ${v.span.length} months` : "no sales in this range"} accent={PALETTE[0]} />
            <Kpi label="Costs" value={money0(v.totalCosts)} sub={countInventory ? "same months, inventory included" : "same months, without inventory"} accent={PALETTE[1]} />
            <Kpi label="Profit" value={signedMoney0(v.totalSales - v.totalCosts)} sub={v.span.length ? "sales minus costs" : undefined} accent={v.totalSales - v.totalCosts >= 0 ? PALETTE[2] : PALETTE[3]} />
            <Kpi label="Margin" value={v.totalSales ? pct((v.totalSales - v.totalCosts) / v.totalSales) : "—"} sub="profit as a share of sales" />
          </div>

          {/* ---- profit by month */}
          <Card
            title="Sales, costs and profit by month"
            sub={v.src === "qb" ? "QuickBooks sales (items, shipping collected, less refunds) against everything QuickBooks recorded as a cost." : "eBay export net sales against everything QuickBooks recorded as a cost (costs include other channels)."}
          >
            {v.span.length ? (
              <>
                <ProfitChart
                  labels={v.span} sales={v.wSales} costs={v.wCosts} salesLabel={sourceLabel} smooth={smooth}
                  selected={picked ? v.span.indexOf(picked) : null} onPick={(i) => setPicked(v.span[i])}
                />
                <Legend items={[{ label: sourceLabel, color: PALETTE[0] }, { label: "Costs", color: PALETTE[1] }, { label: smooth ? "Profit, 3-month average (line)" : "Profit (line)", color: "#111827" }]} />
                <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-600">
                  <label className="flex items-center gap-1.5"><input type="checkbox" checked={smooth} onChange={(e) => setSmooth(e.target.checked)} />Smooth the profit line (3-month average)</label>
                  <span className="text-gray-400">Click any month to see what is inside it.</span>
                </div>
                <p className="mt-2 rounded bg-gray-50 p-2 text-xs text-gray-600">
                  <strong>The black line is profit:</strong> that month’s sales minus that month’s costs. A green dot is a profitable month, a red dot a losing one. Inventory is counted when you <em>buy</em> it, not when you sell it,
                  so a big stock-up shows as a deep dip and the sales it produces arrive in the months after. That is why the line jumps around, and why the smoothed version is the fairer read.
                </p>
                {v.unusual.length > 0 && (
                  <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
                    <span className="text-gray-500">Months that stand out:</span>
                    {v.unusual.map((u) => (
                      <button key={u.month} type="button" onClick={() => setPicked(u.month)} className={`rounded-full border px-2.5 py-1 ${picked === u.month ? "border-indigo-500 bg-indigo-50 text-indigo-700" : "border-gray-200 bg-white text-gray-700 hover:bg-gray-50"}`}>
                        {monthLabel(u.month)}: {u.why.join(", ")}
                      </button>
                    ))}
                  </div>
                )}
              </>
            ) : (
              <Empty text={v.src === "qb" ? "QuickBooks has no sales in this date range." : "The eBay export has no sales in this date range."} />
            )}
          </Card>

          {picked && <MonthPanel month={picked} onClose={() => setPicked(null)} />}

          {/* ---- expenses by category */}
          <div className="grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
            <Card title="Expenses by category" sub="Top-level account, by month. Hover a month for the breakdown.">
              {v.catSeries.length ? (
                <>
                  <StackedBars labels={v.costMonths} series={v.catSeries} />
                  <Legend items={v.catSeries.map((s) => ({ label: s.label, color: s.color }))} />
                </>
              ) : (
                <Empty text="No costs in this date range." />
              )}
            </Card>
            <Card title="Share of spend" sub="Over the whole date range.">
              <Donut rows={v.donutRows} fmt={money0} center={{ big: compactMoney(v.catTotals), small: "spent" }} />
            </Card>
          </div>

          {/* ---- fees */}
          <Card
            title="Marketplace and payment fees"
            sub="eBay, Amazon and PayPal transaction fee accounts. Fees are booked in lumps, so single months can spike."
          >
            {v.feeSeries.length ? (
              <>
                <div className="mb-3 grid grid-cols-2 gap-3 md:grid-cols-3">
                  <Kpi label="Fees in range" value={money0(v.feeTotalAll)} />
                  <Kpi
                    label="eBay fee rate"
                    value={v.rateSales ? pct(v.rateFees / v.rateSales) : "—"}
                    sub={v.rateSales ? `${money0(v.rateFees)} on ${money0(v.rateSales)} of ${v.src === "qb" ? "eBay-tagged QuickBooks" : "eBay export"} sales` : "needs eBay fees and eBay sales in the same months"}
                  />
                </div>
                <StackedBars labels={v.feeMonths} series={v.feeSeries} tipFmt={money0} height={220} />
                <Legend items={v.feeSeries.map((s) => ({ label: s.label, color: s.color }))} />
              </>
            ) : (
              <Empty text="No fee accounts have costs in this date range." />
            )}
          </Card>

          {/* ---- vendors */}
          <Card title="Who you pay" sub="Bills, checks and card charges by vendor, top 15. The small line is the part that was inventory.">
            <HBars
              labelWidth="w-44"
              color={PALETTE[4]}
              rows={data.vendors.map((x): BarRow => ({ label: x.vendor, value: x.amount, sub: x.inventory > 0 ? `${money0(x.inventory)} inventory` : `${num0(x.txns)} payments` }))}
            />
          </Card>

          {/* ---- check against eBay */}
          <Card title="Check against eBay" sub="Monthly eBay export net sales next to what QuickBooks recorded on sales tagged to eBay. A big gap can mean missing or double-counted sales.">
            {v.check.length ? (
              <>
                <LineChart
                  labels={v.check.map((r) => monthLabel(r.month))}
                  series={[
                    { key: "ebay", label: "eBay export", color: PALETTE[0], values: v.check.map((r) => r.ebay) },
                    { key: "qb", label: "QuickBooks (eBay)", color: PALETTE[1], values: v.check.map((r) => r.qb) },
                  ]}
                />
                <Legend items={[{ label: "eBay export", color: PALETTE[0] }, { label: "QuickBooks (eBay)", color: PALETTE[1] }]} />
                <div className="mt-3 max-h-64 overflow-auto">
                  <table className="w-full min-w-[28rem] text-left text-sm">
                    <thead className="sticky top-0 bg-white text-xs text-gray-500">
                      <tr><th className="py-1 pr-3 font-medium">Month</th><th className="pr-3 text-right font-medium">eBay export</th><th className="pr-3 text-right font-medium">QuickBooks</th><th className="pr-3 text-right font-medium">Difference</th><th className="text-right font-medium">%</th></tr>
                    </thead>
                    <tbody>
                      {v.check.map((r) => (
                        <tr key={r.month} className="border-t border-gray-100">
                          <td className="py-1 pr-3">{monthLabel(r.month)}</td>
                          <td className="pr-3 text-right tabular-nums">{money0(r.ebay)}</td>
                          <td className="pr-3 text-right tabular-nums">{money0(r.qb)}</td>
                          <td className={`pr-3 text-right tabular-nums ${Math.abs(r.pct) > 0.1 ? "font-medium text-red-600" : ""}`}>{signedMoney0(r.diff)}</td>
                          <td className={`text-right tabular-nums ${Math.abs(r.pct) > 0.1 ? "font-medium text-red-600" : ""}`}>{pct(r.pct, 0)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            ) : v.exportMonths.length === 0 ? (
              <Empty text="Import the eBay export first; there is nothing to compare against." />
            ) : !v.qbEbayHas ? (
              <Empty text={`No QuickBooks sales are tagged to eBay (by deposit account or payment method), so there is nothing to line up. ${money0(v.unassigned)} of sales is untagged.`} />
            ) : (
              <Empty text="The eBay export and QuickBooks have no month in common in this date range." />
            )}
          </Card>

          <p className="pb-4 text-xs text-gray-500">
            Notes: costs are what QuickBooks recorded on bills, checks, card charges and journal entries (less card and vendor credits), plus fees taken off sales receipts. Inventory purchases are counted when bought,
            not when sold, so a big stock-up month looks like a loss; untick the box above to leave them out. Bill payments are not counted a second time.
            Left out: {money0(data.left_out.sales_other)} on sales lines that are not income (sales tax, notes) and {money0(data.left_out.purchase_non_costs)} on {num0(data.left_out.purchase_non_cost_lines)} purchase lines
            that go to bank, loan, tax, payroll or equity accounts rather than a cost.
          </p>
        </div>
      )}
    </div>
  );
}
