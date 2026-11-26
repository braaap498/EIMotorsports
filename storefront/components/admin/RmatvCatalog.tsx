"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Badge, Modal, Notice, Pager, btn2Cls, inputCls } from "./ui";

/**
 * The Rocky Mountain ATV/MC catalog in detail (owners and admins only; licensed data, never shown on the public store). It is the
 * Automatic Distributors catalog screen with Rocky Mountain's single stock number instead of warehouses:
 *  - Overview: totals, data gaps, stock and stock value, price bands, units of measure, every brand.
 *  - Browser: search any column, filter on brand / stock / price / margin / data gaps / "already has a product" / recently
 *    changed, sort on any column, and open any part to see everything stored about it.
 *  - Make product: the part's single product, with the part's photo links as its photos (downloaded into our storage).
 */

type Totals = {
  total: number; active: number; retired: number; in_stock: number; out_of_stock: number;
  units: number; oem_parts?: number; in_utah?: number; in_kentucky?: number; units_utah?: number; units_kentucky?: number;
  stock_value_at_cost: string | number; stock_value_at_list: string | number; brands: number;
  no_barcode: number; no_fitment: number; no_mfr_number: number; no_name: number; no_cost: number; no_list_price: number; list_below_cost: number; no_photo: number;
  changed_7d: number; changed_30d: number; avg_margin_pct: string | number | null; last_seen: string | null;
};
type BrandRow = {
  brand: string; unbranded: boolean; parts: number; in_stock: number; units: number;
  stock_value_at_cost: string | number; min_cost: string | null; max_cost: string | null; avg_cost: string | number | null;
};
type Overview = {
  totals: Totals;
  brands: BrandRow[];
  price_bands: { band: string; parts: number }[];
  units_of_measure: { uom: string; unset: boolean; parts: number }[];
};
type Row = {
  id: number; sku: string; mfr_number: string | null; manufacturer: string | null; name: string | null;
  unit_of_measure: string | null; list_price: number | null; dealer_cost: number | null; standard_cost: number | null; my_cost: number | null; map_price: number | null;
  qty_other: number; qty_utah: number | null; qty_kentucky: number | null; is_oem?: boolean; msrp: number | null; qty_total: number;
  upc: string | null; images: string[] | null; active: boolean; updated_at: string | null; last_seen_at: string | null;
  margin: number | null; margin_pct: number | null;
  product_id: number | null; // the SINGLE product of this part (never a kit that contains it)
  product_title: string | null;
  product_sku: string | null; // the EI- number
  products: number;
};
type Summary = {
  total: number; in_stock: number; brands: number; min_cost: string | null; max_cost: string | null;
  avg_cost: string | number | null; stock_value_at_cost: string | number; units: number;
};
type Mini = {
  id: number; sku: string; mfr_number: string | null; manufacturer: string | null; name: string | null;
  list_price: string | null; dealer_cost: string | null; qty_other: number;
  upc: string | null; active: boolean;
};
type Detail = {
  item: Record<string, unknown>;
  last_run: { id: number; source: string; status: string; started_at: string; finished_at: string | null; rows_seen: number } | null;
  ours: { id: number; sku: string; title: string; on_hand: number; list_price: string | null; cost: string | null; is_active: boolean; by: string }[];
  same_barcode: Mini[];
  same_part_number: Mini[];
  brand_part_count: number;
};

type Filters = {
  q: string; field: string; brand: string; status: string; stock: string; warehouse: string; source: string; uom: string;
  min_qty: string; max_qty: string; min_cost: string; max_cost: string; min_list: string; max_list: string;
  min_margin_pct: string; max_margin_pct: string; missing: string; ours: string; changed_days: string; seen_days: string;
};
const EMPTY: Filters = {
  q: "", field: "all", brand: "", status: "active", stock: "", warehouse: "", source: "", uom: "",
  min_qty: "", max_qty: "", min_cost: "", max_cost: "", min_list: "", max_list: "",
  min_margin_pct: "", max_margin_pct: "", missing: "", ours: "", changed_days: "", seen_days: "",
};
const DEFAULTS_ONLY = (f: Filters) => JSON.stringify(f) === JSON.stringify(EMPTY);

