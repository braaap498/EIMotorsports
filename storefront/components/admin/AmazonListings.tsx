"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Badge, Notice, Pager, btn2Cls, inputCls } from "./ui";
import { money, useAmazonAccounts } from "./amazonShared";

type Row = {
  id: number; account: string; marketplace: string | null; sku: string; asin: string | null; title: string | null; status: string | null;
  price: number | null; quantity: number | null; fulfillment: string | null; product_id: number | null; product_sku: string | null;
  dropship_ok: boolean | null; pieces_per_sale: number | null; issues: string | null; synced_at: string | null;
};
const LIMIT = 50;

/* Amazon > Listings: sync from Amazon, link each listing to a product, tick Drop ship, set pieces per sale (a 3-pack = 3). */
export default function AmazonListings() {
  const { accounts, account, setAccount } = useAmazonAccounts();
  const [rows, setRows] = useState<Row[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [q, setQ] = useState("");
  const [qIn, setQIn] = useState("");
  const [linked, setLinked] = useState("");
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [link, setLink] = useState<Record<number, string>>({});

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const p = new URLSearchParams({ offset: String(offset), limit: String(LIMIT) });
      if (account) p.set("account", account);
      if (q) p.set("q", q);
      if (linked) p.set("linked", linked);
      const r = await api<{ total: number; items: Row[] }>(`/amazon-listings/repo?${p}`);
      setRows(r.items);
      setTotal(r.total);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy(false);
    }
  }, [offset, account, q, linked]);
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
        Press <strong>Sync from Amazon</strong>, then give each listing its product (type the product id). Quantity changes anywhere in the app reach a
        linked listing once Live pushes and Send quantity changes are ON for the account.
      </p>
      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}
      <div className="flex flex-wrap items-end gap-2">
        <select className={`${inputCls} !w-48`} value={account} onChange={(e) => { setAccount(e.target.value); setOffset(0); }} aria-label="Account">
          <option value="">All accounts</option>
          {accounts.map((a) => <option key={a.account} value={a.account}>{a.display_name || a.account}</option>)}
        </select>
        <input className={`${inputCls} !w-72`} placeholder="SKU, ASIN or title" value={qIn} onChange={(e) => setQIn(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") { setQ(qIn.trim()); setOffset(0); } }} />
        <select className={`${inputCls} !w-40`} value={linked} onChange={(e) => { setLinked(e.target.value); setOffset(0); }} aria-label="Linked">
          <option value="">Linked or not</option>
          <option value="yes">Linked</option>
          <option value="no">Not linked</option>
        </select>
        <button className={btn2Cls} onClick={() => { setQ(qIn.trim()); setOffset(0); }}>Search</button>
        <button className={btn2Cls} disabled={busy} onClick={() => void load()}>{busy ? "Loading…" : "Refresh"}</button>
        <button
          className={btn2Cls}
          disabled={!account}
          title={account ? "" : "Pick an account first"}
          onClick={() => void act(() => api("/amazon-listings/sync", { method: "POST", body: { account } }), "Sync finished.")}
        >
          Sync from Amazon
        </button>
        <button
          className={btn2Cls}
          disabled={!account}
          title={account ? "Send waiting quantity changes now" : "Pick an account first"}
          onClick={() => void act(() => api("/amazon-listings/queue/run", { method: "POST", body: { account } }), "Quantity queue run.")}
        >
          Send waiting quantities
        </button>
      </div>
      <Pager total={total} limit={LIMIT} offset={offset} onChange={setOffset} />
      <div className="overflow-x-auto rounded border border-gray-200 bg-white">
        <table className="w-full min-w-[70rem] text-left text-sm">
          <thead className="bg-gray-50 text-xs uppercase text-gray-500">
            <tr>
              <th className="px-3 py-2">SKU / ASIN</th><th className="px-3 py-2">Title</th><th className="px-3 py-2">Price</th><th className="px-3 py-2">Qty</th>
              <th className="px-3 py-2">Product</th><th className="px-3 py-2">Drop ship</th><th className="px-3 py-2">Pieces</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {rows.map((r) => (
              <tr key={r.id}>
                <td className="px-3 py-2">
                  <div className="font-medium">{r.sku}</div>
                  <div className="text-xs text-blue-600">{r.asin ?? "—"} · {r.marketplace ?? "US"}</div>
                  <button
                    type="button"
                    className="text-xs text-blue-700 underline"
                    onClick={() => {
                      const sku = window.prompt(`Sell similar: a new SKU for the copy of ${r.sku}`);
                      if (sku && sku.trim()) void act(() => api(`/amazon-new/drafts/from-listing/${r.id}?sku=${encodeURIComponent(sku.trim())}`, { method: "POST" }), "Draft made on the New listing screen.");
                    }}
                  >
                    Sell similar
                  </button>
                </td>
                <td className="max-w-[22rem] px-3 py-2">
                  <div className="truncate" title={r.title ?? ""}>{r.title ?? "(no title)"}</div>
                  <div className="flex gap-1 text-xs"><Badge tone={r.fulfillment === "FBA" ? "blue" : "gray"}>{r.fulfillment ?? "?"}</Badge>{r.status && <span className="text-gray-500">{r.status}</span>}</div>
                  {r.issues && <div className="text-xs text-red-700">{r.issues}</div>}
                </td>
                <td className="px-3 py-2">{money(r.price)}</td>
                <td className="px-3 py-2">{r.quantity ?? "—"}</td>
                <td className="px-3 py-2">
                  {r.product_id ? (
                    <span>#{r.product_id} {r.product_sku ?? ""}</span>
                  ) : (
                    <span className="flex gap-1">
                      <input className={`${inputCls} !w-24 !py-1`} placeholder="Product id" value={link[r.id] ?? ""} onChange={(e) => setLink({ ...link, [r.id]: e.target.value })} />
                      <button className={`${btn2Cls} !px-2 !py-1 !text-xs`} disabled={!/^\d+$/.test(link[r.id] ?? "")} onClick={() => void act(() => api(`/amazon-listings/${r.id}/link`, { method: "POST", body: { product_id: Number(link[r.id]) } }), "Linked.")}>Link</button>
                    </span>
                  )}
                </td>
                <td className="px-3 py-2">
                  <input type="checkbox" checked={!!r.dropship_ok} disabled={r.fulfillment === "FBA"} onChange={(e) => void act(() => api(`/amazon-listings/${r.id}/dropship`, { method: "POST", body: { on: e.target.checked } }), "Saved.")} />
                </td>
                <td className="px-3 py-2">
                  <input
                    className={`${inputCls} !w-16 !py-1`}
                    type="number"
                    min={1}
                    defaultValue={r.pieces_per_sale ?? 1}
                    onBlur={(e) => { const n = Number(e.target.value); if (n >= 1 && n !== (r.pieces_per_sale ?? 1)) void act(() => api(`/amazon-listings/${r.id}/pieces`, { method: "POST", body: { pieces: n } }), "Saved."); }}
                  />
                </td>
              </tr>
            ))}
            {rows.length === 0 && <tr><td colSpan={7} className="px-3 py-6 text-center text-gray-500">{busy ? "Loading…" : "No Amazon listings. Pick an account and press Sync from Amazon."}</td></tr>}
          </tbody>
        </table>
      </div>
      <Pager total={total} limit={LIMIT} offset={offset} onChange={setOffset} />
    </div>
  );
}
