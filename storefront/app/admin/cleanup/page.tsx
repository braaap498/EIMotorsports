"use client";

import ClearableInput from "../../../components/admin/ClearableInput";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Badge, Field, Notice, btnCls, btn2Cls, dangerCls, inputCls } from "../../../components/admin/ui";

/* ------------------------------------------------------------------ types */

type Item = {
  id: number;
  ebay_item_id: string | null; // e.g. EBAY-178554199250
  custom_label_sku: string | null;
  title: string;
  list_price: number | string | null;
  quantity_on_hand: number;
  is_published: boolean;
  listing_count: number;
  fitment_count: number;
  prior_merges: number | string;
  part_number: string | null;
};
type SearchResp = { total: number; shown: number; items: Item[] };
type ItemsResp = { items: Item[]; not_found: string[] };

type YearFit = { vehicle_id: number; year: number; submodel: string | null; from_product_ids: number[] };
type ModelFit = { model_id: number; make: string; model: string; vehicle_type: string | null; years: YearFit[] };

type PreviewRow = Item & { sales_count: number; inventory_tx_count: number; po_line_count: number; photo_count: number };
type Preview = {
  fingerprint: string;
  survivor: PreviewRow;
  survivor_is_master: boolean;
  merging_away: PreviewRow[];
  totals: {
    products_deleted: number;
    ebay_listings_total: number;
    ebay_listings_moving: number;
    sales_rows_moving: number;
    inventory_rows_moving: number;
    po_lines_moving: number;
  };
  quantity: { survivor: number; largest_single: number; sum_of_all: number };
  proposed_fitment: ModelFit[];
  proposed_is_universal: boolean;
  warnings: string[];
  blockers: string[];
};
type MergeResult = {
  log_id: number;
  survivor_id: number;
  products_deleted: number;
  ebay_listings_now_on_part: number;
  fitment_vehicles: number;
  quantity_on_hand: number;
};
type LogRow = {
  id: number;
  created_at: string;
  merged_by_email: string | null;
  survivor_product_id: number;
  survivor_title: string | null;
  merged_count: number;
  qty_before: number;
  qty_after: number;
  note: string | null;
};
type Step = "search" | "review" | "fitment" | "confirm" | "done";

/* ---------------------------------------------------------------- helpers */

const money = (v: number | string | null) => (v == null ? "—" : Number(v).toFixed(2));
const isMaster = (p: Item) => Number(p.prior_merges) > 0;

/** The product most people want to keep: an existing master first, then the one with most eBay listings. */
function suggestKeeper(items: Item[]): number | null {
  if (!items.length) return null;
  return [...items].sort(
    (a, b) =>
      Number(b.prior_merges) - Number(a.prior_merges) || b.listing_count - a.listing_count || a.id - b.id
  )[0].id;
}

const STEPS: { key: Step; label: string }[] = [
  { key: "search", label: "1. Find & tick" },
  { key: "review", label: "2. Review" },
  { key: "fitment", label: "3. Compatible vehicles" },
  { key: "confirm", label: "4. Confirm" },
];

function Stepper({ step }: { step: Step }) {
  const idx = STEPS.findIndex((s) => s.key === step);
  return (
    <ol className="flex flex-wrap gap-2 text-xs font-medium">
      {STEPS.map((s, i) => (
        <li
          key={s.key}
          className={`rounded-full px-3 py-1 ${
            step === "done"
              ? "bg-green-100 text-green-800"
              : i === idx
              ? "bg-black text-white"
              : i < idx
              ? "bg-gray-200 text-gray-700"
              : "bg-gray-100 text-gray-400"
          }`}
        >
          {s.label}
        </li>
      ))}
    </ol>
  );
}

function ProductBadges({ p }: { p: Item }) {
  return (
    <span className="flex flex-wrap gap-1">
      {isMaster(p) && <Badge tone="blue">MASTER · {Number(p.prior_merges)} merged</Badge>}
      <Badge tone={p.listing_count > 0 ? "green" : "gray"}>{p.listing_count} eBay</Badge>
      {!p.is_published && <Badge tone="amber">Draft</Badge>}
    </span>
  );
}

/* ------------------------------------------------------- history (bottom) */

