"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Badge, Notice, Pager, btnCls, btn2Cls, inputCls } from "./ui";

/* Availability: what is at 0 (and why), drop ship overrides for kits, and how the website shows stock.
   Backed by /admin/availability/* . The numbers count the distributor's stock only when the master switch is ON. */

type Settings = { use_distributor_stock: boolean; auto_raise: boolean; display_default: "available" | "count" };
type Piece = { part_number: string | null; per_product: number; in_catalog: boolean; distributor_stock: number };
type ListingRef = { listing_id: number; store: string; item_id: string; dropship_ok: boolean };
type Item = {
  product_id: number;
  sku: string | null;
  title: string | null;
  custom_label: string | null;
  on_hand: number;
  own_available: number;
  distributor: number;
  piece_count: number;
  dropship_ok: boolean;
  published: boolean;
  reason: string;
  pieces: Piece[];
  listings: ListingRef[];
};
type Zero = { total: number; page: number; per_page: number; items: Item[]; settings: Settings };
type DisplayRow = { id: number; sku: string | null; title: string | null; stock_display: string | null; shown: string };

const PER = 50;
const modeLabel = (m: string | null) => (m === "count" ? "Number (35 Available)" : m === "available" ? "Available" : "Store default");

export default function AvailabilityAdmin() {
  const [tab, setTab] = useState<"zero" | "display">("zero");
  const [settings, setSettings] = useState<Settings | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const loadSettings = useCallback(async () => {
    try {
      setSettings(await api<Settings>("/availability/settings"));
    } catch (e) {
      setErr(errMsg(e));
    }
  }, []);
  useEffect(() => {
    void loadSettings();
  }, [loadSettings]);

  async function save(patch: Partial<{ use_distributor_stock: boolean; auto_raise: boolean; display_default: string }>) {
    setErr(null);
    setNote(null);
    setBusy(true);
    try {
      setSettings(await api<Settings>("/availability/settings", { method: "PUT", body: patch }));
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  const tabBtn = (k: "zero" | "display", label: string) => (
    <button
      type="button"
      onClick={() => setTab(k)}
      className={`rounded px-3 py-2 text-sm font-medium ${tab === k ? "bg-black text-white" : "border border-gray-300 bg-white text-gray-800 hover:bg-gray-50"}`}
    >
      {label}
    </button>
  );

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Availability</h1>

      {err && <Notice kind="error">{err}</Notice>}
      {note && <Notice kind="success">{note}</Notice>}

      {settings && (
        <div className="max-w-3xl space-y-3 rounded border border-gray-200 bg-white p-4">
          <label className="flex items-start gap-3 text-sm">
            <input
              type="checkbox"
              className="mt-1"
              checked={settings.use_distributor_stock}
              disabled={busy}
              onChange={(e) => void save({ use_distributor_stock: e.target.checked })}
            />
            <span>
              <span className="font-semibold">Count distributor stock</span> (Automatic Distributors, Indiana + Nevada). A single-part product is available if
              we have it or the distributor does. A kit is available from our own shelf only, unless drop shipping is allowed for it below. When
              this is OFF, everything works from our own shelf count only, as before.
            </span>
          </label>
          <label className="flex items-start gap-3 text-sm">
            <input
              type="checkbox"
              className="mt-1"
              checked={settings.auto_raise}
              disabled={busy || !settings.use_distributor_stock}
              onChange={(e) => void save({ auto_raise: e.target.checked })}
            />
            <span>
              <span className="font-semibold">Raise listings automatically</span> when the distributor gets stock back. When this is OFF, a listing that went
              to 0 stays at 0 until its stock changes some other way (a receipt, a manual change).
            </span>
          </label>
          {!settings.use_distributor_stock && (
            <Notice kind="info">
              The switch is OFF. The list below shows what WOULD be at 0 with distributor stock counted; eBay and the website still use the shelf count only.
            </Notice>
          )}
        </div>
      )}

      <div className="flex gap-2">
        {tabBtn("zero", "At 0")}
        {tabBtn("display", "Website display")}
      </div>

      {tab === "zero" ? (
        <ZeroTab settings={settings} setErr={setErr} setNote={setNote} />
      ) : (
        <DisplayTab settings={settings} save={save} setErr={setErr} setNote={setNote} busy={busy} />
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ At 0 */

function ZeroTab({
  settings,
  setErr,
  setNote,
}: {
  settings: Settings | null;
  setErr: (s: string | null) => void;
  setNote: (s: string | null) => void;
}) {
  const [scope, setScope] = useState<"listings" | "website" | "all">("listings");
  const [q, setQ] = useState("");
  const [applied, setApplied] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<Zero | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const qs = new URLSearchParams({ scope, page: String(Math.floor(offset / PER) + 1), per_page: String(PER) });
      if (applied.trim()) qs.set("q", applied.trim());
      setData(await api<Zero>(`/availability/zero?${qs}`));
      setErr(null);
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setLoading(false);
    }
  }, [scope, applied, offset, setErr]);
  useEffect(() => {
    void load();
  }, [load]);

  async function toggle(path: string, on: boolean) {
    setErr(null);
    setNote(null);
    setBusy(true);
    try {
      const r = await api<{ queued: number }>(path, { method: "POST", body: { on } });
      setNote(
        on
          ? `Drop shipping allowed.${r.queued ? ` ${r.queued} eBay listing${r.queued === 1 ? "" : "s"} queued to update.` : ""}`
          : `Drop shipping turned off.${r.queued ? ` ${r.queued} eBay listing${r.queued === 1 ? "" : "s"} queued to update.` : ""}`
      );
      await load();
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  async function applyZeros() {
    if (!window.confirm("Queue an eBay quantity update of 0 for every listing in this list's products that is at 0 now? Only stores with live pushes and sales quantity updates ON are touched.")) return;
    setErr(null);
    setNote(null);
    setBusy(true);
    try {
      const r = await api<{ products: number; listings: number }>("/availability/apply-zeros", { method: "POST" });
      setNote(`Queued ${r.listings.toLocaleString()} eBay listing${r.listings === 1 ? "" : "s"} for ${r.products.toLocaleString()} product${r.products === 1 ? "" : "s"}. They are sent within about a minute.`);
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-2">
        <select
          className={`${inputCls} w-auto`}
          value={scope}
          onChange={(e) => {
            setScope(e.target.value as typeof scope);
            setOffset(0);
          }}
        >
          <option value="listings">With an active eBay listing</option>
          <option value="website">Published on the website</option>
          <option value="all">Either</option>
        </select>
        <input
          className={`${inputCls} w-64`}
          placeholder="Search title, SKU, label"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              setApplied(q);
              setOffset(0);
            }
          }}
        />
        <button type="button" className={btn2Cls} onClick={() => { setApplied(q); setOffset(0); }}>
          Search
        </button>
        <button type="button" className={btnCls} disabled={busy || !settings?.use_distributor_stock} onClick={() => void applyZeros()} title={settings?.use_distributor_stock ? "" : "Turn on Count distributor stock first"}>
          Set listings to 0 now
        </button>
      </div>

      {data && <Pager total={data.total} limit={PER} offset={offset} onChange={setOffset} />}
      {loading && <p className="text-sm text-gray-500">Loading…</p>}

      <div className="space-y-2">
        {data?.items.map((it) => (
          <div key={it.product_id} className="rounded border border-gray-200 bg-white p-3 text-sm">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="font-medium">{it.title}</div>
                <div className="text-xs text-gray-500">
                  {it.sku}
                  {it.custom_label ? ` · ${it.custom_label}` : ""}
                </div>
              </div>
              <div className="flex flex-wrap gap-1">
                <Badge tone="gray">{it.piece_count > 1 ? `Kit of ${it.piece_count}` : it.piece_count === 1 ? "Single part" : "No distributor part"}</Badge>
                {it.published && <Badge tone="blue">On website</Badge>}
                {it.dropship_ok && <Badge tone="amber">Drop ship allowed</Badge>}
              </div>
            </div>
            <p className="mt-1 text-gray-700">{it.reason}</p>
            <p className="mt-1 text-xs text-gray-500">
              On our shelf: {it.on_hand} (available {it.own_available}) · Distributor: {it.distributor}
            </p>
            {it.pieces.length > 0 && (
              <ul className="mt-1 text-xs text-gray-600">
                {it.pieces.map((p, i) => (
                  <li key={i}>
                    {p.part_number ?? "(no number)"} × {p.per_product}: distributor has {p.distributor_stock}
                    {p.in_catalog ? "" : " (not in the live catalog)"}
                  </li>
                ))}
              </ul>
            )}
            <div className="mt-2 flex flex-wrap items-center gap-2">
              {it.piece_count > 1 && (
                <button type="button" className={btn2Cls} disabled={busy} onClick={() => void toggle(`/availability/products/${it.product_id}/dropship`, !it.dropship_ok)}>
                  {it.dropship_ok ? "Stop drop shipping this kit" : "Allow drop shipping for every listing"}
                </button>
              )}
              {it.listings.map((l) => (
                <button
                  key={l.listing_id}
                  type="button"
                  className={btn2Cls}
                  disabled={busy || it.dropship_ok}
                  title={it.dropship_ok ? "Already allowed for every listing of this product" : ""}
                  onClick={() => void toggle(`/availability/listings/${l.listing_id}/dropship`, !l.dropship_ok)}
                >
                  {l.store} {l.item_id}: {l.dropship_ok ? "stop drop shipping" : "allow drop shipping"}
                </button>
              ))}
            </div>
          </div>
        ))}
        {data && data.items.length === 0 && !loading && <p className="text-sm text-gray-500">Nothing is at 0 here.</p>}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ website display */

function DisplayTab({
  settings,
  save,
  setErr,
  setNote,
  busy,
}: {
  settings: Settings | null;
  save: (p: { display_default: string }) => Promise<void>;
  setErr: (s: string | null) => void;
  setNote: (s: string | null) => void;
  busy: boolean;
}) {
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<DisplayRow[]>([]);
  const [work, setWork] = useState(false);

  async function search() {
    try {
      const r = await api<{ items: DisplayRow[] }>(`/availability/display/products?q=${encodeURIComponent(q)}`);
      setRows(r.items);
      setErr(null);
    } catch (e) {
      setErr(errMsg(e));
    }
  }

  async function apply(mode: "available" | "count" | null, body: { all_products?: boolean; product_ids?: number[] }) {
    setErr(null);
    setNote(null);
    setWork(true);
    try {
      const r = await api<{ updated: number }>("/availability/display", { method: "POST", body: { mode, ...body } });
      setNote(`Updated ${r.updated.toLocaleString()} product${r.updated === 1 ? "" : "s"}.`);
      if (rows.length) await search();
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setWork(false);
    }
  }

  return (
    <div className="max-w-3xl space-y-4">
      <p className="text-sm text-gray-600">
        The website shows <b>Available</b>, <b>35 Available</b> (the number) or <b>Out of stock</b>. Out of stock always shows when nothing can be sold; the products stay visible.
      </p>

      <div className="space-y-2 rounded border border-gray-200 bg-white p-4">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="font-semibold">Store default:</span>
          <select className={`${inputCls} w-auto`} value={settings?.display_default ?? "available"} disabled={busy || !settings} onChange={(e) => void save({ display_default: e.target.value })}>
            <option value="available">Available</option>
            <option value="count">Number (35 Available)</option>
          </select>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="font-semibold">All products:</span>
          <button type="button" className={btn2Cls} disabled={work} onClick={() => window.confirm("Set EVERY product to Available?") && void apply("available", { all_products: true })}>
            Set all to Available
          </button>
          <button type="button" className={btn2Cls} disabled={work} onClick={() => window.confirm("Set EVERY product to show the number?") && void apply("count", { all_products: true })}>
            Set all to Number
          </button>
          <button type="button" className={btn2Cls} disabled={work} onClick={() => window.confirm("Make every product follow the store default?") && void apply(null, { all_products: true })}>
            All follow the store default
          </button>
        </div>
      </div>

      <div className="space-y-2 rounded border border-gray-200 bg-white p-4">
        <div className="font-semibold text-sm">One product at a time</div>
        <div className="flex flex-wrap gap-2">
          <input className={`${inputCls} w-64`} placeholder="Search title, SKU, label" value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && void search()} />
          <button type="button" className={btn2Cls} onClick={() => void search()}>
            Search
          </button>
        </div>
        {rows.map((r) => (
          <div key={r.id} className="flex flex-wrap items-center justify-between gap-2 border-t border-gray-100 pt-2 text-sm">
            <div className="min-w-0">
              <div className="truncate font-medium">{r.title}</div>
              <div className="text-xs text-gray-500">
                {r.sku} · shows: {modeLabel(r.shown)}
              </div>
            </div>
            <select
              className={`${inputCls} w-auto`}
              value={r.stock_display ?? ""}
              disabled={work}
              onChange={(e) => void apply((e.target.value || null) as "available" | "count" | null, { product_ids: [r.id] })}
            >
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
