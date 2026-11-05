"use client";

import { useEffect, useMemo, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Notice, btnCls, inputCls } from "./ui";

export type KitPart = {
  key: string;
  distributor_id: number;
  vendor?: string;
  supplier_item_id: number | null;
  part_number: string;
  // what goes in the custom label: the manufacturer number for Autodist parts (part_number stays the vendor SKU, which the PO needs)
  label_part?: string | null;
  description: string;
  unit_cost: number | null;
  quantity: number;
};

type Vendor = { id: number; name: string; has_catalog: boolean };
type CatItem = {
  id: number; sku: string; mfr_number: string | null; manufacturer: string | null; name: string | null;
  list_price: number | null; dealer_cost?: number | null; upc: string | null; in_stock_total: number;
  qty_maine: number; qty_indiana: number; qty_nevada: number;
};
type Brand = { name: string; count: number };
type Listing = { id: number; store: string; item_id: string; status: string | null; in_sync: boolean };
type SyncResult = { store: string; item_id: string; status: string; detail: string };

const JOIN = "xx";
// Same rule as the server (app/kits.py build_cls); the server is the one that decides.
// Each part once, in the order added, however many are in the kit.
export function previewCls(parts: KitPart[]): string {
  const seen = new Set<string>();
  const tokens: string[] = [];
  parts.forEach((p) => {
    const t = (p.label_part || p.part_number).trim().replace(/[^A-Za-z0-9\-_.]/g, "");
    if (t && !seen.has(t.toLowerCase())) {
      seen.add(t.toLowerCase());
      tokens.push(t);
    }
  });
  return tokens.join(JOIN);
}

let keySeq = 0;
export const newKey = () => `k${Date.now()}_${keySeq++}`;

const STATUS_LABEL: Record<string, string> = {
  updated: "Updated on eBay",
  already_current: "Already correct",
  not_sent: "Not sent",
  skipped: "Skipped",
  error: "Failed",
};

// Marks where the typed words appear in a result, so it is clear WHY a part was found (it may be in the barcode, which is
// not visible otherwise). Words match ignoring case; dashes and spaces in part numbers are ignored by the server too.
function highlight(value: string | null | undefined, query: string) {
  const v = value ?? "";
  const words = query.trim().split(/\s+/).filter(Boolean).map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!v || words.length === 0) return v;
  const parts = v.split(new RegExp(`(${words.join("|")})`, "ig"));
  return parts.map((t, i) => (i % 2 === 1 ? <mark key={i} className="bg-yellow-200">{t}</mark> : t));
}

