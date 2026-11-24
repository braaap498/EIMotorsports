"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Badge, Notice, btn2Cls, inputCls } from "./ui";
import { money, useAmazonAccounts } from "./amazonShared";

type QRow = { id: number; listing_id: number; sku: string | null; account: string | null; price: number; status: string; attempts: number; last_error: string | null; created_at: string; done_at: string | null };
type Row = {
  id: number; account: string; sku: string; title: string | null; price: number | null; floor: number | null; buy_box: number | null;
  bb_is_ours: boolean | null; amazon_on_listing: boolean | null; checked: string | null; max_price: number | null;
  action: string; suggested: number | null; why: string;
};

const TONE: Record<string, "green" | "amber" | "blue" | "gray"> = { match: "blue", win: "blue", raise: "green", hold: "gray", none: "gray", unknown: "gray", stand_down: "amber" };

/* Amazon > Buy Box: the price Amazon expects for each listing, our floor (cost + fees + shipping + target margin), and what to do.
   SUGGESTIONS ONLY: nothing changes on Amazon until you press Apply, and Apply needs Live pushes ON. Queue lines a price up for the background loop (same floor check, sent later). */
export default function AmazonBuyBox() {
  const { accounts, account, setAccount } = useAmazonAccounts();
  const [rows, setRows] = useState<Row[]>([]);
  const [only, setOnly] = useState("");
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [queue, setQueue] = useState<QRow[]>([]);
  const [allQueue, setAllQueue] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const p = new URLSearchParams({ limit: "200" });
      if (account) p.set("account", account);
      if (only) p.set("only", only);
      setRows((await api<{ items: Row[] }>(`/amazon-buybox/rows?${p}`)).items);
      try {
        setQueue((await api<{ rows: QRow[] }>("/amazon-buybox/queue")).rows);
      } catch {
        setQueue([]); // the queue list is a convenience; the table above still works without it
      }
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy(false);
    }
  }, [account, only]);
  useEffect(() => {
    void load();
  }, [load]);

  async function act(fn: () => Promise<unknown>, ok: string) {
    setMsg(null);
    try {
      await fn();
      setMsg({ kind: "success", text: ok });
      await load();
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    }
  }

  return (
    <div className="space-y-3">
      <p className="text-xs text-gray-500">
        <strong>Match</strong>: we can meet the Buy Box price and stay above our floor. <strong>Raise</strong>: we hold it with room. <strong>Stand down</strong>: Amazon
        sells below our floor, so we do not chase it. Listings without a product cost use the floor as unknown.
      </p>
      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}
      <div className="flex flex-wrap items-end gap-2">
        <select className={`${inputCls} !w-48`} value={account} onChange={(e) => setAccount(e.target.value)} aria-label="Account">
          <option value="">All accounts</option>
          {accounts.map((a) => <option key={a.account} value={a.account}>{a.display_name || a.account}</option>)}
        </select>
        <select className={`${inputCls} !w-44`} value={only} onChange={(e) => setOnly(e.target.value)} aria-label="Action">
          <option value="">Every action</option>
          <option value="match">Match</option><option value="raise">Raise</option><option value="hold">Hold</option><option value="stand_down">Stand down</option>
        </select>
        <button className={btn2Cls} disabled={busy} onClick={() => void load()}>{busy ? "Loading…" : "Refresh"}</button>
        <button className={btn2Cls} disabled={!account} title={account ? "Read Amazon's expected prices (up to 200 listings)" : "Pick an account first"} onClick={() => void act(() => api("/amazon-buybox/refresh", { method: "POST", body: { account } }), "Prices read from Amazon.")}>Read prices from Amazon</button>
      </div>
      <div className="overflow-x-auto rounded border border-gray-200 bg-white">
        <table className="w-full min-w-[70rem] text-left text-sm">
          <thead className="bg-gray-50 text-xs uppercase text-gray-500">
            <tr><th className="px-3 py-2">SKU</th><th className="px-3 py-2">Our price</th><th className="px-3 py-2">Floor</th><th className="px-3 py-2">Buy Box</th><th className="px-3 py-2">Suggestion</th><th className="px-3 py-2">Max price</th></tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {rows.map((r) => (
              <tr key={r.id}>
                <td className="max-w-[20rem] px-3 py-2"><div className="font-medium">{r.sku}</div><div className="truncate text-xs text-gray-500" title={r.title ?? ""}>{r.title}</div></td>
                <td className="px-3 py-2">{money(r.price)}</td>
                <td className="px-3 py-2">{money(r.floor)}</td>
                <td className="px-3 py-2">{money(r.buy_box)}{r.bb_is_ours ? " (ours)" : ""}{r.amazon_on_listing ? " · Amazon sells" : ""}</td>
                <td className="px-3 py-2">
                  <Badge tone={TONE[r.action] ?? "gray"}>{r.action.replace("_", " ")}</Badge>
                  <div className="text-xs text-gray-500">{r.why}</div>
                  {r.suggested != null && Math.abs(r.suggested - (r.price ?? 0)) > 0.005 && (
                    <button
                      className={`${btn2Cls} mt-1 !px-2 !py-1 !text-xs`}
                      onClick={() => { if (window.confirm(`Change the price on Amazon to ${money(r.suggested)}?`)) void act(() => api(`/amazon-buybox/${r.id}/apply`, { method: "POST", body: { price: r.suggested } }), "Price sent to Amazon."); }}
                    >
                      Apply {money(r.suggested)}
                    </button>
                  )}
                  {r.suggested != null && Math.abs(r.suggested - (r.price ?? 0)) > 0.005 && (
                    <button
                      className={`${btn2Cls} ml-1 mt-1 !px-2 !py-1 !text-xs`}
                      title="Checked against the floor now and again when sent. The background loop sends it within a few minutes."
                      onClick={() => void act(() => api(`/amazon-buybox/${r.id}/queue`, { method: "POST", body: { price: r.suggested } }), "Price queued. It goes out in the next few minutes.")}
                    >
                      Queue
                    </button>
                  )}
                </td>
                <td className="px-3 py-2">
                  <input className={`${inputCls} !w-24 !py-1`} type="number" defaultValue={r.max_price ?? ""} onBlur={(e) => { const v = e.target.value.trim(); if (v !== "" && Number(v) !== r.max_price) void act(() => api(`/amazon-buybox/${r.id}`, { method: "PUT", body: { max_price: Number(v) } }), "Saved."); }} />
                </td>
              </tr>
            ))}
            {rows.length === 0 && <tr><td colSpan={6} className="px-3 py-6 text-center text-gray-500">{busy ? "Loading…" : "Nothing to show. Link listings to products, then read prices from Amazon."}</td></tr>}
          </tbody>
        </table>
      </div>
      {queue.length > 0 && (
        <div className="rounded border border-gray-200 bg-white p-3">
          <div className="mb-2 flex items-center justify-between">
            <div className="text-sm font-semibold">Queued prices</div>
            <button className={`${btn2Cls} !px-2 !py-1 !text-xs`} onClick={() => void act(() => api("/amazon-buybox/queue/run", { method: "POST", body: {} }), "Sent what was waiting.")}>Send waiting prices now</button>
          </div>
          <table className="w-full text-left text-xs">
            <thead className="text-gray-500"><tr><th className="py-1 pr-3">SKU</th><th className="py-1 pr-3">Price</th><th className="py-1 pr-3">Status</th><th className="py-1">Note</th></tr></thead>
            <tbody>
              {queue.slice(0, allQueue ? 100 : 15).map((q) => (
                <tr key={q.id} className="border-t border-gray-100">
                  <td className="py-1 pr-3 font-mono">{q.sku ?? q.listing_id}</td>
                  <td className="py-1 pr-3">{money(q.price)}</td>
                  <td className="py-1 pr-3"><Badge tone={q.status === "done" ? "green" : q.status === "failed" ? "amber" : "gray"}>{q.status}</Badge></td>
                  <td className="py-1 text-gray-500">{q.last_error ?? (q.status === "pending" && q.attempts > 0 ? `Retry ${q.attempts}` : "")}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {queue.length > 15 && (
            <button className="mt-2 text-xs text-blue-700 underline" onClick={() => setAllQueue(!allQueue)}>{allQueue ? "Show fewer" : `Show all ${queue.length}`}</button>
          )}
        </div>
      )}
    </div>
  );
}
