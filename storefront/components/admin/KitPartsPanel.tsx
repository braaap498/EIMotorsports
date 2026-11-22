"use client";

import { useEffect, useMemo, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Notice, btnCls, btn2Cls, inputCls } from "./ui";

export type KitPart = {
  key: string;
  distributor_id: number;
  vendor?: string;
  supplier_item_id: number | null;
  // set when the piece follows a part saved in the vendor's saved-parts list: its cost, description and manufacturer # come from there
  manual_item_id?: number | null;
  part_number: string;
  // what goes in the custom label: the manufacturer number for Autodist parts (part_number stays the vendor SKU, which the PO needs)
  label_part?: string | null;
  description: string;
  unit_cost: number | null;
  quantity: number;
  // the manufacturer's own part number for this piece (a catalog piece shows the catalog's number until changed)
  mfr_number?: string | null;
};

// What the distributor's stock can make of this product (the scarcest piece decides); read-only, worked out by the server.
export type Availability = {
  available: number;
  is_kit: boolean;
  // kits only: the kits OUR OWN single-part products can make (the E&I warehouse quantity of the kit); null for a single part
  ours_available?: number | null;
  parts: {
    id: number; part_number: string; need: number; matched: boolean; stock: number; can_make: number; limiting: boolean;
    on_our_shelf?: number; ours_can_make?: number; ours_limiting?: boolean;
  }[];
};

type Vendor = { id: number; name: string; has_catalog: boolean; supplier?: string | null };
type CatItem = {
  id: number; sku: string; mfr_number: string | null; manufacturer: string | null; name: string | null;
  list_price: number | null; dealer_cost?: number | null; upc: string | null; in_stock_total: number;
  qty_maine: number; qty_indiana: number; qty_nevada: number; qty_other?: number;
};
type Brand = { name: string; count: number };
type Listing = { id: number; store: string; item_id: string; status: string | null; in_sync: boolean };
type SyncResult = { store: string; item_id: string; status: string; detail: string };
type SmResult = { store: string; sm_product_id: string; name: string; before: string; after: string; status: string; detail: string };

