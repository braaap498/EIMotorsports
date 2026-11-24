"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Notice, btn2Cls, inputCls } from "./ui";
import { money, useAmazonAccounts } from "./amazonShared";

type Summary = { orders: number; units: number; revenue: number; fees: number; cost: number; lines_without_cost: number; dropship_lines: number; refunds?: number; estimated_margin: number; days: number };
type Day = { day: string; orders: number; units: number; revenue: number; fees: number; cost: number };
type Prod = { product_id: number | null; title: string | null; sku: string | null; units: number; revenue: number };

/* Amazon > Analytics: the daily rollup built from the Amazon order tables. Margin is an ESTIMATE (revenue - refunds - fees - product cost);
   lines whose product has no cost are counted in revenue and listed in the warning. */
export default function AmazonAnalytics() {
  const { accounts, account, setAccount } = useAmazonAccounts();
  const [days, setDays] = useState(30);
  const [sum, setSum] = useState<Summary | null>(null);
  const [daily, setDaily] = useState<Day[]>([]);
  const [prods, setProds] = useState<Prod[]>([]);
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const p = new URLSearchParams({ days: String(days) });
      if (account) p.set("account", account);
      const [s, d, t] = await Promise.all([
        api<Summary>(`/amazon-analytics/summary?${p}`),
        api<{ days: Day[] }>(`/amazon-analytics/daily?${p}`),
        api<{ products: Prod[] }>(`/amazon-analytics/top-products?${p}`),
      ]);
      setSum(s);
      setDaily(d.days);
      setProds(t.products);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy(false);
    }
  }, [days, account]);
  useEffect(() => {
    void load();
  }, [load]);

  async function rebuild() {
    setMsg(null);
    setBusy(true);
    try {
      await api("/amazon-analytics/rebuild", { method: "POST", body: { ...(account ? { account } : {}), days: Math.max(days, 90) } });
      setMsg({ kind: "success", text: "Rebuilt." });
      await load();
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
      setBusy(false);
    }
  }

  const max = Math.max(1, ...daily.map((d) => d.revenue));
  const kpi = (label: string, v: string) => (
    <div className="rounded border border-gray-200 bg-white px-4 py-3">
      <div className="text-xs uppercase text-gray-500">{label}</div>
      <div className="text-xl font-semibold">{v}</div>
    </div>
  );

  return (
    <div className="space-y-4">
      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}
      <div className="flex flex-wrap items-end gap-2">
        <select className={`${inputCls} !w-48`} value={account} onChange={(e) => setAccount(e.target.value)} aria-label="Account">
          <option value="">All accounts</option>
          {accounts.map((a) => <option key={a.account} value={a.account}>{a.display_name || a.account}</option>)}
        </select>
        <select className={`${inputCls} !w-40`} value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label="Period">
          {[7, 30, 90, 365].map((d) => <option key={d} value={d}>Last {d} days</option>)}
        </select>
        <button className={btn2Cls} disabled={busy} onClick={() => void load()}>{busy ? "Loading…" : "Refresh"}</button>
        <button className={btn2Cls} disabled={busy} title="Recount the daily numbers from the order tables" onClick={() => void rebuild()}>Rebuild</button>
      </div>
      {sum && (
        <>
          <div className="grid gap-2 sm:grid-cols-4 lg:grid-cols-7">
            {kpi("Orders", String(sum.orders))}
            {kpi("Units", String(sum.units))}
            {kpi("Revenue", money(sum.revenue))}
            {kpi("Refunds", money(sum.refunds ?? 0))}
            {kpi("Fees", money(sum.fees))}
            {kpi("Cost", money(sum.cost))}
            {kpi("Est. margin", money(sum.estimated_margin))}
          </div>
          {sum.lines_without_cost > 0 && (
            <Notice kind="info">{sum.lines_without_cost} order line(s) have a product with no cost, so the margin is overstated. Add the costs on the products.</Notice>
          )}
        </>
      )}
      <div className="rounded border border-gray-200 bg-white p-3">
        <div className="mb-2 text-sm font-medium">Revenue per day</div>
        <div className="flex h-32 items-end gap-0.5 overflow-x-auto">
          {daily.map((d) => (
            <div key={d.day} className="min-w-[6px] flex-1 bg-gray-800" style={{ height: `${Math.max(2, (d.revenue / max) * 100)}%` }} title={`${d.day}: ${money(d.revenue)} (${d.orders} orders)`} />
          ))}
          {daily.length === 0 && <p className="text-sm text-gray-500">No data. Press Rebuild after the first poll.</p>}
        </div>
      </div>
      <div className="overflow-x-auto rounded border border-gray-200 bg-white">
        <table className="w-full text-left text-sm">
          <thead className="bg-gray-50 text-xs uppercase text-gray-500"><tr><th className="px-3 py-2">Top products</th><th className="px-3 py-2">SKU</th><th className="px-3 py-2">Units</th><th className="px-3 py-2">Revenue</th></tr></thead>
          <tbody className="divide-y divide-gray-100">
            {prods.map((p, i) => (
              <tr key={`${p.product_id ?? p.sku}-${i}`}><td className="px-3 py-2">{p.title ?? "(no title)"}</td><td className="px-3 py-2">{p.sku ?? "—"}</td><td className="px-3 py-2">{p.units}</td><td className="px-3 py-2">{money(p.revenue)}</td></tr>
            ))}
            {prods.length === 0 && <tr><td colSpan={4} className="px-3 py-4 text-center text-gray-500">Nothing yet.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