function HistoryPanel({ refreshKey }: { refreshKey: number }) {
  const [open, setOpen] = useState(false);
  const [rows, setRows] = useState<LogRow[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    api<LogRow[]>("/cleanup/log?limit=50")
      .then((r) => {
        setRows(r);
        setError("");
      })
      .catch((e) => setError(errMsg(e)));
  }, [open, refreshKey]);

  return (
    <section className="rounded-lg border border-gray-200 bg-white p-4">
      <button type="button" className="text-sm font-semibold underline" onClick={() => setOpen((o) => !o)}>
        {open ? "Hide" : "Show"} recent merges
      </button>
      {open && (
        <div className="mt-3">
          {error && <Notice kind="error">{error}</Notice>}
          {rows && rows.length === 0 && <p className="text-sm text-gray-500">No merges yet.</p>}
          {rows && rows.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="bg-gray-50 text-xs uppercase text-gray-500">
                  <tr>
                    <th className="px-2 py-2">#</th>
                    <th className="px-2 py-2">When</th>
                    <th className="px-2 py-2">Who</th>
                    <th className="px-2 py-2">Kept part</th>
                    <th className="px-2 py-2">Merged</th>
                    <th className="px-2 py-2">Stock</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {rows.map((r) => (
                    <tr key={r.id}>
                      <td className="px-2 py-2 text-gray-500">{r.id}</td>
                      <td className="whitespace-nowrap px-2 py-2">{new Date(r.created_at).toLocaleString()}</td>
                      <td className="px-2 py-2">{r.merged_by_email ?? "—"}</td>
                      <td className="max-w-xs px-2 py-2">
                        <Link href={`/admin/products/${r.survivor_product_id}`} className="hover:underline">
                          {r.survivor_title ?? `Product ${r.survivor_product_id}`}
                        </Link>
                        {r.note && <div className="text-xs text-gray-500">{r.note}</div>}
                      </td>
                      <td className="px-2 py-2">{r.merged_count}</td>
                      <td className="whitespace-nowrap px-2 py-2">
                        {r.qty_before} → {r.qty_after}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

/* --------------------------------------- add vehicles that are not listed */

type Opt = { id: number; name: string };

function AddVehicles({
  onAdd,
}: {
  onAdd: (model: { id: number; name: string; make: string }, years: { vehicle_id: number; year: number; submodel: string | null }[]) => void;
}) {
  const [makes, setMakes] = useState<Opt[]>([]);
  const [makeId, setMakeId] = useState("");
  const [modelQ, setModelQ] = useState("");
  const [models, setModels] = useState<Opt[]>([]);
  const [modelId, setModelId] = useState("");
  const [years, setYears] = useState<{ vehicle_id: number; year: number; submodel: string | null }[]>([]);
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [error, setError] = useState("");

  useEffect(() => {
    api<Opt[]>("/makes").then(setMakes).catch((e) => setError(errMsg(e)));
  }, []);

  useEffect(() => {
    setModelId("");
    setModels([]);
    if (!makeId) return;
    const p = new URLSearchParams({ make_id: makeId, limit: "200" });
    if (modelQ.trim()) p.set("q", modelQ.trim());
    api<{ items: Opt[] }>(`/models?${p}`)
      .then((r) => setModels(r.items))
      .catch((e) => setError(errMsg(e)));
  }, [makeId, modelQ]);

  useEffect(() => {
    setYears([]);
    setPicked(new Set());
    if (!modelId) return;
    api<{ vehicle_id: number; year: number; submodel: string | null }[]>(`/models/${modelId}/years`)
      .then((rows) => setYears([...rows].sort((a, b) => a.year - b.year)))
      .catch((e) => setError(errMsg(e)));
  }, [modelId]);

  function toggle(id: number) {
    setPicked((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  }

  function add() {
    const model = models.find((m) => String(m.id) === modelId);
    const make = makes.find((m) => String(m.id) === makeId);
    if (!model || !make) return;
    onAdd({ id: model.id, name: model.name, make: make.name }, years.filter((y) => picked.has(y.vehicle_id)));
    setPicked(new Set());
  }

  return (
    <div className="space-y-3 rounded-lg border border-gray-200 bg-white p-4">
      <h3 className="text-sm font-semibold">Add a vehicle that is not in the list</h3>
      {error && <Notice kind="error">{error}</Notice>}
      <div className="grid gap-3 sm:grid-cols-3">
        <Field label="Make">
          <select className={inputCls} value={makeId} onChange={(e) => setMakeId(e.target.value)}>
            <option value="">Choose make</option>
            {makes.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Narrow models (optional)">
          <ClearableInput className={inputCls} value={modelQ} onChange={(e) => setModelQ(e.target.value)} placeholder="e.g. Rubicon" disabled={!makeId} />
        </Field>
        <Field label="Model">
          <select className={inputCls} value={modelId} onChange={(e) => setModelId(e.target.value)} disabled={!makeId}>
            <option value="">Choose model</option>
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}
              </option>
            ))}
          </select>
        </Field>
      </div>
      {years.length > 0 && (
        <div className="space-y-2">
          <div className="flex flex-wrap gap-2">
            {years.map((y) => (
              <button
                key={y.vehicle_id}
                type="button"
                onClick={() => toggle(y.vehicle_id)}
                className={`rounded border px-2 py-1 text-xs font-medium ${
                  picked.has(y.vehicle_id) ? "border-black bg-black text-white" : "border-gray-300 bg-white text-gray-800"
                }`}
              >
                {y.year}
                {y.submodel ? ` ${y.submodel}` : ""}
              </button>
            ))}
          </div>
          <div className="flex gap-2">
            <button type="button" className={btn2Cls} onClick={() => setPicked(new Set(years.map((y) => y.vehicle_id)))}>
              All years
            </button>
            <button type="button" className={btnCls} disabled={picked.size === 0} onClick={add}>
              Add {picked.size || ""} to the list
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------ step 3: fitment checklist */

function FitmentStep({
  models,
  setModels,
  chosen,
  setChosen,
  added,
  setAdded,
  totalProducts,
  universal,
  setUniversal,
}: {
  models: ModelFit[];
  setModels: (m: ModelFit[]) => void;
  chosen: Set<number>;
  setChosen: (s: Set<number>) => void;
  added: Set<number>;
  setAdded: (s: Set<number>) => void;
  totalProducts: number;
  universal: boolean;
  setUniversal: (b: boolean) => void;
}) {
  const [filter, setFilter] = useState("");
  const shown = useMemo(() => {
    const f = filter.trim().toLowerCase();
    if (!f) return models;
    return models.filter((m) => `${m.make} ${m.model}`.toLowerCase().includes(f));
  }, [models, filter]);

  function setMany(ids: number[], on: boolean) {
    const n = new Set(chosen);
    ids.forEach((id) => (on ? n.add(id) : n.delete(id)));
    setChosen(n);
  }

  function addVehicles(
    model: { id: number; name: string; make: string },
    rows: { vehicle_id: number; year: number; submodel: string | null }[]
  ) {
    const next = models.map((m) => ({ ...m, years: [...m.years] }));
    let target = next.find((m) => m.model_id === model.id);
    if (!target) {
      target = { model_id: model.id, make: model.make, model: model.name, vehicle_type: null, years: [] };
      next.push(target);
    }
    const have = new Set(target.years.map((y) => y.vehicle_id));
    const newAdded = new Set(added);
    for (const r of rows) {
      if (!have.has(r.vehicle_id)) {
        target.years.push({ vehicle_id: r.vehicle_id, year: r.year, submodel: r.submodel, from_product_ids: [] });
        newAdded.add(r.vehicle_id);
      }
    }
    target.years.sort((a, b) => a.year - b.year);
    next.sort((a, b) => `${a.make} ${a.model}`.localeCompare(`${b.make} ${b.model}`));
    setModels(next);
    setAdded(newAdded);
    setMany(rows.map((r) => r.vehicle_id), true);
  }

  const totalVehicles = models.reduce((n, m) => n + m.years.length, 0);
  const removed = totalVehicles - added.size - Array.from(chosen).filter((id) => !added.has(id)).length;

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-gray-200 bg-white p-4">
        <p className="text-sm text-gray-700">
          This is every vehicle that any of the {totalProducts} products fits, combined. Only the ones that are
          <strong> highlighted </strong>
          will be fitted to the kept part. Click a year to remove or add it. Nothing is saved until the final step.
        </p>
        <div className="mt-3 flex flex-wrap items-center gap-3 text-sm">
          <Badge tone="blue">{chosen.size} vehicles will be fitted</Badge>
          {added.size > 0 && <Badge tone="green">{Array.from(chosen).filter((id) => added.has(id)).length} added by you</Badge>}
          {removed > 0 && <Badge tone="amber">{removed} proposed ones removed</Badge>}
          <button type="button" className={btn2Cls} onClick={() => setChosen(new Set(models.flatMap((m) => m.years.map((y) => y.vehicle_id))))}>
            Select all
          </button>
          <button type="button" className={btn2Cls} onClick={() => setChosen(new Set())}>
            Select none
          </button>
        </div>
        <label className="mt-3 flex items-center gap-2 text-sm">
          <input type="checkbox" checked={universal} onChange={(e) => setUniversal(e.target.checked)} />
          Mark this part as universal fit
        </label>
      </div>

      <AddVehicles onAdd={addVehicles} />

      <div className="rounded-lg border border-gray-200 bg-white p-4">
        <div className="mb-3 max-w-sm">
          <ClearableInput className={inputCls} placeholder="Filter the list (make or model)…" value={filter} onChange={(e) => setFilter(e.target.value)} />
        </div>
        {models.length === 0 && <p className="text-sm text-gray-500">None of these products had any fitment. Add vehicles above.</p>}
        <div className="space-y-4">
          {shown.map((m) => {
            const ids = m.years.map((y) => y.vehicle_id);
            const on = ids.filter((id) => chosen.has(id)).length;
            return (
              <div key={m.model_id} className="border-b border-gray-100 pb-3 last:border-0">
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  <span className="text-sm font-semibold">
                    {m.make} {m.model}
                  </span>
                  {m.vehicle_type && <Badge>{m.vehicle_type}</Badge>}
                  <span className="text-xs text-gray-500">
                    {on} of {ids.length} years
                  </span>
                  <button type="button" className="text-xs underline" onClick={() => setMany(ids, true)}>
                    all
                  </button>
                  <button type="button" className="text-xs text-red-700 underline" onClick={() => setMany(ids, false)}>
                    remove model
                  </button>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {m.years.map((y) => {
                    const sel = chosen.has(y.vehicle_id);
                    const wasAdded = added.has(y.vehicle_id);
                    return (
                      <button
                        key={y.vehicle_id}
                        type="button"
                        onClick={() => setMany([y.vehicle_id], !sel)}
                        title={wasAdded ? "Added by you" : `On ${y.from_product_ids.length} of ${totalProducts} products`}
                        className={`rounded border px-2 py-1 text-xs font-medium ${
                          sel
                            ? wasAdded
                              ? "border-green-700 bg-green-700 text-white"
                              : "border-black bg-black text-white"
                            : "border-gray-300 bg-white text-gray-400 line-through"
                        }`}
                      >
                        {y.year}
                        {y.submodel ? ` ${y.submodel}` : ""}
                      </button>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------- the page */

export default function PartsCleanupPage() {
  const [step, setStep] = useState<Step>("search");

  // step 1
  const [qInput, setQInput] = useState("");
  const [searchedQ, setSearchedQ] = useState("");
  const [items, setItems] = useState<Item[]>([]);
  const [total, setTotal] = useState(0);
  const [searching, setSearching] = useState(false);
  const [searchErr, setSearchErr] = useState("");
  // "Search Item Number" box: the list is built up one lookup at a time
  const [itemInput, setItemInput] = useState("");
  const [listMode, setListMode] = useState<"title" | "items">("title");
  const [itemSearching, setItemSearching] = useState(false);
  const [notFound, setNotFound] = useState<string[]>([]);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [keeper, setKeeper] = useState<number | null>(null);
  const [previewing, setPreviewing] = useState(false);

  // steps 2-4
  const [preview, setPreview] = useState<Preview | null>(null);
  const [models, setModels] = useState<ModelFit[]>([]);
  const [chosen, setChosen] = useState<Set<number>>(new Set());
  const [added, setAdded] = useState<Set<number>>(new Set());
  const [universal, setUniversal] = useState(false);
  const [ack, setAck] = useState(false);
  const [qty, setQty] = useState("");
  const [note, setNote] = useState("");
  const [typed, setTyped] = useState("");
  const [merging, setMerging] = useState(false);
  const [mergeErr, setMergeErr] = useState<{ text: string; stale: boolean } | null>(null);
  const [result, setResult] = useState<MergeResult | null>(null);
  const [logKey, setLogKey] = useState(0);

  // Warn before a refresh or tab close while a merge is half-set-up.
  useEffect(() => {
    if (step === "search" || step === "done") return;
    const h = (e: BeforeUnloadEvent) => {
      e.preventDefault();
    };
    window.addEventListener("beforeunload", h);
    return () => window.removeEventListener("beforeunload", h);
  }, [step]);

  const runSearch = useCallback(async (q: string) => {
    setSearching(true);
    setSearchErr("");
    try {
      const r = await api<SearchResp>(`/cleanup/search?q=${encodeURIComponent(q)}&limit=400`);
      setItems(r.items);
      setTotal(r.total);
      setSearchedQ(q);
      setListMode("title");
      setNotFound([]);
      setSelected(new Set());
      setKeeper(null);
    } catch (e) {
      setSearchErr(errMsg(e));
    } finally {
      setSearching(false);
    }
  }, []);

  /** Item-number search: adds the part(s) to the list instead of replacing it. */
  async function addByItemNumbers(raw: string) {
    setItemSearching(true);
    setSearchErr("");
    try {
      const r = await api<ItemsResp>(`/cleanup/by-items?ids=${encodeURIComponent(raw)}`);
      // A list that came from a title search is replaced, so only the numbers typed here are shown.
      const base = listMode === "items" ? items : [];
      const have = new Set(base.map((i) => i.id));
      const merged = [...base, ...r.items.filter((i) => !have.has(i.id))];
      setItems(merged);
      setTotal(merged.length);
      setListMode("items");
      setSearchedQ("item numbers");
      setNotFound(r.not_found);
      if (listMode !== "items") {
        setSelected(new Set());
        setKeeper(null);
      }
      setItemInput("");
    } catch (e) {
      setSearchErr(errMsg(e));
    } finally {
      setItemSearching(false);
    }
  }

  /** Re-reads the current list from the database (after a merge, or after "Start again"). */
  async function reloadList() {
    if (listMode === "items") {
      if (items.length === 0) return;
      try {
        const r = await api<ItemsResp>(`/cleanup/by-items?ids=${encodeURIComponent(items.map((i) => i.ebay_item_id).filter(Boolean).join(","))}`);
        setItems(r.items); // parts that were merged away simply drop out of the list
        setTotal(r.items.length);
        setNotFound([]);
        setSelected(new Set());
        setKeeper(null);
      } catch (e) {
        setSearchErr(errMsg(e));
      }
    } else if (searchedQ) {
      await runSearch(searchedQ);
    }
  }

  function removeFromList(id: number) {
    setItems((list) => list.filter((i) => i.id !== id));
    const n = new Set(selected);
    n.delete(id);
    setSelected(n);
    if (keeper === id) setKeeper(null);
  }

  function clearList() {
    setItems([]);
    setTotal(0);
    setSearchedQ("");
    setListMode("title");
    setNotFound([]);
    setSelected(new Set());
    setKeeper(null);
  }

  function applySelection(next: Set<number>) {
    setSelected(next);
    const chosenItems = items.filter((i) => next.has(i.id));
    if (keeper == null || !next.has(keeper)) setKeeper(suggestKeeper(chosenItems));
  }
  function toggle(id: number) {
    const n = new Set(selected);
    if (n.has(id)) n.delete(id);
    else n.add(id);
    applySelection(n);
  }

  async function startReview() {
    if (keeper == null) return;
    setPreviewing(true);
    setSearchErr("");
    try {
      const pv = await api<Preview>("/cleanup/preview", {
        method: "POST",
        body: { survivor_id: keeper, product_ids: Array.from(selected) },
      });
      setPreview(pv);
      setModels(pv.proposed_fitment);
      setChosen(new Set(pv.proposed_fitment.flatMap((m) => m.years.map((y) => y.vehicle_id))));
      setAdded(new Set());
      setUniversal(pv.proposed_is_universal);
      setAck(false);
      setQty("");
      setNote("");
      setTyped("");
      setMergeErr(null);
      setStep("review");
    } catch (e) {
      setSearchErr(errMsg(e));
    } finally {
      setPreviewing(false);
    }
  }

  function cancel() {
    // Nothing has been written, so cancelling is just going back.
    setStep("search");
    setPreview(null);
    setMergeErr(null);
  }

  async function commit() {
    if (!preview) return;
    setMerging(true);
    setMergeErr(null);
    try {
      const res = await api<MergeResult>("/cleanup/merge", {
        method: "POST",
        body: {
          survivor_id: preview.survivor.id,
          product_ids: [preview.survivor.id, ...preview.merging_away.map((r) => r.id)],
          fingerprint: preview.fingerprint,
          vehicle_ids: Array.from(chosen),
          is_universal: universal,
          quantity_on_hand: Number(qty),
          confirm_count: preview.totals.products_deleted,
          note: note.trim() || null,
        },
      });
      setResult(res);
      setLogKey((k) => k + 1);
      setStep("done");
    } catch (e) {
      const text = errMsg(e);
      setMergeErr({ text, stale: /changed since|no longer exist|start again|preview/i.test(text) });
    } finally {
      setMerging(false);
    }
  }

  async function backToSearch() {
    setStep("search");
    setPreview(null);
    setResult(null);
    await reloadList();
  }

  const selectedItems = items.filter((i) => selected.has(i.id));
  const tooMany = selected.size - 1 > 1500;

  /* ------------------------------------------------------------- render */
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">Parts clean-up</h1>
        <Stepper step={step} />
      </div>

      {/* ============================ step 1: search ============================ */}
      {step === "search" && (
        <>
          <Notice>
            Search for a part, tick every listing that is the <strong>same physical part</strong>, and pick the one to
            keep. Nothing is changed until the last step, and you can cancel at any point before it. To link one new
            duplicate to an existing master, tick that duplicate and the master (marked MASTER).
          </Notice>

          <div className="flex flex-wrap items-end gap-x-6 gap-y-3">
            <form
              className="flex flex-wrap items-end gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                if (qInput.trim().length >= 2) runSearch(qInput.trim());
              }}
            >
              <Field label="Search title or SKU" className="w-full max-w-md">
                <ClearableInput className={inputCls} value={qInput} onChange={(e) => setQInput(e.target.value)} placeholder='e.g. "Tusk 1.8 bar"' autoFocus />
              </Field>
              <button className={btnCls} disabled={searching || qInput.trim().length < 2}>
                {searching ? "Searching…" : "Search"}
              </button>
            </form>

            <form
              className="flex flex-wrap items-end gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                if (itemInput.trim()) addByItemNumbers(itemInput.trim());
              }}
            >
              <Field label="Search Item Number" className="w-full max-w-md">
                <ClearableInput
                  className={inputCls}
                  value={itemInput}
                  onChange={(e) => setItemInput(e.target.value)}
                  placeholder="e.g. EBAY-178554199250, EBAY-178554199250"
                />
              </Field>
              <button className={btnCls} disabled={itemSearching || !itemInput.trim()}>
                {itemSearching ? "Searching…" : "Search"}
              </button>
            </form>
          </div>

          {searchErr && <Notice kind="error">{searchErr}</Notice>}
          {notFound.length > 0 && (
            <Notice kind="error">
              Not found: {notFound.join(", ")}. Check the number and try again.
            </Notice>
          )}

          {searchedQ && !searching && (
            <>
              {listMode === "title" && total > items.length && (
                <Notice>
                  Showing the first {items.length} of {total} matches. Add more words to the search so every duplicate
                  is in the list before you tick them.
                </Notice>
              )}
              <div className="flex flex-wrap items-center gap-3 text-sm">
                <span className="text-gray-600">
                  {listMode === "items"
                    ? `${items.length} part${items.length === 1 ? "" : "s"} added by item number`
                    : `${items.length} result${items.length === 1 ? "" : "s"} for “${searchedQ}”`}
                </span>
                <button type="button" className={btn2Cls} onClick={() => applySelection(new Set(items.map((i) => i.id)))} disabled={items.length === 0}>
                  Tick all {items.length}
                </button>
                <button type="button" className={btn2Cls} onClick={() => applySelection(new Set())} disabled={selected.size === 0}>
                  Clear ticks
                </button>
                {listMode === "items" && (
                  <button type="button" className={btn2Cls} onClick={clearList}>
                    Clear list
                  </button>
                )}
              </div>

              <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white">
                <table className="w-full text-left text-sm">
                  <thead className="bg-gray-50 text-xs uppercase text-gray-500">
                    <tr>
                      <th className="px-3 py-2">Merge</th>
                      <th className="px-3 py-2">Keep</th>
                      <th className="px-3 py-2">eBay item #</th>
                      <th className="px-3 py-2">Custom label SKU</th>
                      <th className="px-3 py-2">Title</th>
                      <th className="px-3 py-2">Part #</th>
                      <th className="px-3 py-2">Price</th>
                      <th className="px-3 py-2">Qty</th>
                      <th className="px-3 py-2">Fits</th>
                      <th className="px-3 py-2" />
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100">
                    {items.map((p) => (
                      <tr key={p.id} className={selected.has(p.id) ? "bg-blue-50" : ""}>
                        <td className="px-3 py-2">
                          <input type="checkbox" checked={selected.has(p.id)} onChange={() => toggle(p.id)} aria-label={`Merge ${p.ebay_item_id ?? p.title}`} />
                        </td>
                        <td className="px-3 py-2">
                          <input
                            type="radio"
                            name="keeper"
                            checked={keeper === p.id}
                            disabled={!selected.has(p.id)}
                            onChange={() => setKeeper(p.id)}
                            aria-label={`Keep ${p.ebay_item_id ?? p.title}`}
                          />
                        </td>
                        <td className="whitespace-nowrap px-3 py-2 text-gray-600">{p.ebay_item_id ?? "—"}</td>
                        <td className="whitespace-nowrap px-3 py-2 text-gray-600">{p.custom_label_sku ?? "—"}</td>
                        <td className="max-w-md px-3 py-2">
                          <div className="font-medium">{p.title}</div>
                        </td>
                        <td className="whitespace-nowrap px-3 py-2 text-gray-600">{p.part_number ?? "—"}</td>
                        <td className="px-3 py-2">{money(p.list_price)}</td>
                        <td className="px-3 py-2">{p.quantity_on_hand}</td>
                        <td className="px-3 py-2">{p.fitment_count}</td>
                        <td className="px-3 py-2">
                          <ProductBadges p={p} />
                          {listMode === "items" && (
                            <button type="button" className="mt-1 text-xs text-red-700 underline" onClick={() => removeFromList(p.id)}>
                              remove from list
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                    {items.length === 0 && (
                      <tr>
                        <td colSpan={10} className="px-3 py-6 text-center text-gray-500">
                          {listMode === "items" ? "Nothing in the list." : "Nothing matches. Try fewer words."}
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>

              {selected.size > 0 && (
                <div className="sticky bottom-3 flex flex-wrap items-center gap-3 rounded-lg border border-gray-300 bg-white p-3 shadow-lg">
                  <span className="text-sm">
                    {selected.size} ticked
                    {keeper != null && (
                      <>
                        {" "}
                        · keeping <strong>{items.find((i) => i.id === keeper)?.ebay_item_id ?? "this part"}</strong>
                      </>
                    )}
                  </span>
                  <button className={btnCls} disabled={selected.size < 2 || keeper == null || previewing || tooMany} onClick={startReview}>
                    {previewing ? "Checking…" : `Review merge of ${selected.size}`}
                  </button>
                  {selected.size < 2 && <span className="text-xs text-gray-500">Tick at least two.</span>}
                  {tooMany && <span className="text-xs text-red-700">Too many for one merge (max 1500 plus the kept one).</span>}
                  {selectedItems.length > 0 && keeper != null && !isMaster(items.find((i) => i.id === keeper)!) && selectedItems.some(isMaster) && (
                    <span className="text-xs text-amber-700">A MASTER is ticked. You usually want to keep that one.</span>
                  )}
                </div>
              )}
            </>
          )}

          <HistoryPanel refreshKey={logKey} />
        </>
      )}

      {/* ============================ step 2: review ============================ */}
      {step === "review" && preview && (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {[
              ["Products deleted", preview.totals.products_deleted],
              ["eBay listings moved to the kept part", preview.totals.ebay_listings_moving],
              ["Sales rows moved", preview.totals.sales_rows_moving],
              ["Inventory history rows moved", preview.totals.inventory_rows_moving],
            ].map(([label, value]) => (
              <div key={String(label)} className="rounded-lg border border-gray-200 bg-white p-4">
                <div className="text-3xl font-semibold">{value}</div>
                <div className="text-xs text-gray-500">{label}</div>
              </div>
            ))}
          </div>

          <section className="rounded-lg border-2 border-green-600 bg-white p-4">
            <div className="mb-1 flex flex-wrap items-center gap-2">
              <Badge tone="green">KEPT</Badge>
              <ProductBadges p={preview.survivor} />
            </div>
            <div className="font-semibold">{preview.survivor.title}</div>
            <div className="text-sm text-gray-600">
              eBay item {preview.survivor.ebay_item_id ?? "—"} · Custom label SKU {preview.survivor.custom_label_sku ?? "—"} · Part # {preview.survivor.part_number ?? "—"} · {preview.survivor.listing_count} eBay listing
              {preview.survivor.listing_count === 1 ? "" : "s"} · qty {preview.survivor.quantity_on_hand}
            </div>
            <p className="mt-2 text-xs text-gray-500">
              All eBay item numbers from the products below are re-linked to this part, so eBay sales still land on it.
              The website title stays as shown here. You can retitle the part after the merge.
            </p>
          </section>

          {preview.blockers.length > 0 && (
            <div className="space-y-2">
              {preview.blockers.map((b) => (
                <Notice key={b} kind="error">
                  {b}
                </Notice>
              ))}
            </div>
          )}
          {preview.warnings.length > 0 && (
            <div className="space-y-2">
              {preview.warnings.map((w) => (
                <p key={w} className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
                  ⚠ {w}
                </p>
              ))}
              <label className="flex items-center gap-2 text-sm font-medium">
                <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />I have read the warnings and these really are the same part
              </label>
            </div>
          )}

          <details className="rounded-lg border border-gray-200 bg-white p-4">
            <summary className="cursor-pointer text-sm font-semibold">
              The {preview.merging_away.length} products that will be deleted (their eBay links move to the kept part)
            </summary>
            <div className="mt-3 max-h-96 overflow-auto">
              <table className="w-full text-left text-sm">
                <thead className="sticky top-0 bg-gray-50 text-xs uppercase text-gray-500">
                  <tr>
                    <th className="px-2 py-2">eBay item #</th>
                    <th className="px-2 py-2">Custom label SKU</th>
                    <th className="px-2 py-2">Title</th>
                    <th className="px-2 py-2">Part #</th>
                    <th className="px-2 py-2">Qty</th>
                    <th className="px-2 py-2">eBay</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {preview.merging_away.map((r) => (
                    <tr key={r.id}>
                      <td className="whitespace-nowrap px-2 py-1 text-gray-600">{r.ebay_item_id ?? "—"}</td>
                      <td className="whitespace-nowrap px-2 py-1 text-gray-600">{r.custom_label_sku ?? "—"}</td>
                      <td className="px-2 py-1">{r.title}</td>
                      <td className="whitespace-nowrap px-2 py-1 text-gray-600">{r.part_number ?? "—"}</td>
                      <td className="px-2 py-1">{r.quantity_on_hand}</td>
                      <td className="px-2 py-1">{r.listing_count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>

          <div className="flex flex-wrap gap-3">
            <button className={btnCls} disabled={preview.blockers.length > 0 || (preview.warnings.length > 0 && !ack)} onClick={() => setStep("fitment")}>
              Next: compatible vehicles
            </button>
            <button className={btn2Cls} onClick={cancel}>
              Cancel — nothing has been changed
            </button>
          </div>
        </>
      )}

      {/* ============================ step 3: fitment ============================ */}
      {step === "fitment" && preview && (
        <>
          <FitmentStep
            models={models}
            setModels={setModels}
            chosen={chosen}
            setChosen={setChosen}
            added={added}
            setAdded={setAdded}
            totalProducts={preview.merging_away.length + 1}
            universal={universal}
            setUniversal={setUniversal}
          />
          <div className="sticky bottom-3 flex flex-wrap items-center gap-3 rounded-lg border border-gray-300 bg-white p-3 shadow-lg">
            <button className={btn2Cls} onClick={() => setStep("review")}>
              Back
            </button>
            <button className={btnCls} disabled={chosen.size === 0 && !universal} onClick={() => setStep("confirm")}>
              Next: confirm ({chosen.size} vehicles)
            </button>
            <button className={btn2Cls} onClick={cancel}>
              Cancel — nothing has been changed
            </button>
            {chosen.size === 0 && !universal && <span className="text-xs text-red-700">Choose at least one vehicle, or mark the part universal.</span>}
          </div>
        </>
      )}

      {/* ============================ step 4: confirm ============================ */}
      {step === "confirm" && preview && (
        <>
          <section className="space-y-3 rounded-lg border border-gray-200 bg-white p-4">
            <h2 className="text-lg font-semibold">What will happen</h2>
            <ul className="list-disc space-y-1 pl-5 text-sm">
              <li>
                Keep <strong>{preview.survivor.ebay_item_id ?? preview.survivor.title}</strong> — {preview.survivor.title}
              </li>
              <li>
                <strong>Delete {preview.totals.products_deleted} products</strong> (a full copy is saved in the merge history)
              </li>
              <li>
                All <strong>{preview.totals.ebay_listings_total}</strong> eBay item numbers end up linked to the kept part, so sales and stock keep
                tracking
              </li>
              <li>
                {preview.totals.sales_rows_moving} sales rows, {preview.totals.inventory_rows_moving} inventory history rows and{" "}
                {preview.totals.po_lines_moving} purchase-order lines move to the kept part
              </li>
              <li>
                Fitment becomes exactly <strong>{chosen.size}</strong> vehicles{universal ? " and the part is marked universal" : ""}
              </li>
            </ul>
          </section>

          <section className="space-y-3 rounded-lg border border-gray-200 bg-white p-4">
            <h2 className="text-lg font-semibold">How many are really on the shelf?</h2>
            <p className="text-sm text-gray-700">
              Every duplicate was counting the same shelf, so the numbers are <strong>not</strong> added together. Type the real quantity.
            </p>
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <button type="button" className={btn2Cls} onClick={() => setQty(String(preview.quantity.survivor))}>
                Kept part has {preview.quantity.survivor}
              </button>
              <button type="button" className={btn2Cls} onClick={() => setQty(String(preview.quantity.largest_single))}>
                Largest single listing: {preview.quantity.largest_single}
              </button>
              <span className="text-xs text-gray-500">(adding them all up would give {preview.quantity.sum_of_all}, which is usually wrong)</span>
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Quantity on hand after the merge">
                <input className={inputCls} inputMode="numeric" value={qty} onChange={(e) => setQty(e.target.value.replace(/[^0-9]/g, ""))} />
              </Field>
              <Field label="Note for the merge history (optional)">
                <input className={inputCls} value={note} onChange={(e) => setNote(e.target.value)} placeholder="e.g. Tusk 1.8 bar caps" />
              </Field>
            </div>
            {qty !== "" && Number(qty) > preview.quantity.largest_single && (
              <Notice kind="error">That is more than any single listing had ({preview.quantity.largest_single}). Check it before you continue.</Notice>
            )}
            {qty !== "" && (
              <p className="text-sm text-gray-600">
                Stock changes from {preview.quantity.survivor} to {Number(qty)} (recorded as an inventory adjustment).
              </p>
            )}
          </section>

          <section className="space-y-3 rounded-lg border-2 border-red-300 bg-white p-4">
            <h2 className="text-lg font-semibold text-red-800">Final check</h2>
            <p className="text-sm">
              To delete {preview.totals.products_deleted} products and merge them, type <code className="rounded bg-gray-100 px-1">merge {preview.totals.products_deleted}</code> below.
            </p>
            <div className="max-w-xs">
              <input className={inputCls} value={typed} onChange={(e) => setTyped(e.target.value)} placeholder={`merge ${preview.totals.products_deleted}`} autoComplete="off" />
            </div>
            {mergeErr && (
              <div className="space-y-2">
                <Notice kind="error">{mergeErr.text}</Notice>
                {mergeErr.stale && (
                  <button className={btn2Cls} onClick={() => { cancel(); reloadList(); }}>
                    Start again
                  </button>
                )}
              </div>
            )}
            <div className="flex flex-wrap gap-3">
              <button
                className={dangerCls}
                disabled={merging || qty === "" || typed.trim().toLowerCase() !== `merge ${preview.totals.products_deleted}`}
                onClick={commit}
              >
                {merging ? "Merging…" : "Commit merge"}
              </button>
              <button className={btn2Cls} disabled={merging} onClick={() => setStep("fitment")}>
                Back
              </button>
              <button className={btn2Cls} disabled={merging} onClick={cancel}>
                Cancel — nothing has been changed
              </button>
            </div>
          </section>
        </>
      )}

      {/* ============================ done ============================ */}
      {step === "done" && result && (
        <section className="space-y-4 rounded-lg border-2 border-green-600 bg-white p-5">
          <Notice kind="success">Merge #{result.log_id} is complete.</Notice>
          <ul className="list-disc space-y-1 pl-5 text-sm">
            <li>{result.products_deleted} duplicate products deleted</li>
            <li>{result.ebay_listings_now_on_part} eBay item numbers are now linked to the kept part</li>
            <li>{result.fitment_vehicles} vehicles fitted</li>
            <li>Quantity on hand: {result.quantity_on_hand}</li>
          </ul>
          <p className="text-sm text-gray-700">
            The kept part still has the title of the product you kept. If it names one machine, open it and retitle it so it describes the whole part.
          </p>
          <div className="flex flex-wrap gap-3">
            <Link href={`/admin/products/${result.survivor_id}`} className={btnCls}>
              Open the kept product
            </Link>
            <button className={btn2Cls} onClick={backToSearch}>
              Back to search
            </button>
          </div>
        </section>
      )}
    </div>
  );
}