const JOIN = "xx";
// Same rule as the server (app/kits.py build_cls); the server is the one that decides.
// Each part once, in the order added, however many are in the kit.
export type LabelBy = "mfr" | "sku";
export function previewCls(parts: KitPart[], labelBy: LabelBy = "mfr"): string {
  const seen = new Set<string>();
  const tokens: string[] = [];
  parts.forEach((p) => {
    const t = (labelBy === "sku" ? p.part_number : p.label_part || p.part_number).trim().replace(/[^A-Za-z0-9\-_.]/g, "");
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
  labelBy,
  onLabelByChange,
  onMpn,
  onAvail,
}: {
  productId?: number;
  parts: KitPart[];
  onChange: (p: KitPart[]) => void;
  disabled?: boolean;
  cardCls: string;
  onSaved?: (cls: string | null) => void;
  // the product's label as it is now (typed or saved). While there is one, adding parts never changes it.
  currentCls?: string | null;
  // how Automatic Distributors parts are written in a NEW label (it is only built when there is none yet)
  labelBy: LabelBy;
  onLabelByChange: (v: LabelBy) => void;
  // the product's Manufacturer part # as the server has it (the pieces' numbers, joined), and whether it comes from the pieces
  onMpn?: (mpn: string | null, locked: boolean) => void;
  // the kit's available count from the distributor stock (null when there are no parts), for the product's quantity field
  onAvail?: (a: Availability | null) => void;
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
  const [man, setMan] = useState({ part_number: "", mfr_number: "", same: false, description: "", unit_cost: "", quantity: "1" });
  // parts already typed for this vendor (vendors without a catalog), to pick instead of typing again
  type SavedPart = { id: number; part_number: string; mfr_number: string | null; description: string | null; unit_cost: number | null };
  const [savedQ, setSavedQ] = useState("");
  const [savedRes, setSavedRes] = useState<SavedPart[]>([]);
  const [savedTick, setSavedTick] = useState(0);
  const [editSaved, setEditSaved] = useState<{ id: number; mfr: string; desc: string; cost: string } | null>(null);
  // saving
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const [listings, setListings] = useState<Listing[]>([]);
  const [canSync, setCanSync] = useState(false);
  const [results2, setResults2] = useState<SyncResult[] | null>(null);
  const [smRes, setSmRes] = useState<SmResult[] | null>(null);
  const [avail, setAvail] = useState<Availability | null>(null);
  const [dirty, setDirty] = useState(false);
  const [savedCls, setSavedCls] = useState<string | null>(null);
  // the parts as last saved, so Cancel changes can put them back
  const [savedParts, setSavedParts] = useState<KitPart[]>([]);

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
  }, []);

  // the brand list belongs to the chosen vendor's catalog
  const supplier = vendor?.has_catalog ? vendor.supplier ?? "" : "";
  useEffect(() => {
    setBrand("");
    if (!supplier) return setBrands([]);
    api<{ items: Brand[] }>(`/kits/catalog/brands?supplier=${encodeURIComponent(supplier)}`).then((r) => setBrands(r.items)).catch(() => setBrands([]));
  }, [supplier]);

  // the saved parts of a vendor that has no catalog: refreshed when the vendor, the search, or the saved pieces change
  const manualVendorId = vendor && !vendor.has_catalog ? vendor.id : null;
  useEffect(() => {
    if (manualVendorId === null) return setSavedRes([]);
    const t = setTimeout(() => {
      api<{ items: SavedPart[] }>(`/kits/manual-catalog?distributor_id=${manualVendorId}&q=${encodeURIComponent(savedQ.trim())}`)
        .then((r) => setSavedRes(r.items))
        .catch(() => setSavedRes([]));
    }, 250);
    return () => clearTimeout(t);
  }, [manualVendorId, savedQ, savedParts, savedTick]);

  // existing product: load what is saved
  useEffect(() => {
    if (!productId) return;
    api<{ cls: string | null; parts: (Omit<KitPart, "key"> & { id: number })[]; listings: Listing[]; can_sync: boolean; mpn?: string | null; mpn_locked?: boolean; availability?: Availability | null }>(`/kits/${productId}`)
      .then((r) => {
        const loaded = r.parts.map((p) => ({ ...p, key: `p${p.id}`, description: p.description ?? "" }));
        onChange(loaded);
        setSavedParts(loaded);
        setListings(r.listings);
        setCanSync(r.can_sync);
        setSavedCls(r.cls);
        setDirty(false);
        onMpn?.(r.mpn ?? null, !!r.mpn_locked);
        setAvail(r.availability ?? null);
        onAvail?.(r.availability ?? null);
      })
      .catch((e) => setMsg({ kind: "error", text: errMsg(e) }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [productId]);

  const built = useMemo(() => previewCls(parts, labelBy), [parts, labelBy]);
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
      if (supplier) p.set("supplier", supplier);
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

  // after a part is added the search is finished: clear it so the Add button does not stay behind and get clicked twice
  function clearSearch() {
    setResults(null);
    setTotal(0);
    setOffset(0);
    setQ("");
    setCodes("");
    setAddQty({});
  }

  function addFromCatalog(it: CatItem) {
    if (!vendorId) return;
    const qty = Math.max(1, Math.min(1000, Number(addQty[it.id] || 1) || 1));
    const existing = parts.find((p) => p.supplier_item_id === it.id);
    if (existing) {
      edit(parts.map((p) => (p === existing ? { ...p, quantity: Math.min(1000, p.quantity + qty) } : p)));
      clearSearch();
      setMsg({ kind: "success", text: `${it.sku} was already in the list, so its quantity went up by ${qty}. Click Save distributor parts to keep it.` });
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
        mfr_number: it.mfr_number ?? null,
        description: [it.manufacturer, it.name].filter(Boolean).join(" ").slice(0, 250),
        unit_cost: it.dealer_cost ?? null,
        quantity: qty,
      },
    ]);
    clearSearch();
    setMsg({ kind: "success", text: `Added ${it.sku}. Click Save distributor parts to keep it.` });
  }

  function cancelChanges() {
    onChange(savedParts);
    setDirty(false);
    clearSearch();
    setMan({ part_number: "", mfr_number: "", same: false, description: "", unit_cost: "", quantity: "1" });
    setMsg(null);
  }

  // add a part that was typed before: nothing to type again
  function addSaved(it: SavedPart) {
    if (!vendorId) return setMsg({ kind: "error", text: "Pick the vendor first." });
    edit([
      ...parts,
      {
        key: newKey(),
        distributor_id: Number(vendorId),
        vendor: vendor?.name,
        supplier_item_id: null,
        manual_item_id: it.id,
        part_number: it.part_number,
        mfr_number: it.mfr_number || null,
        description: it.description ?? "",
        unit_cost: it.unit_cost,
        quantity: Math.max(1, Math.min(1000, Number(man.quantity) || 1)),
      },
    ]);
    setMsg(null);
  }

  async function saveSavedEdit() {
    if (!editSaved) return;
    const cost = editSaved.cost.trim() === "" ? null : Number(editSaved.cost);
    if (cost !== null && Number.isNaN(cost)) return setMsg({ kind: "error", text: "Cost must be a number." });
    try {
      await api(`/kits/manual-catalog/${editSaved.id}`, { method: "PUT", body: { mfr_number: editSaved.mfr.trim() || null, description: editSaved.desc.trim() || null, unit_cost: cost } });
      setEditSaved(null);
      setSavedTick((n) => n + 1);
      setMsg({ kind: "success", text: "Saved part updated. Products that use it follow the change; reopen a product to see it there." });
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    }
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
        mfr_number: (man.same ? man.part_number.trim() : man.mfr_number.trim()) || null,
        description: man.description.trim(),
        unit_cost: cost,
        quantity: Math.max(1, Math.min(1000, Number(man.quantity) || 1)),
      },
    ]);
    setMan({ part_number: "", mfr_number: "", same: false, description: "", unit_cost: "", quantity: "1" });
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
      manual_item_id: p.manual_item_id ?? null,
      part_number: p.part_number,
      mfr_number: (p.mfr_number ?? "").trim() || null,
      description: p.description || null,
      unit_cost: p.unit_cost,
      quantity: p.quantity,
    }));

  async function sendToEbay() {
    if (!productId) return;
    setBusy(true);
    setResults2(null);
    setSmRes(null);
    try {
      const r = await api<{ cls: string; results: SyncResult[]; inventory?: SmResult[] }>(`/kits/${productId}/sync`, { method: "POST" });
      setResults2(r.results);
      setSmRes(r.inventory ?? null);
      const fresh = await api<{ listings: Listing[] }>(`/kits/${productId}`);
      setListings(fresh.listings);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy(false);
    }
  }

  async function inventoryRecords(apply: boolean) {
    if (!productId) return;
    if (apply && !confirm("Change the custom label on the eBay inventory records listed as 'would change'?")) return;
    setBusy(true);
    try {
      const r = await api<{ results: SmResult[] }>("/ebay-edit/sm-label", { method: "POST", body: { product_id: productId, apply } });
      setSmRes(r.results);
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
      const r = await api<{ cls: string | null; cls_changed: boolean; duplicates: { sku: string; title: string }[]; listings_to_update: number; mpn?: string | null; mpn_locked?: boolean; availability?: Availability | null }>(
        `/kits/${productId}/parts`,
        { method: "PUT", body: { parts: payloadParts(), label_by: labelBy } }
      );
      setDirty(false);
      setSavedCls(r.cls);
      onSaved?.(r.cls);
      onMpn?.(r.mpn ?? null, !!r.mpn_locked);
      setAvail(r.availability ?? null);
      onAvail?.(r.availability ?? null);
      let text = !r.cls
        ? "Parts saved (no custom label yet)."
        : r.cls_changed
          ? `Parts saved. Custom label created: ${r.cls}.`
          : `Parts saved. The custom label stays ${r.cls} (it was not changed; edit the label box above to change it).`;
      if (r.duplicates.length) text += ` Warning: ${r.duplicates.map((d) => d.sku).join(", ")} already use the same label.`;
      if (r.cls && r.listings_to_update > 0 && !canSync) text += ` ${r.listings_to_update} eBay listing(s) still need the new label; an admin must send it.`;
      setMsg({ kind: "success", text });
      // read it again: the Send button decides from the listings' state, and the server may have refreshed the costs
      const fresh = await api<{ parts: (Omit<KitPart, "key"> & { id: number })[]; listings: Listing[] }>(`/kits/${productId}`);
      const loaded = fresh.parts.map((p) => ({ ...p, key: `p${p.id}`, description: p.description ?? "" }));
      onChange(loaded);
      setSavedParts(loaded);
      setListings(fresh.listings);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy(false);
    }
  }

  const pending = listings.filter((l) => !l.in_sync).length;
  // The label in the box above may be typed but not saved yet; Send always sends the saved one.
  const labelSaved = (currentCls ?? savedCls ?? "").trim() === (savedCls ?? "").trim();
  const showSend = !dirty && labelSaved && !!savedCls && listings.length > 0 && pending > 0 && canSync;
  const small = "rounded border border-gray-300 px-2 py-1 text-xs";

  return (
    <section className={cardCls}>
      <h2 className="text-lg font-semibold">Distributor parts</h2>
      <p className="text-xs text-gray-500">
        If this product is made of parts bought from a vendor, add them here. The custom label is automatically built from the part numbers. Each part also has its own manufacturer number; the product's Manufacturer part # shows them all.
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
                      {p.manual_item_id ? <span className="text-xs text-gray-500" title="Cost, description and manufacturer # come from the vendor's saved part">(saved part)</span> : null}
                    </div>
                    {p.description && <div className="break-words text-xs text-gray-700">{p.description}</div>}
                    <label className="mt-1 flex flex-wrap items-center gap-2 text-xs text-gray-600">
                      Manufacturer #
                      <input
                        type="text" maxLength={60} disabled={disabled} className={`${inputCls} font-mono`} style={{ width: 220 }}
                        placeholder={p.supplier_item_id ? "from the catalog" : p.manual_item_id ? "from the saved part" : "the manufacturer's part number"}
                        value={p.mfr_number ?? ""}
                        onChange={(e) => edit(parts.map((x) => (x === p ? { ...x, mfr_number: e.target.value } : x)))}
                      />
                    </label>
                    {(() => {
                      const a = avail?.parts.find((x) => `p${x.id}` === p.key);
                      if (!a) return null;
                      return (
                        <>
                          <div className={`mt-1 text-xs ${a.limiting ? "font-semibold text-amber-800" : "text-gray-600"}`}>
                            {a.matched
                              ? `Distributor stock ${a.stock.toLocaleString()}${a.need > 1 ? `, ${a.need} per kit` : ""}: enough for ${a.can_make.toLocaleString()}`
                              : "Not matched to a distributor catalog part, so it counts as 0"}
                          </div>
                          {avail?.is_kit && a.on_our_shelf !== undefined && (
                            <div className={`text-xs ${a.ours_limiting ? "font-semibold text-amber-800" : "text-gray-600"}`}>
                              {`On our shelf (our single-part products with this part): ${a.on_our_shelf.toLocaleString()}${a.need > 1 ? `, ${a.need} per kit` : ""}: enough for ${(a.ours_can_make ?? 0).toLocaleString()}`}
                            </div>
                          )}
                        </>
                      );
                    })()}
                  </div>
                  <div className="flex items-center gap-5 text-sm">
                    <div>
                      <div className="text-[11px] uppercase text-gray-500">Your cost</div>
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

      {avail && avail.parts.length > 0 && (
        <div className="rounded bg-gray-50 p-2 text-sm">
          <span className="text-xs text-gray-500">{avail.is_kit ? "Kit available from distributor stock: " : "Available from distributor stock: "}</span>
          <span className="font-semibold">{avail.available.toLocaleString()}</span>
          {avail.is_kit && avail.ours_available != null && (
            <>
              <span className="ml-4 text-xs text-gray-500">Kit on our own shelf (E&amp;I warehouse quantity): </span>
              <span className="font-semibold">{avail.ours_available.toLocaleString()}</span>
            </>
          )}
          {dirty && <span className="ml-2 text-xs text-gray-500">(as last saved; save the parts to update)</span>}
        </div>
      )}

      {parts.some((p) => p.supplier_item_id) && (
        <label className="flex flex-wrap items-center gap-2 text-xs">
          Write catalog parts in the label as
          <select className={inputCls} style={{ width: "auto" }} value={labelBy} disabled={disabled || !!keptCls} onChange={(e) => onLabelByChange(e.target.value as LabelBy)}>
            <option value="mfr">Manufacturer # (default)</option>
            <option value="sku">Vendor SKU</option>
          </select>
          {keptCls && <span className="text-gray-500">(this product already has a label, so this is not used)</span>}
        </label>
      )}

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
                <button type="button" className={`${btnCls} w-28 shrink-0 grow-0 whitespace-nowrap text-center`} disabled={searching} onClick={() => void search(0)}>{searching ? "Searching…" : "Search"}</button>
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
                        <tr><th className="p-1">SKU</th><th className="p-1">Mfr #</th><th className="p-1">Brand</th><th className="p-1">Name</th><th className="p-1">UPC</th><th className="p-1">Your cost</th><th className="p-1">Stock</th><th className="p-1">Qty</th><th className="p-1" /></tr>
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
                            <td className="p-1" title={supplier === "rmatv" ? `${it.qty_other ?? 0} in stock` : `ME ${it.qty_maine} · IN ${it.qty_indiana} · NV ${it.qty_nevada}`}>{it.in_stock_total}</td>
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
              <p className="text-xs text-gray-500">{vendor.name} has no catalog loaded here. Pick a part you already typed, or add a new one below.</p>
              <div className="space-y-1 rounded border border-gray-200 p-2">
                <label className="block text-xs">
                  Saved parts
                  <input className={inputCls} value={savedQ} placeholder="search vendor part #, manufacturer # or description" onChange={(e) => setSavedQ(e.target.value)} />
                </label>
                {savedRes.length === 0 ? (
                  <p className="text-xs text-gray-500">{savedQ.trim() ? "Nothing saved matches. Add it as a new part below." : `No parts saved for ${vendor.name} yet. Parts you add below are saved here when you save the distributor parts.`}</p>
                ) : (
                  <ul className="max-h-48 divide-y divide-gray-100 overflow-auto text-sm">
                    {savedRes.map((it) => (
                      <li key={it.id} className="flex items-center justify-between gap-2 py-1">
                        <span className="min-w-0 flex-1 truncate">
                          <span className="font-medium">{it.part_number}</span>
                          {it.mfr_number && <span className="ml-2 text-gray-600">mfr {it.mfr_number}</span>}
                          {it.description && <span className="ml-2 text-gray-500">{it.description}</span>}
                          {it.unit_cost !== null && <span className="ml-2 text-gray-600">${it.unit_cost.toFixed(2)}</span>}
                        </span>
                        <button type="button" className={btn2Cls} onClick={() => addSaved(it)}>Add</button>
                        <button type="button" className="px-1 text-xs text-gray-600 underline" onClick={() => setEditSaved({ id: it.id, mfr: it.mfr_number ?? "", desc: it.description ?? "", cost: it.unit_cost != null ? String(it.unit_cost) : "" })}>Edit</button>
                      </li>
                    ))}
                  </ul>
                )}
                {editSaved && (
                  <div className="flex flex-wrap items-end gap-2 rounded bg-gray-50 p-2">
                    <span className="text-xs text-gray-600">Editing the saved part: every product that uses it follows.</span>
                    <label className="text-xs">Manufacturer #<input className={inputCls} value={editSaved.mfr} onChange={(e) => setEditSaved({ ...editSaved, mfr: e.target.value })} /></label>
                    <label className="min-w-[10rem] flex-1 text-xs">Description<input className={inputCls} value={editSaved.desc} onChange={(e) => setEditSaved({ ...editSaved, desc: e.target.value })} /></label>
                    <label className="text-xs">Cost<input type="number" step="0.01" className={`${inputCls} w-24`} value={editSaved.cost} onChange={(e) => setEditSaved({ ...editSaved, cost: e.target.value })} /></label>
                    <button type="button" className={btnCls} onClick={() => void saveSavedEdit()}>Save</button>
                    <button type="button" className={btn2Cls} onClick={() => setEditSaved(null)}>Cancel</button>
                  </div>
                )}
                <p className="text-xs text-gray-500">The Qty below is used for the part you add. A part added from this list follows it: change it here and every product that uses it follows.</p>
              </div>
              <p className="text-xs font-medium text-gray-700">Not in the list? Add a new part</p>
              <div className="flex flex-wrap items-end gap-2">
                <label className="text-xs">Vendor part #<input className={inputCls} value={man.part_number} onChange={(e) => setMan({ ...man, part_number: e.target.value })} /></label>
                <label className="text-xs">Manufacturer #<input className={inputCls} value={man.same ? man.part_number : man.mfr_number} disabled={man.same} onChange={(e) => setMan({ ...man, mfr_number: e.target.value })} /></label>
                <label className="flex items-center gap-1 pb-2 text-xs" title="Use the vendor part # as the manufacturer # too"><input type="checkbox" checked={man.same} onChange={(e) => setMan({ ...man, same: e.target.checked })} />Same as vendor part #</label>
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
          {!disabled && (dirty || showSend) && (
            <div className="flex flex-wrap items-center gap-3">
              {dirty && (
                <>
                  <button type="button" className={btnCls} disabled={busy} onClick={() => void save()}>
                    {busy ? "Working…" : "Save distributor parts"}
                  </button>
                  <button type="button" className={btnCls} disabled={busy} onClick={cancelChanges}>
                    Cancel distributor parts changes
                  </button>
                  <span className="text-xs text-amber-700">Unsaved changes</span>
                </>
              )}
              {showSend && (
                <button type="button" className={btnCls} disabled={busy} onClick={() => void sendToEbay()}>
                  Send custom label to eBay
                </button>
              )}
            </div>
          )}
          {listings.length > 0 && (
            <p className="text-xs text-gray-600">
              {listings.length} eBay listing{listings.length === 1 ? "" : "s"} linked; {listings.length - pending} show this label{pending ? `, ${pending} not confirmed yet` : ""}.
            </p>
          )}
          {!disabled && !dirty && labelSaved && !!savedCls && listings.length > 0 && canSync && (
            <div className="space-y-2 rounded border border-gray-200 p-3">
              <p className="text-xs text-gray-600">
                Sending the label to eBay also updates the matching records in Seller Hub &gt; Listings &gt; Inventory. This checks or redoes only those records.
              </p>
              <div className="flex flex-wrap items-center gap-2">
                <button type="button" className={btn2Cls} disabled={busy} onClick={() => void inventoryRecords(false)}>Check eBay inventory records</button>
                {smRes && smRes.some((r) => r.status === "would_change") && (
                  <button type="button" className={btnCls} disabled={busy} onClick={() => void inventoryRecords(true)}>
                    Update {smRes.filter((r) => r.status === "would_change").length} inventory record(s)
                  </button>
                )}
              </div>
              {smRes && (
                <ul className="space-y-1 text-xs">
                  {smRes.map((r, i) => (
                    <li key={`${r.store}-${r.sm_product_id}-${i}`} className={r.status === "error" || r.status === "not_sent" ? "text-red-700" : "text-gray-700"}>
                      Inventory{r.name ? ` “${r.name}”` : ""} – <b>{r.status.replace(/_/g, " ")}</b>: {r.detail}
                    </li>
                  ))}
                </ul>
              )}
            </div>
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
