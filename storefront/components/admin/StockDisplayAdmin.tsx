"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Notice, btn2Cls, inputCls } from "./ui";

/* How the website shows stock: Available, or the number ("35 Available"). Lives under Products. Same settings as before
   (/availability/display and the store default); only the place moved. Out of stock always shows when nothing can be sold. */

type Avail = { display_default: "available" | "count" };
type DisplayRow = { id: number; sku: string | null; title: string | null; stock_display: string | null; shown: string };
const modeLabel = (m: string | null) => (m === "count" ? "Number (35 Available)" : m === "available" ? "Available" : "Store default");

export default function StockDisplayAdmin() {
  const [settings, setSettings] = useState<Avail | null>(null);
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<DisplayRow[]>([]);
  const [work, setWork] = useState(false);
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);

  const loadSettings = useCallback(async () => {
    try {
      setSettings(await api<Avail>("/availability/settings"));
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    }
  }, []);
  useEffect(() => {
    void loadSettings();
  }, [loadSettings]);

  async function saveDefault(v: string) {
    setWork(true);
    setMsg(null);
    try {
      setSettings(await api<Avail>("/availability/settings", { method: "PUT", body: { display_default: v } }));
      setMsg({ kind: "success", text: "Saved." });
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setWork(false);
    }
  }

  async function search() {
    try {
      setRows((await api<{ items: DisplayRow[] }>(`/availability/display/products?q=${encodeURIComponent(q)}`)).items);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    }
  }

  async function apply(mode: "available" | "count" | null, body: { all_products?: boolean; product_ids?: number[] }) {
    setMsg(null);
    setWork(true);
    try {
      const r = await api<{ updated: number }>("/availability/display", { method: "POST", body: { mode, ...body } });
      setMsg({ kind: "success", text: `Updated ${r.updated.toLocaleString()} product${r.updated === 1 ? "" : "s"}.` });
      if (rows.length) await search();
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setWork(false);
    }
  }

  return (
    <div className="max-w-3xl space-y-4">
      <h1 className="text-xl font-semibold">Website stock display</h1>
      <p className="text-sm text-gray-600">
        The website shows <b>Available</b>, <b>35 Available</b> (the number) or <b>Out of stock</b>. Out of stock always shows when nothing can be sold; the products stay visible.
      </p>
      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}

      <div className="space-y-2 rounded border border-gray-200 bg-white p-4">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="font-semibold">Store default:</span>
          <select className={`${inputCls} w-auto`} value={settings?.display_default ?? "available"} disabled={work || !settings} onChange={(e) => void saveDefault(e.target.value)}>
            <option value="available">Available</option>
            <option value="count">Number (35 Available)</option>
          </select>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="font-semibold">All products:</span>
          <button type="button" className={btn2Cls} disabled={work} onClick={() => window.confirm("Set EVERY product to Available?") && void apply("available", { all_products: true })}>Set all to Available</button>
          <button type="button" className={btn2Cls} disabled={work} onClick={() => window.confirm("Set EVERY product to show the number?") && void apply("count", { all_products: true })}>Set all to Number</button>
          <button type="button" className={btn2Cls} disabled={work} onClick={() => window.confirm("Make every product follow the store default?") && void apply(null, { all_products: true })}>All follow the store default</button>
        </div>
      </div>

      <div className="space-y-2 rounded border border-gray-200 bg-white p-4">
        <div className="text-sm font-semibold">One product at a time</div>
        <div className="flex flex-wrap gap-2">
          <input className={`${inputCls} w-64`} placeholder="Search title, SKU, label" value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && void search()} />
          <button type="button" className={btn2Cls} onClick={() => void search()}>Search</button>
        </div>
        {rows.map((r) => (
          <div key={r.id} className="flex flex-wrap items-center justify-between gap-2 border-t border-gray-100 pt-2 text-sm">
            <div className="min-w-0">
              <div className="truncate font-medium">{r.title}</div>
              <div className="text-xs text-gray-500">{r.sku} · shows: {modeLabel(r.shown)}</div>
            </div>
            <select className={`${inputCls} w-auto`} value={r.stock_display ?? ""} disabled={work} onChange={(e) => void apply((e.target.value || null) as "available" | "count" | null, { product_ids: [r.id] })}>
              <option value="">Store default</option>
              <option value="available">Available</option>
              <option value="count">Number (35 Available)</option>
            </select>
          </div>
        ))}
      </div>
    </div>
  );
}