export default function KitPartsPanel({
  productId,
  parts,
  onChange,
  disabled,
  cardCls,
  onSaved,
  currentCls,
}: {
  productId?: number;
  parts: KitPart[];
  onChange: (p: KitPart[]) => void;
  disabled?: boolean;
  cardCls: string;
  onSaved?: (cls: string | null) => void;
  // the product's label as it is now (typed or saved). While there is one, adding parts never changes it.
  currentCls?: string | null;
}) {
  const [vendors, setVendors] = useState<Vendor[]>([]);
  const [vendorId, setVendorId] = useState<number | "">("");
  const [brands, setBrands] = useState<Brand[]>([]);
  // search controls
  const [q, setQ] = useState("");
  const [field, setField] = useState("all");
  const [match, setMatch] = useState("contains");
  const [brand, setBrand] = useState("");
  const [inStock, setInStock] = useState(false);
  const [minCost, setMinCost] = useState("");
  const [maxCost, setMaxCost] = useState("");
  const [codes, setCodes] = useState("");
  const [fromProduct, setFromProduct] = useState(false);
  const [sort, setSort] = useState("best");
  const [showMore, setShowMore] = useState(false);
  const [results, setResults] = useState<CatItem[] | null>(null);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [searching, setSearching] = useState(false);
  const [addQty, setAddQty] = useState<Record<number, string>>({});
  // typed-in piece (vendors without a catalog)
  const [man, setMan] = useState({ part_number: "", description: "", unit_cost: "", quantity: "1" });
  // saving
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const [listings, setListings] = useState<Listing[]>([]);
  const [canSync, setCanSync] = useState(false);
  const [results2, setResults2] = useState<SyncResult[] | null>(null);
  const [dirty, setDirty] = useState(false);
  const [savedCls, setSavedCls] = useState<string | null>(null);

  const vendor = vendors.find((v) => v.id === vendorId);
  const PAGE = 30;

  useEffect(() => {
    api<{ items: Vendor[] }>("/kits/vendors")
      .then((r) => {
        setVendors(r.items);
        const auto = r.items.find((v) => v.has_catalog);
        if (auto) setVendorId(auto.id);
      })
      .catch((e) => setMsg({ kind: "error", text: errMsg(e) }));
    api<{ items: Brand[] }>("/kits/catalog/brands").then((r) => setBrands(r.items)).catch(() => {});
  }, []);

  // existing product: load what is saved
  useEffect(() => {
    if (!productId) return;
    api<{ cls: string | null; parts: (Omit<KitPart, "key"> & { id: number })[]; listings: Listing[]; can_sync: boolean }>(`/kits/${productId}`)
      .then((r) => {
        onChange(r.parts.map((p) => ({ ...p, key: `p${p.id}`, description: p.description ?? "" })));
        setListings(r.listings);
        setCanSync(r.can_sync);
        setSavedCls(r.cls);
        setDirty(false);
      })
      .catch((e) => setMsg({ kind: "error", text: errMsg(e) }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [productId]);

  const built = useMemo(() => previewCls(parts), [parts]);
  const keptCls = (currentCls ?? savedCls ?? "").trim();
  const cls = keptCls || built; // an existing label is never replaced by the parts
  const edit = (next: KitPart[]) => {
    setDirty(true);
    onChange(next);
  };

  async function search(off = 0) {
    setSearching(true);
    setMsg(null);
    try {
      const p = new URLSearchParams();
      if (q.trim()) p.set("q", q.trim());
      if (field !== "all") p.set("field", field);
      if (match !== "contains") p.set("match", match);
      if (brand) p.set("brand", brand);
      if (inStock) p.set("in_stock", "true");
      if (minCost) p.set("min_cost", minCost);
      if (maxCost) p.set("max_cost", maxCost);
      if (codes.trim()) p.set("codes", codes);
      if (fromProduct && productId) p.set("from_product", String(productId));
      if (sort !== "best") p.set("sort", sort);
      p.set("limit", String(PAGE));
      p.set("offset", String(off));
      const r = await api<{ total: number; items: CatItem[] }>(`/kits/catalog?${p}`);
      setResults(r.items);
      setTotal(r.total);
      setOffset(off);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setSearching(false);
    }
  }

  function addFromCatalog(it: CatItem) {
    if (!vendorId) return;
    const qty = Math.max(1, Math.min(1000, Number(addQty[it.id] || 1) || 1));
    const existing = parts.find((p) => p.supplier_item_id === it.id);
    if (existing) {
      edit(parts.map((p) => (p === existing ? { ...p, quantity: Math.min(1000, p.quantity + qty) } : p)));
      return;
    }
    edit([
      ...parts,
      {
        key: newKey(),
        distributor_id: Number(vendorId),
        vendor: vendor?.name,
        supplier_item_id: it.id,
        part_number: it.sku,
        label_part: it.mfr_number || it.sku,
        description: [it.manufacturer, it.name].filter(Boolean).join(" ").slice(0, 250),
        unit_cost: it.dealer_cost ?? null,
        quantity: qty,
      },
    ]);
  }

  function addManual() {
    if (!vendorId) return setMsg({ kind: "error", text: "Pick the vendor first." });
    if (!man.part_number.trim()) return setMsg({ kind: "error", text: "Type the vendor's part number." });
    const cost = man.unit_cost.trim() === "" ? null : Number(man.unit_cost);
    if (cost !== null && Number.isNaN(cost)) return setMsg({ kind: "error", text: "Cost must be a number." });
    edit([
      ...parts,
      {
        key: newKey(),
        distributor_id: Number(vendorId),
        vendor: vendor?.name,
        supplier_item_id: null,
        part_number: man.part_number.trim(),
        description: man.description.trim(),
        unit_cost: cost,
        quantity: Math.max(1, Math.min(1000, Number(man.quantity) || 1)),
      },
    ]);
    setMan({ part_number: "", description: "", unit_cost: "", quantity: "1" });
    setMsg(null);
  }

  const move = (i: number, d: -1 | 1) => {
    const j = i + d;
    if (j < 0 || j >= parts.length) return;
    const next = [...parts];
    [next[i], next[j]] = [next[j], next[i]];
    edit(next);
  };

  const payloadParts = () =>
    parts.map((p) => ({
      distributor_id: p.distributor_id,
      supplier_item_id: p.supplier_item_id,
      part_number: p.part_number,
      description: p.description || null,
      unit_cost: p.unit_cost,
      quantity: p.quantity,
    }));

  async function sendToEbay() {
    if (!productId) return;
    setBusy(true);
    setResults2(null);
    try {
      const r = await api<{ cls: string; results: SyncResult[] }>(`/kits/${productId}/sync`, { method: "POST" });
      setResults2(r.results);
      const fresh = await api<{ listings: Listing[] }>(`/kits/${productId}`);
      setListings(fresh.listings);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    if (!productId) return;
    setBusy(true);
    setMsg(null);
    setResults2(null);
    try {
      const r = await api<{ cls: string | null; cls_changed: boolean; duplicates: { sku: string; title: string }[]; listings_to_update: number }>(
        `/kits/${productId}/parts`,
        { method: "PUT", body: { parts: payloadParts() } }
      );
      setDirty(false);
      setSavedCls(r.cls);
      onSaved?.(r.cls);
      let text = !r.cls
        ? "Saved (no custom label yet)."
        : r.cls_changed
          ? `Saved. Custom label created: ${r.cls}.`
          : `Saved. The custom label stays ${r.cls} (it was not changed; edit the label box above to change it).`;
      if (r.duplicates.length) text += ` Warning: ${r.duplicates.map((d) => d.sku).join(", ")} already use the same label.`;
      setMsg({ kind: "success", text });
      if (r.cls && r.listings_to_update > 0) {
        if (canSync) await sendToEbay();
        else setMsg({ kind: "success", text: text + ` ${r.listings_to_update} eBay listing(s) still need the new label; an admin must send it.` });
      }
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy(false);
    }
  }

  const pending = listings.filter((l) => !l.in_sync).length;
  const small = "rounded border border-gray-300 px-2 py-1 text-xs";

  return (
    <section className={cardCls}>
      <h2 className="text-lg font-semibold">Kit parts &amp; custom label (CLS)</h2>
      <p className="text-xs text-gray-500">
        If this product is made of parts bought from a vendor, add them here. The custom label is built from the part
        numbers, is the same on every eBay listing of this product, and the parts are what goes on the purchase order when this
        product is ordered. Autodist parts use the manufacturer number in the label, each part once, in the order added. A label
        that already exists is never changed by adding parts; edit it in the label box above. Leave empty if it is a single part.
      </p>

      {/* the pieces */}
      {parts.length > 0 && (
        <ul className="space-y-2">
          {parts.map((p, i) => {
            const vendorName = (p.vendor ?? vendors.find((v) => v.id === p.distributor_id)?.name ?? "").replace(/\s*[-–]\s*catalog\s*$/i, "");
            return (
              <li key={p.key} className="rounded border border-gray-200 bg-white p-3">
                <div className="grid items-center gap-x-5 gap-y-2 sm:grid-cols-[minmax(0,1fr)_auto_auto]">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-baseline gap-x-2">
                      <span className="break-all font-mono text-sm font-semibold">{p.part_number}</span>
                      {vendorName && <span className="text-xs text-gray-500">{vendorName}</span>}
                    </div>
                    {p.description && <div className="break-words text-xs text-gray-700">{p.description}</div>}
                  </div>
                  <div className="flex items-center gap-5 text-sm">
                    <div>
                      <div className="text-[11px] uppercase text-gray-500">Cost</div>
                      <div>{p.unit_cost != null ? `$${Number(p.unit_cost).toFixed(2)}` : "—"}</div>
                    </div>
                    <label>
                      <span className="block text-[11px] uppercase text-gray-500">Qty per kit</span>
                      <input
                        type="number" min={1} max={1000} disabled={disabled} className={inputCls} style={{ width: 84 }} value={p.quantity}
                        onChange={(e) => edit(parts.map((x) => (x === p ? { ...x, quantity: Math.max(1, Math.min(1000, Number(e.target.value) || 1)) } : x)))}
                      />
                    </label>
                  </div>
                  <div className="flex items-center gap-1 text-xs">
                    <button type="button" className="px-1" disabled={disabled || i === 0} onClick={() => move(i, -1)} aria-label="Move up">↑</button>
                    <button type="button" className="px-1" disabled={disabled || i === parts.length - 1} onClick={() => move(i, 1)} aria-label="Move down">↓</button>
                    <button type="button" className="px-1 text-red-700 underline" disabled={disabled} onClick={() => edit(parts.filter((x) => x !== p))}>Remove</button>
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      )}
      <div className="rounded bg-gray-50 p-2 text-sm">
        <span className="text-xs text-gray-500">{keptCls ? "Custom label (kept as is): " : `Custom label ${dirty ? "after saving" : "now"}: `}</span>
        <span className="font-mono">{cls || "(none yet)"}</span>
        {cls.length > 50 && <span className="ml-2 text-xs text-red-700">{cls.length} characters: eBay allows 50</span>}
      </div>

      {/* adding pieces */}
      {!disabled && (
        <div className="space-y-3 rounded border border-gray-200 p-3">
          <div className="flex flex-wrap items-end gap-3">
            <label className="text-xs">
              Vendor
              <select className={`${inputCls} block`} value={vendorId} onChange={(e) => { setVendorId(e.target.value ? Number(e.target.value) : ""); setResults(null); }}>
                <option value="">(choose)</option>
                {vendors.map((v) => <option key={v.id} value={v.id}>{v.name}{v.has_catalog ? "" : " (no catalog)"}</option>)}
              </select>
            </label>
          </div>

          {vendor && vendor.has_catalog && (
            <div className="space-y-2">
              <div className="flex flex-wrap items-end gap-2">
                <label className="min-w-[14rem] flex-1 text-xs">
                  Search the catalog
                  <input className={inputCls} value={q} placeholder="part number, SKU, UPC, name or brand" onChange={(e) => setQ(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void search(0); } }} />
                </label>
                <label className="text-xs">In
                  <select className={`${inputCls} block`} value={field} onChange={(e) => setField(e.target.value)}>
                    <option value="all">Everything</option><option value="sku">Vendor SKU</option><option value="mfr">Manufacturer #</option>
                    <option value="name">Name</option><option value="brand">Brand</option><option value="upc">UPC</option>
                  </select>
                </label>
                <label className="text-xs">Match
                  <select className={`${inputCls} block`} value={match} onChange={(e) => setMatch(e.target.value)}>
                    <option value="contains">Contains</option><option value="starts">Starts with</option><option value="exact">Exactly</option>
                  </select>
                </label>
                <button type="button" className={btnCls} disabled={searching} onClick={() => void search(0)}>{searching ? "Searching…" : "Search"}</button>
                <button type="button" className="text-xs underline" onClick={() => setShowMore((s) => !s)}>{showMore ? "Fewer options" : "More ways to search"}</button>
              </div>
              {showMore && (
                <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
                  <label className="text-xs">Brand
                    <select className={`${inputCls} block`} value={brand} onChange={(e) => setBrand(e.target.value)}>
                      <option value="">(any)</option>
                      {brands.map((b) => <option key={b.name} value={b.name}>{b.name} ({b.count})</option>)}
                    </select>
                  </label>
                  <label className="text-xs">Cost from
                    <input type="number" step="0.01" className={inputCls} value={minCost} onChange={(e) => setMinCost(e.target.value)} />
                  </label>
                  <label className="text-xs">Cost to
                    <input type="number" step="0.01" className={inputCls} value={maxCost} onChange={(e) => setMaxCost(e.target.value)} />
                  </label>
                  <label className="text-xs">Sort
                    <select className={`${inputCls} block`} value={sort} onChange={(e) => setSort(e.target.value)}>
                      <option value="best">Best match</option><option value="sku">SKU</option><option value="name">Name</option>
                      <option value="brand">Brand</option><option value="cost_asc">Cost, low first</option><option value="cost_desc">Cost, high first</option>
                      <option value="stock">Most in stock</option>
                    </select>
                  </label>
                  <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={inStock} onChange={(e) => setInStock(e.target.checked)} />In stock at the vendor only</label>
                  {productId && (
                    <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={fromProduct} onChange={(e) => setFromProduct(e.target.checked)} />Only parts matching this product&apos;s barcode / part #</label>
                  )}
                  <label className="text-xs sm:col-span-2 lg:col-span-4">Paste a list of part numbers, SKUs or UPCs (one per line)
                    <textarea className={inputCls} rows={2} value={codes} onChange={(e) => setCodes(e.target.value)} />
                  </label>
                </div>
              )}
              {results && (
                <div className="space-y-1">
                  <p className="text-xs text-gray-500">{total.toLocaleString()} found{total > PAGE ? `, showing ${offset + 1}–${Math.min(offset + PAGE, total)}` : ""}</p>
                  <div className="max-h-96 overflow-auto rounded border">
                    <table className="w-full text-xs">
                      <thead className="sticky top-0 bg-gray-50 text-left text-gray-500">
                        <tr><th className="p-1">SKU</th><th className="p-1">Mfr #</th><th className="p-1">Brand</th><th className="p-1">Name</th><th className="p-1">UPC</th><th className="p-1">Cost</th><th className="p-1">Stock</th><th className="p-1">Qty</th><th className="p-1" /></tr>
                      </thead>
                      <tbody>
                        {results.map((it) => (
                          <tr key={it.id} className="border-t align-middle">
                            <td className="p-1 font-mono">{highlight(it.sku, q)}</td>
                            <td className="p-1">{highlight(it.mfr_number, q)}</td>
                            <td className="p-1">{highlight(it.manufacturer, q)}</td>
                            <td className="p-1">{highlight(it.name, q)}</td>
                            <td className="p-1 font-mono">{highlight(it.upc, q)}</td>
                            <td className="p-1">{it.dealer_cost != null ? `$${it.dealer_cost.toFixed(2)}` : ""}</td>
                            <td className="p-1" title={`ME ${it.qty_maine} · IN ${it.qty_indiana} · NV ${it.qty_nevada}`}>{it.in_stock_total}</td>
                            <td className="p-1"><input type="number" min={1} className="w-14 rounded border px-1 py-1" value={addQty[it.id] ?? "1"} onChange={(e) => setAddQty((a) => ({ ...a, [it.id]: e.target.value }))} /></td>
                            <td className="p-1"><button type="button" className="rounded bg-gray-900 px-2 py-1 text-white" onClick={() => addFromCatalog(it)}>Add</button></td>
                          </tr>
                        ))}
                        {results.length === 0 && <tr><td colSpan={9} className="p-2 text-gray-500">Nothing found. Try fewer words, “Contains”, or a different field.</td></tr>}
                      </tbody>
                    </table>
                  </div>
                  {total > PAGE && (
                    <div className="flex gap-2">
                      <button type="button" className={small} disabled={offset === 0 || searching} onClick={() => void search(Math.max(0, offset - PAGE))}>Previous</button>
                      <button type="button" className={small} disabled={offset + PAGE >= total || searching} onClick={() => void search(offset + PAGE)}>Next</button>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          {vendor && !vendor.has_catalog && (
            <div className="space-y-2">
              <p className="text-xs text-gray-500">{vendor.name} has no catalog loaded here, so type the piece in.</p>
              <div className="flex flex-wrap items-end gap-2">
                <label className="text-xs">Vendor part #<input className={inputCls} value={man.part_number} onChange={(e) => setMan({ ...man, part_number: e.target.value })} /></label>
                <label className="min-w-[12rem] flex-1 text-xs">Description<input className={inputCls} value={man.description} onChange={(e) => setMan({ ...man, description: e.target.value })} /></label>
                <label className="text-xs">Cost<input type="number" step="0.01" className={`${inputCls} w-24`} value={man.unit_cost} onChange={(e) => setMan({ ...man, unit_cost: e.target.value })} /></label>
                <label className="text-xs">Qty<input type="number" min={1} className={`${inputCls} w-20`} value={man.quantity} onChange={(e) => setMan({ ...man, quantity: e.target.value })} /></label>
                <button type="button" className={btnCls} onClick={addManual}>Add piece</button>
              </div>
            </div>
          )}
        </div>
      )}

      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}

      {productId ? (
        <div className="space-y-2">
          {!disabled && (
            <div className="flex flex-wrap items-center gap-3">
              <button type="button" className={btnCls} disabled={busy || !dirty} onClick={() => void save()}>
                {busy ? "Working…" : "Save kit parts & update eBay"}
              </button>
              {!dirty && listings.length > 0 && pending > 0 && canSync && (
                <button type="button" className="text-sm underline" disabled={busy} onClick={() => void sendToEbay()}>
                  Send the label to {pending} eBay listing{pending === 1 ? "" : "s"} now
                </button>
              )}
            </div>
          )}
          {listings.length > 0 && (
            <p className="text-xs text-gray-600">
              {listings.length} eBay listing{listings.length === 1 ? "" : "s"} linked; {listings.length - pending} show this label{pending ? `, ${pending} not confirmed yet` : ""}.
            </p>
          )}
          {results2 && (
            <ul className="space-y-1 text-xs">
              {results2.map((r) => (
                <li key={`${r.store}-${r.item_id}`} className={r.status === "error" || r.status === "not_sent" ? "text-red-700" : "text-gray-700"}>
                  <span className="font-mono">{r.item_id}</span> ({r.store}) – <b>{STATUS_LABEL[r.status] ?? r.status}</b>: {r.detail}
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : (
        <p className="text-xs text-gray-500">The custom label is created when you create the product.</p>
      )}
    </section>
  );
}
