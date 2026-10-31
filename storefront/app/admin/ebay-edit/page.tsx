"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Badge, Notice, btnCls, btn2Cls, inputCls } from "../../../components/admin/ui";

type Item = {
  store: string;
  item_id: string;
  title: string;
  price: string | null;
  currency: string;
  quantity_available: number;
  quantity_sold: number;
  sku: string;
  listing_type: string;
  status: string;
  url: string;
  image_url: string;
  description_chars: number;
  editable: boolean;
  not_editable_reason: string | null;
};
type Diff = { field: string; before: string | null; after: string; note?: string };
type Preview = {
  call: string;
  diff: Diff[];
  xml: string;
  fingerprint: string;
  live_pushes_enabled: boolean;
};
type LogRow = {
  id: number;
  created_at: string;
  user_email: string | null;
  item_id: string;
  action: string;
  dry_run: boolean;
  ok: boolean;
  error: string | null;
};

export default function EbayEditPage() {
  const [store, setStore] = useState("");
  const [itemId, setItemId] = useState("");
  const [item, setItem] = useState<Item | null>(null);
  const [title, setTitle] = useState("");
  const [price, setPrice] = useState("");
  const [qty, setQty] = useState("");
  const [sku, setSku] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [log, setLog] = useState<LogRow[]>([]);
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const [busy, setBusy] = useState("");
  const started = useRef(false);

  function fill(i: Item) {
    setItem(i);
    setTitle(i.title);
    setPrice(i.price ?? "");
    setQty(String(i.quantity_available));
    setSku(i.sku);
    setPreview(null);
  }

  async function loadLog(s: string, id: string) {
    try {
      const r = await api<{ rows: LogRow[] }>(`/ebay-edit/log?store=${encodeURIComponent(s)}&item_id=${encodeURIComponent(id)}&limit=10`);
      setLog(r.rows);
    } catch {
      /* the log is a convenience; never block the page on it */
    }
  }

  async function load(s: string, id: string) {
    setBusy("load");
    setMsg(null);
    try {
      fill(await api<Item>(`/ebay-edit/item?store=${encodeURIComponent(s)}&item_id=${encodeURIComponent(id)}`));
      void loadLog(s, id);
    } catch (e) {
      setItem(null);
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const p = new URLSearchParams(window.location.search);
    const s = p.get("store") ?? "";
    const id = p.get("item") ?? "";
    setStore(s);
    setItemId(id);
    if (s && id) void load(s, id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function changes() {
    return { title, price, quantity: qty.trim() === "" ? null : Number(qty), sku };
  }

  async function doPreview() {
    if (!item) return;
    setBusy("preview");
    setMsg(null);
    setPreview(null);
    try {
      setPreview(await api<Preview>("/ebay-edit/preview", { method: "POST", body: { store, item_id: item.item_id, changes: changes() } }));
      void loadLog(store, item.item_id);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  async function doPush() {
    if (!item || !preview) return;
    const list = preview.diff.map((d) => `${d.field}: ${d.before ?? "—"} → ${d.after}`).join("\n");
    if (!window.confirm(`Send this to eBay (${store}) for item ${item.item_id}?\n\n${list}\n\nThis changes the live listing.`)) return;
    setBusy("push");
    setMsg(null);
    try {
      const r = await api<{ item: Item; warnings: { message: string }[] }>("/ebay-edit/push", {
        method: "POST",
        body: { store, item_id: item.item_id, changes: changes(), fingerprint: preview.fingerprint },
      });
      fill(r.item);
      setMsg({ kind: "success", text: `Sent. eBay accepted the change.${r.warnings.length ? " eBay warnings: " + r.warnings.map((w) => w.message).join("; ") : ""}` });
      void loadLog(store, item.item_id);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
      void loadLog(store, item.item_id);
    } finally {
      setBusy("");
    }
  }

  const dirty = !!item && (title !== item.title || price !== (item.price ?? "") || qty !== String(item.quantity_available) || sku !== item.sku);

  return (
    <div className="max-w-4xl space-y-5">
      <div className="flex items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">Edit eBay listing</h1>
        <Link href="/admin/ebay-listings" className={btn2Cls}>
          Back to Live listings
        </Link>
      </div>

      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}
      {busy === "load" && <p className="text-sm text-gray-600">Reading the listing from eBay…</p>}

      {!item && busy !== "load" && !msg && (
        <Notice>Open this page from the Edit link on a listing in Live listings.</Notice>
      )}

      {item && (
        <>
          <div className="flex gap-4 rounded border border-gray-200 bg-white p-4">
            {item.image_url ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={item.image_url} alt="" className="h-20 w-20 rounded object-cover" />
            ) : null}
            <div className="space-y-1 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{store}</span>
                <a href={item.url} target="_blank" rel="noreferrer" className="text-blue-700 hover:underline">
                  {item.item_id}
                </a>
                <Badge tone="gray">{item.status || "—"}</Badge>
                <span className="text-gray-500">{item.quantity_sold} sold</span>
              </div>
              <div className="text-gray-500">Description ({item.description_chars.toLocaleString("en-US")} characters), photos and item specifics are not editable here yet.</div>
            </div>
          </div>

          {!item.editable && <Notice kind="error">{item.not_editable_reason}</Notice>}

          <div className="space-y-4 rounded border border-gray-200 bg-white p-4">
            <label className="block text-sm font-medium text-gray-700">
              <span className="mb-1 block">
                Title <span className="font-normal text-gray-500">({title.length}/80)</span>
              </span>
              <input className={inputCls} value={title} maxLength={80} disabled={!item.editable} onChange={(e) => { setTitle(e.target.value); setPreview(null); }} />
            </label>
            <div className="grid gap-4 sm:grid-cols-3">
              <label className="block text-sm font-medium text-gray-700">
                <span className="mb-1 block">Price ({item.currency})</span>
                <input className={inputCls} inputMode="decimal" value={price} disabled={!item.editable} onChange={(e) => { setPrice(e.target.value); setPreview(null); }} />
              </label>
              <label className="block text-sm font-medium text-gray-700">
                <span className="mb-1 block">Available quantity</span>
                <input className={inputCls} inputMode="numeric" value={qty} disabled={!item.editable} onChange={(e) => { setQty(e.target.value); setPreview(null); }} />
              </label>
              <label className="block text-sm font-medium text-gray-700">
                <span className="mb-1 block">Custom label (SKU)</span>
                <input className={inputCls} value={sku} maxLength={50} disabled={!item.editable} onChange={(e) => { setSku(e.target.value); setPreview(null); }} />
              </label>
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <button type="button" className={btnCls} disabled={!item.editable || !dirty || busy !== ""} onClick={() => void doPreview()}>
                {busy === "preview" ? "Checking…" : "Preview what would be sent"}
              </button>
              <button type="button" className={btn2Cls} disabled={busy !== "" || !dirty} onClick={() => fill(item)}>
                Reset
              </button>
              <span className="text-xs text-gray-500">A preview never sends anything to eBay.</span>
            </div>
          </div>

          {preview && (
            <div className="space-y-3 rounded border border-amber-300 bg-amber-50 p-4">
              <h2 className="text-sm font-semibold">This is exactly what would be sent</h2>
              <table className="w-full text-left text-sm">
                <thead className="text-xs uppercase text-gray-500">
                  <tr>
                    <th className="py-1 pr-3">Field</th>
                    <th className="py-1 pr-3">On eBay now</th>
                    <th className="py-1">Would become</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.diff.map((d) => (
                    <tr key={d.field} className="align-top">
                      <td className="py-1 pr-3 font-medium">{d.field}</td>
                      <td className="py-1 pr-3">{d.before ?? "—"}</td>
                      <td className="py-1">
                        {d.after}
                        {d.note && <div className="text-xs text-gray-500">{d.note}</div>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <details className="text-xs">
                <summary className="cursor-pointer text-gray-600">The request itself ({preview.call})</summary>
                <pre className="mt-2 overflow-x-auto rounded bg-white p-2">{preview.xml}</pre>
              </details>
              <div className="flex flex-wrap items-center gap-3">
                <button type="button" className={btnCls} disabled={!preview.live_pushes_enabled || busy !== ""} onClick={() => void doPush()}>
                  {busy === "push" ? "Sending…" : "Send to eBay"}
                </button>
                {!preview.live_pushes_enabled && (
                  <span className="text-sm text-gray-700">
                    Live pushes are OFF for {store}. Turn them on in the Stores box on Live listings to send.
                  </span>
                )}
              </div>
            </div>
          )}

          <div className="rounded border border-gray-200 bg-white p-4">
            <h2 className="text-sm font-semibold">Recent activity on this listing</h2>
            <ul className="mt-2 divide-y divide-gray-100 text-sm">
              {log.map((l) => (
                <li key={l.id} className="flex flex-wrap items-center gap-2 py-1.5">
                  <span className="text-gray-500">{new Date(l.created_at).toLocaleString()}</span>
                  <Badge tone={l.dry_run ? "gray" : l.ok ? "green" : "amber"}>{l.dry_run ? "Preview" : l.ok ? "Sent" : "Failed"}</Badge>
                  <span className="text-gray-600">{l.user_email ?? ""}</span>
                  {l.error && <span className="text-red-700">{l.error}</span>}
                </li>
              ))}
              {log.length === 0 && <li className="py-1.5 text-gray-500">Nothing yet.</li>}
            </ul>
          </div>
        </>
      )}
    </div>
  );
}