const n = (x: number | string | null | undefined) => Number(x ?? 0).toLocaleString("en-US");
const money = (v: string | number | null | undefined) =>
  v == null || v === "" ? "—" : `$${Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const money0 = (v: string | number | null | undefined) => `$${Number(v ?? 0).toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
const pct = (v: string | number | null | undefined) => (v == null ? "—" : `${Number(v).toFixed(0)}%`);
const when = (v: unknown) => (typeof v === "string" && v ? new Date(v).toLocaleString() : "—");

const MISSING_LABEL: Record<string, string> = {
  upc: "no UPC", mfr: "no manufacturer number", name: "no name", cost: "no cost", list: "no list price",
  below_cost: "list price below cost", photo: "no photo link", fitment: "no fitment",
};

type SortKey =
  | "brand" | "sku" | "mfr" | "name" | "uom" | "list" | "cost" | "margin" | "margin_pct"
  | "stock" | "updated" | "seen";

function Stat({ label, value, sub, onClick }: { label: string; value: string; sub?: string; onClick?: () => void }) {
  const body = (
    <>
      <div className="text-xl font-semibold leading-tight">{value}</div>
      <div className="text-xs text-gray-600">{label}</div>
      {sub && <div className="text-[11px] text-gray-400">{sub}</div>}
    </>
  );
  return onClick ? (
    <button type="button" onClick={onClick} className="rounded border border-gray-200 bg-white px-3 py-2 text-left hover:border-gray-400 hover:bg-gray-50">
      {body}
    </button>
  ) : (
    <div className="rounded border border-gray-200 bg-white px-3 py-2">{body}</div>
  );
}

function Lbl({ text, children }: { text: string; children: ReactNode }) {
  return (
    <label className="block text-xs font-medium text-gray-600">
      {text}
      <div className="mt-0.5 font-normal">{children}</div>
    </label>
  );
}

/* What the unit-of-measure codes in the feed stand for. Only codes listed here get a meaning shown; anything else
   shows just the code, so nothing is guessed. */
const UOM_NAMES: Record<string, string> = {
  EA: "Each",
  PR: "Pair",
  SET: "Set",
  ST: "Set",
  KT: "Kit",
  KIT: "Kit",
  PK: "Pack",
  PKG: "Package",
  PC: "Piece",
  PCS: "Pieces",
  BX: "Box",
  CS: "Case",
  CA: "Case",
  CT: "Carton",
  DZ: "Dozen",
  RL: "Roll",
  FT: "Foot",
  YD: "Yard",
  IN: "Inch",
  GL: "Gallon",
  GAL: "Gallon",
  QT: "Quart",
  PT: "Pint",
  OZ: "Ounce",
  LB: "Pound",
  LT: "Liter",
  ML: "Milliliter",
  BG: "Bag",
  BT: "Bottle",
  CN: "Can",
  TB: "Tube",
  PL: "Pail",
  DR: "Drum",
  SH: "Sheet",
};
const uomName = (code: string | null | undefined) => (code ? UOM_NAMES[code.trim().toUpperCase()] : undefined);

export default function RmatvCatalog({ hasData, version }: { hasData: boolean; version: number }) {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [showBrands, setShowBrands] = useState(false);
  const [overviewOpen, setOverviewOpen] = useState(false);
  const [brandQ, setBrandQ] = useState("");
  const [f, setF] = useState<Filters>(EMPTY);
  const [qInput, setQInput] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [sort, setSort] = useState<SortKey>("brand");
  const [dir, setDir] = useState<"asc" | "desc">("asc");
  const [limit, setLimit] = useState(50);
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<{ total: number; rows: Row[]; summary: Summary } | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [flash, setFlash] = useState<{ kind: "success" | "error" | "info"; text: string } | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [tick, setTick] = useState(0); // bumps after a product is made, so the list and the overview reload

  const set = useCallback((patch: Partial<Filters>) => {
    setF((p) => ({ ...p, ...patch }));
    setOffset(0);
  }, []);

  useEffect(() => {
    if (!hasData) return;
    api<Overview>("/rmatv/overview")
      .then(setOverview)
      .catch((e) => setErr(errMsg(e)));
  }, [hasData, version, tick]);

  // typing in the search box waits a moment before searching
  useEffect(() => {
    const t = setTimeout(() => {
      setF((p) => (p.q === qInput ? p : { ...p, q: qInput }));
      setOffset(0);
    }, 350);
    return () => clearTimeout(t);
  }, [qInput]);

  const queryString = useMemo(() => {
    const p = new URLSearchParams();
    (Object.keys(f) as (keyof Filters)[]).forEach((k) => {
      if (f[k].trim() !== "") p.set(k, f[k].trim());
    });
    p.set("sort", sort);
    p.set("dir", dir);
    p.set("limit", String(limit));
    p.set("offset", String(offset));
    return p.toString();
  }, [f, sort, dir, limit, offset]);

  useEffect(() => {
    if (!hasData) return;
    let cancelled = false;
    setLoading(true);
    api<{ total: number; rows: Row[]; summary: Summary }>(`/rmatv/items?${queryString}`)
      .then((d) => {
        if (!cancelled) {
          setData(d);
          setErr(null);
        }
      })
      .catch((e) => !cancelled && setErr(errMsg(e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [hasData, queryString, version, tick]);

  function clickSort(k: SortKey) {
    if (k === sort) setDir((d) => (d === "asc" ? "desc" : "asc"));
    else {
      setSort(k);
      setDir(["list", "cost", "margin", "margin_pct", "stock", "updated", "seen"].includes(k) ? "desc" : "asc");
    }
    setOffset(0);
  }
  const reset = () => {
    setF(EMPTY);
    setQInput("");
    setOffset(0);
  };
  const openDetail = (id: number) =>
    api<Detail>(`/rmatv/items/${id}`)
      .then(setDetail)
      .catch((e) => setErr(errMsg(e)));

  async function makeProduct(r: Row) {
    if (!window.confirm(`Make a product from ${r.sku}?\n\nIt is created unpublished, with this part as its distributor part and the custom label set to the SKU when that is free. The part's photo links become the product's photos and are saved into our storage.`)) return;
    setBusyId(r.id);
    setFlash(null);
    try {
      const m = await api<{ product_id: number; title: string; custom_label: string | null; photos: number; photos_saving: boolean; fitment: { entries?: number; unreadable?: number; makes_created?: number; models_created?: number; fitment_added?: number; error?: boolean } | null }>(`/rmatv/items/${r.id}/make-product`, { method: "POST" });
      setFlash({
        kind: "info",
        text: `Made product #${m.product_id}: ${m.title}${m.custom_label ? ` (custom label ${m.custom_label})` : ". Its custom label was left blank: that number is already a label or cannot be one"}. ${
          m.photos ? `${m.photos} photo${m.photos === 1 ? "" : "s"} ${m.photos_saving ? "are being saved to storage" : "will be saved when the next photo run starts (Import tab, Photos)"}.` : "This part has no photo link."
        }${
          m.fitment ? (m.fitment.error ? " The fitment could not be added; add it with the fitment tool." : ` Fitment: ${m.fitment.fitment_added ?? 0} vehicle-year${m.fitment.fitment_added === 1 ? "" : "s"} added${m.fitment.makes_created || m.fitment.models_created ? ` (new to the fitment tool: ${m.fitment.makes_created ?? 0} make${m.fitment.makes_created === 1 ? "" : "s"}, ${m.fitment.models_created ?? 0} model${m.fitment.models_created === 1 ? "" : "s"})` : ""}${m.fitment.unreadable ? `; ${m.fitment.unreadable} entr${m.fitment.unreadable === 1 ? "y" : "ies"} could not be read` : ""}.`) : ""
        } It is NOT published; set a price and category on the product.`,
      });
      setTick((x) => x + 1);
    } catch (e) {
      setFlash({ kind: "error", text: errMsg(e) });
    } finally {
      setBusyId(null);
    }
  }

  const brandRows = useMemo(() => {
    const b = overview?.brands ?? [];
    const w = brandQ.trim().toLowerCase();
    return w ? b.filter((x) => x.brand.toLowerCase().includes(w)) : b;
  }, [overview, brandQ]);

  const t = overview?.totals;
  const activeChips: { label: string; clear: () => void }[] = [];
  if (f.brand) activeChips.push({ label: `Brand: ${f.brand}`, clear: () => set({ brand: "" }) });
  if (f.status !== "active") activeChips.push({ label: f.status === "all" ? "Including retired" : "Retired only", clear: () => set({ status: "active" }) });
  if (f.source) activeChips.push({ label: f.source === "oem" ? "OEM parts only" : "Main catalog only", clear: () => set({ source: "" }) });
  if (f.warehouse) activeChips.push({ label: `Stock in ${f.warehouse === "utah" ? "Utah" : "Kentucky"}`, clear: () => set({ warehouse: "" }) });
  if (f.stock) activeChips.push({ label: f.stock === "in" ? "In stock" : "Out of stock", clear: () => set({ stock: "" }) });
  if (f.uom) activeChips.push({ label: `Unit: ${f.uom}`, clear: () => set({ uom: "" }) });
  if (f.missing) activeChips.push({ label: `Data gap: ${MISSING_LABEL[f.missing]}`, clear: () => set({ missing: "" }) });
  if (f.ours) activeChips.push({ label: f.ours === "yes" ? "Has a product" : "No product yet", clear: () => set({ ours: "" }) });
  if (f.changed_days) activeChips.push({ label: `Changed in ${f.changed_days} days`, clear: () => set({ changed_days: "" }) });
  if (f.seen_days) activeChips.push({ label: `In the files within ${f.seen_days} days`, clear: () => set({ seen_days: "" }) });
  const ranges: [string, string, string, string][] = [
    ["min_cost", "max_cost", "Cost", "$"], ["min_list", "max_list", "List", "$"],
    ["min_qty", "max_qty", "Stock", ""], ["min_margin_pct", "max_margin_pct", "Margin", "%"],
  ];
  ranges.forEach(([lo, hi, name, unit]) => {
    const a = f[lo as keyof Filters];
    const b = f[hi as keyof Filters];
    if (a || b)
      activeChips.push({
        label: `${name}: ${a ? `${unit === "$" ? "$" : ""}${a}${unit === "%" ? "%" : ""}` : "any"} to ${b ? `${unit === "$" ? "$" : ""}${b}${unit === "%" ? "%" : ""}` : "any"}`,
        clear: () => set({ [lo]: "", [hi]: "" } as Partial<Filters>),
      });
  });

  function Th({ k, children, title, right }: { k: SortKey; children: ReactNode; title?: string; right?: boolean }) {
    const on = sort === k;
    return (
      <th className={`whitespace-nowrap py-1 pr-3 ${right ? "text-right" : ""}`} title={title}>
        <button type="button" onClick={() => clickSort(k)} className={`uppercase hover:text-black ${on ? "text-black" : ""}`}>
          {children} <span className={on ? "" : "text-gray-300"}>{on ? (dir === "asc" ? "▲" : "▼") : "▲▼"}</span>
        </button>
      </th>
    );
  }

  if (!hasData) {
    return (
      <div className="rounded border border-gray-200 bg-white p-4 text-sm text-gray-500">
        No Rocky Mountain parts are stored yet. Open the Import tab: connect to the SFTP feed and sync, or upload the catalog file from the Rocky Mountain dealer site.
      </div>
    );
  }

  return (
    <div className="space-y-5">
      {err && <Notice kind="error">{err}</Notice>}
      {flash && <Notice kind={flash.kind}>{flash.text}</Notice>}

      {/* ---------------------------------------------------------------- overview */}
      <div className="space-y-3 rounded border border-gray-200 bg-white p-4 text-sm">
        <button
          type="button"
          className="flex w-full flex-wrap items-center justify-between gap-2 text-left"
          onClick={() => setOverviewOpen((o) => !o)}
          aria-expanded={overviewOpen}
        >
          <span className="font-semibold">
            <span className="mr-2 inline-block w-3 text-gray-500">{overviewOpen ? "▾" : "▸"}</span>Catalog overview
          </span>
          <span className="text-xs text-gray-500">
            {overviewOpen ? "Click any number to browse those parts." : t ? `${n(t.active)} active parts · ${n(t.in_stock)} in stock` : ""}
          </span>
        </button>
        {overviewOpen && (!t ? (
          <p className="text-gray-500">Loading…</p>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-7">
              <Stat label="active parts" value={n(t.active)} sub={t.retired ? `${n(t.retired)} retired` : undefined} onClick={reset} />
              <Stat label="in stock at Rocky Mountain" value={n(t.in_stock)} onClick={() => set({ stock: "in", status: "active" })} />
              <Stat label="OEM parts" value={n(t.oem_parts ?? 0)} onClick={() => set({ source: "oem", status: "active" })} />
              <Stat label="out of stock" value={n(t.out_of_stock)} onClick={() => set({ stock: "out", status: "active" })} />
              <Stat label="brands" value={n(t.brands)} onClick={() => setShowBrands(true)} />
              <Stat label="avg margin (list vs cost)" value={t.avg_margin_pct == null ? "—" : `${Number(t.avg_margin_pct).toFixed(1)}%`} />
              <Stat label="changed in last 7 days" value={n(t.changed_7d)} sub={`${n(t.changed_30d)} in 30 days`} onClick={() => set({ changed_days: "7" })} />
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              <div>
                <h3 className="mb-1 text-xs font-semibold uppercase text-gray-500">Stock at Rocky Mountain</h3>
                <table className="w-full text-left text-xs">
                  <thead className="text-gray-500">
                    <tr><th className="py-1 pr-3"></th><th className="py-1 pr-3 text-right">Parts</th><th className="py-1 text-right">Units</th></tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100">
                    <tr>
                      <td className="py-1 pr-3"><button type="button" className="underline" onClick={() => set({ stock: "in", status: "active" })}>In stock</button></td>
                      <td className="py-1 pr-3 text-right">{n(t.in_stock)}</td>
                      <td className="py-1 text-right">{n(t.units)}</td>
                    </tr>
                    {t.units_utah != null && (
                      <>
                        <tr>
                          <td className="py-1 pr-3"><button type="button" className="underline" onClick={() => set({ warehouse: "utah", status: "active" })}>Utah</button></td>
                          <td className="py-1 pr-3 text-right">{n(t.in_utah)}</td>
                          <td className="py-1 text-right">{n(t.units_utah)}</td>
                        </tr>
                        <tr>
                          <td className="py-1 pr-3"><button type="button" className="underline" onClick={() => set({ warehouse: "kentucky", status: "active" })}>Kentucky</button></td>
                          <td className="py-1 pr-3 text-right">{n(t.in_kentucky)}</td>
                          <td className="py-1 text-right">{n(t.units_kentucky)}</td>
                        </tr>
                      </>
                    )}
                  </tbody>
                </table>
                <p className="mt-1 text-xs text-gray-500">
                  All Rocky Mountain stock is worth {money0(t.stock_value_at_cost)} at your cost ({money0(t.stock_value_at_list)} at list). This is their shelf, not yours. Rocky Mountain shows no more than 10 of a part, so these totals are a floor.
                </p>
              </div>

              <div>
                <h3 className="mb-1 text-xs font-semibold uppercase text-gray-500">Parts by your cost</h3>
                <div className="space-y-1">
                  {(() => {
                    const max = Math.max(1, ...overview.price_bands.map((b) => b.parts));
                    return overview.price_bands.map((b) => (
                      <div key={b.band} className="flex items-center gap-2 text-xs">
                        <span className="w-24 shrink-0 text-gray-600">{b.band}</span>
                        <div className="h-3 flex-1 rounded bg-gray-100">
                          <div className="h-3 rounded bg-gray-700" style={{ width: `${(b.parts / max) * 100}%` }} />
                        </div>
                        <span className="w-16 text-right">{n(b.parts)}</span>
                      </div>
                    ));
                  })()}
                </div>
              </div>
            </div>

            <div>
              <h3 className="mb-1 text-xs font-semibold uppercase text-gray-500">Data gaps (active parts)</h3>
              <div className="flex flex-wrap gap-2 text-xs">
                {(
                  [
                    ["upc", t.no_barcode], ["mfr", t.no_mfr_number], ["name", t.no_name], ["photo", t.no_photo], ["fitment", t.no_fitment],
                    ["cost", t.no_cost], ["list", t.no_list_price], ["below_cost", t.list_below_cost],
                  ] as const
                ).map(([k, c]) => (
                  <button
                    key={k}
                    type="button"
                    disabled={!c}
                    onClick={() => set({ missing: k, status: "active" })}
                    className="rounded border border-gray-200 px-2 py-1 hover:bg-gray-50 disabled:opacity-50"
                  >
                    <b>{n(c)}</b> {MISSING_LABEL[k]}
                  </button>
                ))}
              </div>
            </div>

            <div>
              <h3 className="mb-1 text-xs font-semibold uppercase text-gray-500">Units of measure</h3>
              <div className="flex flex-wrap gap-2 text-xs">
                {overview.units_of_measure.map((u) => (
                  <button
                    key={u.uom}
                    type="button"
                    disabled={u.unset}
                    onClick={() => set({ uom: u.uom })}
                    className="rounded border border-gray-200 px-2 py-1 text-center hover:bg-gray-50 disabled:opacity-60"
                  >
                    {u.uom} <b>{n(u.parts)}</b>
                    {uomName(u.uom) && <div className="text-[11px] font-normal text-gray-500">({uomName(u.uom)})</div>}
                  </button>
                ))}
              </div>
            </div>

            <div className="border-t border-gray-100 pt-2">
              <button type="button" className={btn2Cls} onClick={() => setShowBrands((v) => !v)}>
                {showBrands ? "Hide" : "Show"} all {n(t.brands)} brands
              </button>
              {showBrands && (
                <div className="mt-2 space-y-2">
                  <input className={`${inputCls} max-w-xs`} placeholder="Find a brand" value={brandQ} onChange={(e) => setBrandQ(e.target.value)} />
                  <div className="max-h-96 overflow-auto rounded border border-gray-100">
                    <table className="w-full text-left text-xs">
                      <thead className="sticky top-0 bg-gray-50 uppercase text-gray-500">
                        <tr>
                          <th className="px-2 py-1">Brand</th>
                          <th className="px-2 py-1 text-right">Parts</th>
                          <th className="px-2 py-1 text-right">In stock</th>
                          <th className="px-2 py-1 text-right">Units</th>
                          <th className="px-2 py-1 text-right">Cost range</th>
                          <th className="px-2 py-1 text-right">Avg cost</th>
                          <th className="px-2 py-1 text-right">Stock at cost</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-gray-100">
                        {brandRows.map((b) => (
                          <tr key={b.brand}>
                            <td className="px-2 py-1">
                              {b.unbranded ? (
                                <span className="text-gray-500">{b.brand}</span>
                              ) : (
                                <button type="button" className="underline" onClick={() => set({ brand: b.brand, status: "active" })}>{b.brand}</button>
                              )}
                            </td>
                            <td className="px-2 py-1 text-right">{n(b.parts)}</td>
                            <td className="px-2 py-1 text-right">{n(b.in_stock)}</td>
                            <td className="px-2 py-1 text-right">{n(b.units)}</td>
                            <td className="px-2 py-1 text-right whitespace-nowrap">{money(b.min_cost)} – {money(b.max_cost)}</td>
                            <td className="px-2 py-1 text-right">{money(b.avg_cost)}</td>
                            <td className="px-2 py-1 text-right">{money0(b.stock_value_at_cost)}</td>
                          </tr>
                        ))}
                        {brandRows.length === 0 && (
                          <tr><td colSpan={7} className="px-2 py-3 text-gray-500">No brand matches that.</td></tr>
                        )}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </div>
          </>
        ))}
      </div>

      {/* ---------------------------------------------------------------- browser */}
      <div className="space-y-3 rounded border border-gray-200 bg-white p-4 text-sm">
        <h2 className="font-semibold">Browse and search the catalog</h2>

        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          <div className="sm:col-span-2">
            <Lbl text="Search (every word must match)">
              <input
                className={inputCls}
                placeholder="SKU, manufacturer number, name, brand or UPC — dashes and spaces don't matter"
                value={qInput}
                onChange={(e) => setQInput(e.target.value)}
              />
            </Lbl>
          </div>
          <Lbl text="Search in">
            <select className={inputCls} value={f.field} onChange={(e) => set({ field: e.target.value })}>
              <option value="all">Everything</option>
              <option value="sku">SKU</option>
              <option value="mfr">Manufacturer number</option>
              <option value="name">Name</option>
              <option value="brand">Brand</option>
              <option value="upc">UPC</option>
            </select>
          </Lbl>
          <Lbl text="Brand">
            <input
              className={inputCls}
              list="rmatv-brands"
              placeholder="Any brand"
              value={f.brand}
              onChange={(e) => set({ brand: e.target.value })}
            />
            <datalist id="rmatv-brands">
              {(overview?.brands ?? []).filter((b) => !b.unbranded).map((b) => (
                <option key={b.brand} value={b.brand} />
              ))}
            </datalist>
          </Lbl>
          <Lbl text="Stock">
            <select className={inputCls} value={f.stock} onChange={(e) => set({ stock: e.target.value })}>
              <option value="">Any</option>
              <option value="in">In stock at Rocky Mountain</option>
              <option value="out">Out of stock</option>
            </select>
          </Lbl>
          <Lbl text="Source">
            <select className={inputCls} value={f.source} onChange={(e) => set({ source: e.target.value })}>
              <option value="">Main + OEM</option>
              <option value="oem">OEM parts only</option>
              <option value="main">Main catalog only</option>
            </select>
          </Lbl>
          <Lbl text="Warehouse">
            <select className={inputCls} value={f.warehouse} onChange={(e) => set({ warehouse: e.target.value })}>
              <option value="">Either</option>
              <option value="utah">Has stock in Utah</option>
              <option value="kentucky">Has stock in Kentucky</option>
            </select>
          </Lbl>
          <Lbl text="Part status">
            <select className={inputCls} value={f.status} onChange={(e) => set({ status: e.target.value })}>
              <option value="active">Active (in the files)</option>
              <option value="inactive">Retired (left the files)</option>
              <option value="all">Both</option>
            </select>
          </Lbl>
          <Lbl text="Product of ours?">
            <select className={inputCls} value={f.ours} onChange={(e) => set({ ours: e.target.value })}>
              <option value="">Any</option>
              <option value="yes">Has its product</option>
              <option value="no">No product yet</option>
            </select>
          </Lbl>
        </div>

        <button type="button" className="text-xs underline" onClick={() => setAdvanced((v) => !v)}>
          {advanced ? "Hide" : "Show"} price, margin, stock-level and data-gap filters
        </button>
        {advanced && (
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            {(
              [
                ["min_cost", "Your cost from ($)"], ["max_cost", "Your cost to ($)"],
                ["min_list", "List price from ($)"], ["max_list", "List price to ($)"],
                ["min_margin_pct", "Margin from (%)"], ["max_margin_pct", "Margin to (%)"],
                ["min_qty", "Stock from"], ["max_qty", "Stock to"],
              ] as const
            ).map(([k, label]) => (
              <Lbl key={k} text={label}>
                <input className={inputCls} inputMode="decimal" value={f[k]} onChange={(e) => set({ [k]: e.target.value } as Partial<Filters>)} />
              </Lbl>
            ))}
            <Lbl text="Data gap">
              <select className={inputCls} value={f.missing} onChange={(e) => set({ missing: e.target.value })}>
                <option value="">None</option>
                {Object.entries(MISSING_LABEL).map(([k, v]) => (
                  <option key={k} value={k}>{v}</option>
                ))}
              </select>
            </Lbl>
            <Lbl text="Unit of measure">
              <select className={inputCls} value={f.uom} onChange={(e) => set({ uom: e.target.value })}>
                <option value="">Any</option>
                {(overview?.units_of_measure ?? []).filter((u) => !u.unset).map((u) => (
                  <option key={u.uom} value={u.uom}>{u.uom}</option>
                ))}
              </select>
            </Lbl>
            <Lbl text="Changed within (days)">
              <select className={inputCls} value={f.changed_days} onChange={(e) => set({ changed_days: e.target.value })}>
                <option value="">Any time</option>
                <option value="1">1 day</option>
                <option value="7">7 days</option>
                <option value="30">30 days</option>
                <option value="90">90 days</option>
              </select>
            </Lbl>
            <Lbl text="Last seen in the files within">
              <select className={inputCls} value={f.seen_days} onChange={(e) => set({ seen_days: e.target.value })}>
                <option value="">Any time</option>
                <option value="1">1 day</option>
                <option value="7">7 days</option>
                <option value="30">30 days</option>
              </select>
            </Lbl>
          </div>
        )}

        {activeChips.length > 0 && (
          <div className="flex flex-wrap items-center gap-2">
            {activeChips.map((c) => (
              <button key={c.label} type="button" onClick={c.clear} className="rounded-full border border-gray-300 bg-gray-50 px-2 py-0.5 text-xs hover:bg-gray-100" title="Remove this filter">
                {c.label} ×
              </button>
            ))}
          </div>
        )}
        <div className="flex flex-wrap items-center gap-3">
          <button type="button" className={btn2Cls} disabled={DEFAULTS_ONLY(f) && qInput === ""} onClick={reset}>
            Clear all filters
          </button>
          <label className="flex items-center gap-2 text-xs text-gray-600">
            Per page
            <select className="rounded border border-gray-300 px-2 py-1 text-sm" value={limit} onChange={(e) => { setLimit(Number(e.target.value)); setOffset(0); }}>
              {[25, 50, 100, 200].map((x) => (
                <option key={x} value={x}>{x}</option>
              ))}
            </select>
          </label>
          {loading && <span className="text-xs text-gray-500">Searching…</span>}
        </div>

        {data && (
          <div className="rounded bg-gray-50 px-3 py-2 text-xs text-gray-700">
            <b>{n(data.total)}</b> parts match · <b>{n(data.summary.in_stock)}</b> in stock · <b>{n(data.summary.brands)}</b> brands ·{" "}
            <b>{n(data.summary.units)}</b> units at Rocky Mountain worth <b>{money0(data.summary.stock_value_at_cost)}</b> at your cost · cost range{" "}
            {money(data.summary.min_cost)} – {money(data.summary.max_cost)}, average {money(data.summary.avg_cost)}
          </div>
        )}

        {data && (
          <>
            {data.total > limit && <Pager total={data.total} limit={limit} offset={offset} onChange={setOffset} />}
            <div className="overflow-x-auto">
              {/* fixed layout: every column keeps the same width on every page and filter; the name takes what is left */}
              <table className="w-full min-w-[88rem] table-fixed text-left text-xs">
                <colgroup>
                  {["2.75rem", "9rem", "8rem", "12rem", "", "5rem", "5rem", "5rem", "4rem", "7.5rem", "10.5rem", "8rem", "6rem"].map((w, i) => (
                    <col key={i} style={w ? { width: w } : undefined} />
                  ))}
                </colgroup>
                <thead className="text-gray-500">
                  <tr>
                    <th className="py-1 pr-2"></th>
                    <Th k="brand">Brand</Th>
                    <Th k="sku">SKU</Th>
                    <Th k="mfr">Mfr no.</Th>
                    <Th k="name">Name</Th>
                    <Th k="list" right>List</Th>
                    <Th k="cost" right>Cost</Th>
                    <Th k="margin_pct" right title="(list − cost) / list">Margin</Th>
                    <Th k="stock" right title="At Rocky Mountain: Utah + Kentucky total (UT / KY shown under it); 10+ counts as 10">Stock</Th>
                    <th className="whitespace-nowrap py-1 pr-3">UPC</th>
                    <th className="whitespace-nowrap py-1 pr-3">Ours</th>
                    <Th k="updated">Changed</Th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {data.rows.map((r) => (
                    <tr
                      key={r.id}
                      className={`cursor-pointer align-top hover:bg-gray-50 ${r.active ? "" : "text-gray-400"}`}
                      onClick={() => void openDetail(r.id)}
                    >
                      <td className="py-1 pr-2">
                        {r.images && r.images.length > 0 ? (
                          // eslint-disable-next-line @next/next/no-img-element
                          <img
                            src={r.images[0]}
                            alt=""
                            loading="lazy"
                            referrerPolicy="no-referrer"
                            className="h-9 w-9 rounded border border-gray-100 bg-white object-contain"
                            onError={(e) => {
                              (e.currentTarget as HTMLImageElement).style.visibility = "hidden";
                            }}
                          />
                        ) : (
                          <div className="h-9 w-9" />
                        )}
                      </td>
                      <td className="truncate py-1 pr-3" title={r.manufacturer ?? ""}>{r.manufacturer ?? "—"}</td>
                      <td className="py-1 pr-3 font-medium" title={r.sku}>
                        <div className="break-all">{r.sku}</div>
                        {(r.is_oem || !r.active) && (
                          <div className="mt-0.5 flex flex-wrap items-center gap-1">
                            {r.is_oem && <span className="whitespace-nowrap rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold leading-none text-amber-800">OEM</span>}
                            {!r.active && <Badge tone="gray">retired</Badge>}
                          </div>
                        )}
                      </td>
                      <td className="break-all py-1 pr-3" title={r.mfr_number ?? ""}>{r.mfr_number ?? "—"}</td>
                      <td className="break-words py-1 pr-3">{r.name ?? "—"}</td>
                      <td className="whitespace-nowrap py-1 pr-3 text-right" title={r.map_price != null ? `MAP price (MSRP ${r.msrp != null ? money(r.msrp) : "none"})` : ""}>{money(r.list_price)}{r.map_price != null && <span className="text-gray-400">*</span>}</td>
                      <td className="whitespace-nowrap py-1 pr-3 text-right" title={r.my_cost != null && r.standard_cost != null ? `Catalog price ${money(r.standard_cost)}` : ""}>{money(r.dealer_cost)}</td>
                      <td className="py-1 pr-3 text-right whitespace-nowrap" title={r.margin == null ? "" : `${money(r.margin)} per unit`}>
                        {r.margin == null ? "—" : <span className={Number(r.margin) < 0 ? "text-red-700" : ""}>{pct(r.margin_pct)}</span>}
                      </td>
                      <td className="whitespace-nowrap py-1 pr-3 text-right font-medium" title={r.qty_utah != null || r.qty_kentucky != null ? `Utah ${n(r.qty_utah ?? 0)} · Kentucky ${n(r.qty_kentucky ?? 0)}` : ""}>
                        {n(r.qty_other)}
                        {(r.qty_utah != null || r.qty_kentucky != null) && <div className="text-[10px] font-normal text-gray-500">UT {n(r.qty_utah ?? 0)} · KY {n(r.qty_kentucky ?? 0)}</div>}
                      </td>
                      <td className="whitespace-nowrap py-1 pr-3 font-mono" title={r.upc ?? ""}>{r.upc ?? "—"}</td>
                      <td className="truncate py-1 pr-3" onClick={(e) => e.stopPropagation()}>
                        {r.product_id ? (
                          <Link href={`/admin/products/${r.product_id}`} title={r.product_title ?? ""}>
                            <Badge tone="green">{r.product_sku ?? `#${r.product_id}`}</Badge>
                            {r.products > 1 && <span className="ml-1 text-gray-500">+{r.products - 1}</span>}
                          </Link>
                        ) : (
                          <button
                            type="button"
                            className="rounded border border-gray-300 px-1.5 py-0 text-xs hover:bg-gray-50 disabled:opacity-50"
                            disabled={busyId === r.id || !r.active}
                            title={r.images && r.images.length ? `Makes the product and saves its ${r.images.length} photo${r.images.length === 1 ? "" : "s"}` : "Makes the product (this part has no photo link)"}
                            onClick={() => void makeProduct(r)}
                          >
                            Make product
                          </button>
                        )}
                      </td>
                      <td className="py-1 pr-3 whitespace-nowrap text-gray-500">{r.updated_at ? new Date(r.updated_at).toLocaleDateString() : "—"}</td>
                    </tr>
                  ))}
                  {data.rows.length === 0 && (
                    <tr><td colSpan={13} className="py-3 text-gray-500">Nothing matches those filters.</td></tr>
                  )}
                </tbody>
              </table>
            </div>
            <Pager total={data.total} limit={limit} offset={offset} onChange={setOffset} />
            <p className="text-[11px] text-gray-400">
              Click a part to see everything stored about it. “Changed” is the last time its price, stock or details differed from the previous files. The photo is Rocky Mountain’s link; it is saved into our storage when a product is made from the part.
            </p>
          </>
        )}
      </div>

      {detail && <DetailModal d={detail} onClose={() => setDetail(null)} onBrand={(b) => { setDetail(null); set({ brand: b, status: "active" }); }} onOpen={(id) => void openDetail(id)} />}
    </div>
  );
}

function DetailModal({ d, onClose, onBrand, onOpen }: { d: Detail; onClose: () => void; onBrand: (b: string) => void; onOpen: (id: number) => void }) {
  const it = d.item as Record<string, string | number | boolean | null>;
  const cost = it.dealer_cost == null ? null : Number(it.dealer_cost);
  const list = it.list_price == null ? null : Number(it.list_price);
  const total = Number(it.qty_other ?? 0);
  const rows: [string, ReactNode][] = [
    ["Rocky Mountain SKU", it.sku as string],
    ["Manufacturer number", (it.mfr_number as string) ?? "—"],
    ["Brand", it.manufacturer ? <button type="button" className="underline" onClick={() => onBrand(String(it.manufacturer))}>{String(it.manufacturer)} ({n(d.brand_part_count)} parts)</button> : "—"],
    ["Unit of measure", (it.unit_of_measure as string) ?? "—"],
    ["UPC", (it.upc as string) ?? "—"],
    ["List price", it.map_price != null ? `${money(it.list_price as string | null)} (MAP price)` : money(it.list_price as string | null)],
    ["Your cost", money(it.dealer_cost as string | null)],
    ...(it.my_cost != null && it.standard_cost != null ? ([["Catalog (dealer) price", money(it.standard_cost as number)]] as [string, ReactNode][]) : []),
    ...(it.map_price != null && it.msrp != null ? ([["MSRP (not used: MAP is the list price)", money(it.msrp as number)]] as [string, ReactNode][]) : []),
    [
      "Margin",
      cost != null && list != null && list > 0 ? `${money(list - cost)} per unit (${(((list - cost) / list) * 100).toFixed(1)}% of list)` : "—",
    ],
    ...(it.qty_utah != null || it.qty_kentucky != null ? ([["Stock in Utah", n(Number(it.qty_utah ?? 0))], ["Stock in Kentucky", n(Number(it.qty_kentucky ?? 0))]] as [string, ReactNode][]) : []),
    ["Stock at Rocky Mountain", `${n(total)}${cost != null ? ` (${money0(cost * total)} at cost)` : ""} — a part shown as 10+ counts as 10`],
    ["Status", it.active ? <Badge tone="green">active, in the files</Badge> : <Badge tone="gray">retired, left the files</Badge>],
    ["Last changed", when(it.updated_at)],
    ["Last seen in the files", when(it.last_seen_at)],
    ["First stored", when(it.created_at)],
  ];
  const known = new Set(["id", "supplier", "sku", "mfr_number", "mfr_number_norm", "manufacturer", "name", "unit_of_measure", "list_price", "dealer_cost", "qty_maine", "qty_indiana", "qty_nevada", "qty_other", "qty_utah", "qty_kentucky", "is_oem", "msrp", "my_cost", "map_price", "standard_cost", "upc", "upc_norm", "images", "images_synced_at", "active", "updated_at", "last_seen_at", "created_at", "last_run_id"]);
  const extra = Object.entries(it).filter(([k, v]) => !known.has(k) && v != null && v !== "");

  const mini = (title: string, list: Mini[]) =>
    list.length > 0 && (
      <div>
        <h3 className="mb-1 text-xs font-semibold uppercase text-gray-500">{title}</h3>
        <table className="w-full text-left text-xs">
          <tbody className="divide-y divide-gray-100">
            {list.map((m) => (
              <tr key={m.id} className="cursor-pointer hover:bg-gray-50" onClick={() => onOpen(m.id)}>
                <td className="py-1 pr-2">{m.manufacturer ?? "—"}</td>
                <td className="py-1 pr-2 font-medium">{m.sku}</td>
                <td className="py-1 pr-2">{m.name}</td>
                <td className="py-1 pr-2 text-right">{money(m.dealer_cost)}</td>
                <td className="py-1 text-right whitespace-nowrap">{n(m.qty_other)} in stock</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );

  return (
    <Modal title={`${it.manufacturer ? `${it.manufacturer} ` : ""}${it.sku}`} onClose={onClose}>
      <div className="space-y-4 text-sm">
        <p className="font-medium">{(it.name as string) ?? "(no name)"}</p>
        {Array.isArray(it.images) && it.images.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {(it.images as string[]).map((u) => (
              <a key={u} href={u} target="_blank" rel="noreferrer" title="Open the full-size photo">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={u} alt="" loading="lazy" referrerPolicy="no-referrer" className="h-28 w-28 rounded border border-gray-200 bg-white object-contain" />
              </a>
            ))}
          </div>
        )}
        <dl className="grid grid-cols-[10rem_1fr] gap-x-3 gap-y-1 text-xs">
          {rows.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-gray-500">{k}</dt>
              <dd>{v}</dd>
            </div>
          ))}
          {extra.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-gray-500">{k.replace(/_/g, " ")}</dt>
              <dd className="break-words">{String(v)}</dd>
            </div>
          ))}
          {d.last_run && (
            <div className="contents">
              <dt className="text-gray-500">Last import</dt>
              <dd>#{d.last_run.id} · {d.last_run.source} · {when(d.last_run.started_at)} · {n(d.last_run.rows_seen)} parts</dd>
            </div>
          )}
        </dl>

        <div>
          <h3 className="mb-1 text-xs font-semibold uppercase text-gray-500">Our products for this part</h3>
          {d.ours.length === 0 ? (
            <p className="text-xs text-gray-500">None yet. No product of ours follows this part or has its barcode or manufacturer number.</p>
          ) : (
            <table className="w-full text-left text-xs">
              <thead className="text-gray-500">
                <tr><th className="py-1 pr-2">Our SKU</th><th className="py-1 pr-2">Title</th><th className="py-1 pr-2">Matched by</th><th className="py-1 pr-2 text-right">On our shelf</th><th className="py-1 pr-2 text-right">Our cost</th><th className="py-1 text-right">Our price</th></tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {d.ours.map((p) => (
                  <tr key={p.id} className={p.is_active ? "" : "text-gray-400"}>
                    <td className="py-1 pr-2 font-medium"><Link href={`/admin/products/${p.id}`} className="underline">{p.sku}</Link></td>
                    <td className="py-1 pr-2">{p.title}</td>
                    <td className="py-1 pr-2">{p.by === "linked part" ? <Badge tone="green">linked part</Badge> : p.by === "barcode" ? <Badge tone="green">barcode</Badge> : <Badge tone="amber">part number — check the brand</Badge>}</td>
                    <td className="py-1 pr-2 text-right">{n(p.on_hand)}</td>
                    <td className="py-1 pr-2 text-right">{money(p.cost)}</td>
                    <td className="py-1 text-right">{money(p.list_price)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {mini("Other Rocky Mountain parts with the same barcode", d.same_barcode)}
        {mini("Other Rocky Mountain parts with the same manufacturer number", d.same_part_number)}
        <p className="text-[11px] text-gray-400">Licensed data: dealer cost and stock are for this screen only.</p>
        <div className="flex justify-end"><button type="button" className={btn2Cls} onClick={onClose}>Close</button></div>
      </div>
    </Modal>
  );
}
