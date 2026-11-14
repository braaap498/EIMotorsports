"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Notice, btnCls, btn2Cls, dangerCls, inputCls } from "../../../components/admin/ui";
import DescriptionEditor from "../../../components/admin/DescriptionEditor";
import CompatEditor, { type CompatRow } from "../../../components/admin/CompatEditor";

type Spec = { name: string; values: string[] };
type Copy = {
  id: number;
  store: string;
  source_item_id: string;
  product_id: number | null;
  status: string;
  new_item_id: string | null;
  title: string;
  description: string;
  price: string;
  sku: string;
  photos: string[];
  original_photos: string[];
  item_specifics: Spec[];
  dropship?: boolean;
  currency: string;
  category_name: string;
  compat_count: number;
  condition_note: string;
  category_id: string;
  compat_notes: string[];
  needs_store: boolean;
  model_item_id: string;
  compat: CompatRow[];
  compat_edited: boolean;
  terms: string[]; // plain-language shipping / handling / returns lines
  terms_problem: string;
};
type StoreChoice = { store: string; seller: string | null; live: boolean };
type Hint = { name: string; value: string };
type CatHit = { category_id: string; name: string; path: string; tree: string; motors: boolean };
type Preview = {
  ok: boolean;
  errors: { code: string; message: string }[];
  warnings: { code: string; message: string }[];
  fees: { name: string; amount: string }[];
  quantity: number;
  quantity_own?: number;
  quantity_distributor?: number;
  distributor_counted?: boolean;
  distributor_note?: string;
  duplicates: string[];
  fingerprint: string | null;
  live_pushes_enabled: boolean;
};

const squash = (h: string) => h.replace(/\s+/g, "");

function CopyEditor({ id }: { id: number }) {
  const [copy, setCopy] = useState<Copy | null>(null);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [price, setPrice] = useState("");
  const [sku, setSku] = useState("");
  const [photos, setPhotos] = useState<string[]>([]);
  const [specs, setSpecs] = useState<{ name: string; value: string }[]>([]);
  const [compat, setCompat] = useState<CompatRow[]>([]);
  const [compatTouched, setCompatTouched] = useState(false); // only an edit made here is sent, so choosing a store can still rebuild the list
  const [dirty, setDirty] = useState(false);
  const [dropship, setDropship] = useState(false);
  const [pv, setPv] = useState<Preview | null>(null);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const [stores, setStores] = useState<StoreChoice[]>([]);
  const [pickStore, setPickStore] = useState("");
  const [modelItem, setModelItem] = useState("");
  const [hints, setHints] = useState<Hint[]>([]);
  const [catQ, setCatQ] = useState("");
  const [catHits, setCatHits] = useState<CatHit[] | null>(null);

  function fill(c: Copy) {
    setCopy(c);
    setTitle(c.title);
    setDescription(c.description);
    setPrice(c.price);
    setSku(c.sku);
    setPhotos(c.photos);
    setSpecs(c.item_specifics.map((s) => ({ name: s.name, value: s.values.join("; ") })));
    setCompat(c.compat ?? []);
    setCompatTouched(false);
    setDropship(!!c.dropship);
    setDirty(false);
  }

  useEffect(() => {
    let cancelled = false;
    api<Copy>(`/ebay-copy/${id}`)
      .then((c) => {
        if (!cancelled) fill(c);
      })
      .catch((e) => {
        if (!cancelled) setMsg({ kind: "error", text: errMsg(e) });
      });
    return () => {
      cancelled = true;
    };
  }, [id]);

  // A draft made from a product has no store yet: load the connected stores so one can be chosen.
  useEffect(() => {
    if (!copy || copy.source_item_id) return;
    api<{ stores: StoreChoice[] }>("/ebay-draft/stores")
      .then((r) => {
        setStores(r.stores);
        setPickStore((cur) => cur || copy.store || (r.stores[0]?.store ?? ""));
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [copy?.id]);

  async function chooseStore() {
    if (!pickStore) return;
    if (dirty && !(await save())) return;
    setBusy("store");
    setMsg(null);
    setPv(null);
    try {
      const r = await api<{ store: string; model_item_id: string; category_name: string; hint_specifics: Hint[]; compat_note?: string; category_item_id?: string; category_store?: string; brand?: string }>(`/ebay-draft/${id}/store`, {
        method: "POST",
        body: { store: pickStore, ...(modelItem.trim() ? { model_item_id: modelItem.trim() } : {}) },
      });
      fill(await api<Copy>(`/ebay-copy/${id}`));
      setHints(r.hint_specifics);
      const brandNote = r.brand ? ` Brand: ${r.brand}.` : " No brand was found for this product, so add a Brand item specific by hand (eBay requires it).";
      setMsg({
        kind: r.brand ? "success" : "error",
        text: (r.category_item_id
          ? `Set up for ${r.store}: shipping and policies from eBay item ${r.model_item_id}, category${r.category_name ? ` ${r.category_name}` : ""} from item ${r.category_item_id} (${r.category_store}). ${r.compat_note ? r.compat_note + " " : ""}Nothing has gone to eBay.`
          : `Set up for ${r.store}, using eBay item ${r.model_item_id} as the model${r.category_name ? ` (${r.category_name})` : ""}. ${r.compat_note ? r.compat_note + " " : ""}Nothing has gone to eBay.`) + brandNote,
      });
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  async function searchCats() {
    const q = catQ.trim();
    if (q.length < 2) return;
    setBusy("catsearch");
    setMsg(null);
    try {
      const r = await api<{ categories: CatHit[] }>(`/ebay-draft/categories?q=${encodeURIComponent(q)}`);
      setCatHits(r.categories);
    } catch (e) {
      setCatHits(null);
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  async function pickCat(h: CatHit) {
    if (dirty && !(await save())) return;
    setBusy("catpick");
    setMsg(null);
    setPv(null);
    try {
      const r = await api<{ compat_note?: string }>(`/ebay-draft/${id}/category`, {
        method: "POST",
        body: { category_id: h.category_id, category_name: h.name, tree: h.tree },
      });
      fill(await api<Copy>(`/ebay-copy/${id}`));
      setCatHits(null);
      setCatQ("");
      setMsg({ kind: "success", text: `Category set to ${h.path} (${h.category_id}). ${r.compat_note ? r.compat_note + " " : ""}Nothing has gone to eBay.` });
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  async function rebuildCompat(mode: "add" | "replace") {
    if (dirty && !(await save())) return;
    if (mode === "replace" && !window.confirm("Replace the whole table with one made from the product's fitment? Rows you added by hand are lost.")) return;
    setBusy("compat");
    setMsg(null);
    try {
      const r = await api<{ rows: number; added: number; notes: string[] }>(`/ebay-draft/${id}/rebuild-compat`, { method: "POST", body: { mode } });
      fill(await api<Copy>(`/ebay-copy/${id}`));
      setMsg({ kind: "success", text: `${r.added} vehicle row${r.added === 1 ? "" : "s"} added from the product's fitment; the table now has ${r.rows}.${r.notes.length ? ` ${r.notes.length} note${r.notes.length === 1 ? "" : "s"} about what eBay does not list are below.` : ""}` });
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  function touch() {
    setDirty(true);
    setPv(null); // an edit makes any preview out of date
  }

  async function save(): Promise<boolean> {
    setBusy("save");
    setMsg(null);
    try {
      const c = await api<Copy>(`/ebay-copy/${id}`, {
        method: "PUT",
        body: {
          title,
          description,
          price,
          photos,
          item_specifics: specs
            .filter((s) => s.name.trim() || s.value.trim())
            .map((s) => ({ name: s.name, values: s.value.split(";").map((v) => v.trim()).filter(Boolean) })),
          dropship,
          ...(compatTouched ? { compat } : {}),
        },
      });
      fill(c);
      setMsg({ kind: "success", text: "Draft saved on the website. Nothing has gone to eBay." });
      return true;
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
      return false;
    } finally {
      setBusy("");
    }
  }

  async function check() {
    if (dirty && !(await save())) return;
    setBusy("check");
    setMsg(null);
    setPv(null);
    try {
      setPv(await api<Preview>(`/ebay-copy/${id}/preview`, { method: "POST" }));
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  async function listIt() {
    if (!pv?.fingerprint || !copy) return;
    if (!window.confirm(`List this as a NEW listing on ${copy.store}?\n\nQuantity ${pv.quantity}, price ${price}.\n\nIt goes live on eBay now.`)) return;
    setBusy("list");
    setMsg(null);
    try {
      const r = await api<{ item_id: string; warnings: { message: string }[]; notes: string[] }>(`/ebay-copy/${id}/list`, {
        method: "POST",
        body: { fingerprint: pv.fingerprint },
      });
      setMsg({
        kind: "success",
        text: `Live on eBay as item ${r.item_id}. ${r.notes.join(" ")}${r.warnings.length ? " eBay warnings: " + r.warnings.map((w) => w.message).join("; ") : ""}`,
      });
      setCopy((c) => (c ? { ...c, status: "listed", new_item_id: r.item_id } : c));
      setPv(null);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  async function discard() {
    if (!window.confirm("Throw this draft away? The original listing is not touched.")) return;
    try {
      await api(`/ebay-copy/${id}`, { method: "DELETE" });
      window.location.href = "/admin/ebay-listings";
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    }
  }

  if (!copy) return <div className="space-y-4">{msg ? <Notice kind={msg.kind}>{msg.text}</Notice> : <p className="text-sm text-gray-500">Loading…</p>}</div>;

  const listed = copy.status === "listed";
  const off = listed || busy !== "";
  const fromProduct = !copy.source_item_id;
  const notReady = fromProduct && (!copy.store || !copy.category_id);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">{fromProduct ? "New listing from a product" : "Sell similar"}</h1>
        <div className="flex gap-2">
          {fromProduct && <Link href="/admin/ebay-copy" className={btn2Cls}>Other drafts</Link>}
          <Link href="/admin/ebay-listings" className={btn2Cls}>Back to Listings</Link>
        </div>
      </div>
      <p className="text-sm text-gray-600">
        {fromProduct ? `New listing for product ${copy.product_id}` : `Copy of eBay item ${copy.source_item_id} in ${copy.store}`}
        {copy.category_name ? ` · ${copy.category_name}` : ""}
        {copy.compat_count ? ` · ${copy.compat_count} compatible vehicles` : ""}. This is a draft on the website only.
        When it is listed, the new item number is linked to the same product (id {copy.product_id}).
      </p>
      {fromProduct && !listed && (
        <div className="space-y-3 rounded border border-gray-200 bg-white p-4 text-sm">
          <h2 className="font-semibold">Where to list it</h2>
          <p className="text-xs text-gray-500">
            Choose the store. The category, shipping and return rules (business policies if the store uses them, otherwise the shipping services and return terms written on its older listings), location and handling time are taken from a live listing in that
            store that is as close to this part as possible (same website category first). To use a particular listing as the model, type its eBay item number.
          </p>
          <div className="flex flex-wrap items-end gap-3">
            <label className="block">
              <span className="mb-1 block font-semibold">Store</span>
              <select className={inputCls} value={pickStore} disabled={off} onChange={(e) => setPickStore(e.target.value)}>
                {stores.length === 0 && <option value="">No connected stores</option>}
                {stores.map((s) => (
                  <option key={s.store} value={s.store}>{s.store}{s.live ? "" : " (live pushes off)"}</option>
                ))}
              </select>
            </label>
            <label className="block">
              <span className="mb-1 block font-semibold">Take category from item # (any store, optional)</span>
              <input className={inputCls} value={modelItem} disabled={off} placeholder="e.g. 177464505625" onChange={(e) => setModelItem(e.target.value)} />
            </label>
            <button type="button" className={btnCls} disabled={off || !pickStore} onClick={() => void chooseStore()}>
              {busy === "store" ? "Reading eBay…" : copy.store ? "Use this store" : "Choose store"}
            </button>
          </div>
          {copy.store ? (
            <p className="text-xs text-gray-600">
              Store: <strong>{copy.store}</strong>
              {copy.category_name ? ` · category ${copy.category_name} (${copy.category_id})` : ""}
              {copy.model_item_id ? ` · model listing ${copy.model_item_id}` : ""}
            </p>
          ) : (
            <p className="text-xs text-amber-700">No store chosen yet, so this cannot be checked or listed.</p>
          )}
          {copy.store && (
            <div className="space-y-2 border-t border-gray-100 pt-3">
              <label className="block">
                <span className="mb-1 block font-semibold">Or search eBay for the category</span>
                <span className="flex flex-wrap items-center gap-2">
                  <input
                    className={`${inputCls} max-w-sm`}
                    value={catQ}
                    disabled={off}
                    placeholder="e.g. fork seals"
                    onChange={(e) => setCatQ(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        void searchCats();
                      }
                    }}
                  />
                  <button type="button" className={btn2Cls} disabled={off || catQ.trim().length < 2} onClick={() => void searchCats()}>
                    {busy === "catsearch" ? "Searching…" : "Search categories"}
                  </button>
                </span>
              </label>
              {catHits && catHits.length === 0 && <p className="text-xs text-gray-600">eBay suggested no categories for that. Try other words.</p>}
              {catHits && catHits.length > 0 && (
                <ul className="max-h-64 divide-y divide-gray-100 overflow-auto rounded border border-gray-200">
                  {catHits.map((h) => (
                    <li key={`${h.tree}-${h.category_id}`}>
                      <button type="button" className="w-full px-3 py-2 text-left text-xs hover:bg-gray-50 disabled:opacity-50" disabled={off} onClick={() => void pickCat(h)}>
                        <span className="font-medium">{h.path}</span>
                        <span className="ml-2 text-gray-500">{h.category_id} · {h.motors ? "eBay Motors" : "eBay US"}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              <p className="text-xs text-gray-500">Picking one keeps the store&rsquo;s shipping and return settings, sets the right eBay site for the category, and makes the compatible-vehicles table again.</p>
            </div>
          )}
        </div>
      )}
      {(copy.compat_notes ?? []).length > 0 && (
        <details className="rounded border border-amber-200 bg-amber-50 p-3 text-sm">
          <summary className="cursor-pointer font-medium">
            Compatibility: {copy.compat_count} vehicle rows converted to eBay&rsquo;s list; {(copy.compat_notes ?? []).length} note{(copy.compat_notes ?? []).length === 1 ? "" : "s"}
          </summary>
          <ul className="mt-2 list-disc space-y-1 pl-5 text-xs">
            {(copy.compat_notes ?? []).map((n, i) => <li key={i}>{n}</li>)}
          </ul>
        </details>
      )}
      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}
      {listed && (
        <Notice kind="success">
          Listed as eBay item {copy.new_item_id}.{" "}
          <Link className="underline" href={`/admin/ebay-edit?store=${encodeURIComponent(copy.store)}&item=${copy.new_item_id}`}>Open it</Link>
        </Notice>
      )}

      <div className="space-y-4 rounded border border-gray-200 bg-white p-4 text-sm">
        <label className="block">
          <span className="mb-1 block font-semibold">Title ({title.length}/80)</span>
          <input className={`${inputCls} w-full`} value={title} disabled={off} maxLength={80} onChange={(e) => { setTitle(e.target.value); touch(); }} />
        </label>
        <div className="flex flex-wrap gap-4">
          <label className="block">
            <span className="mb-1 block font-semibold">Price ({copy.currency})</span>
            <input className={inputCls} value={price} disabled={off} onChange={(e) => { setPrice(e.target.value); touch(); }} />
          </label>
          <label className="block">
            <span className="mb-1 block font-semibold">Custom label (SKU), from the product</span>
            <input className={`${inputCls} bg-gray-100 text-gray-600`} value={sku} readOnly tabIndex={-1} title="Read only. The custom label comes from the product." />
          </label>
        </div>
        <label className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            className="mt-1"
            checked={dropship}
            disabled={off}
            onChange={(e) => {
              setDropship(e.target.checked);
              setDirty(true);
              setPv(null);
            }}
          />
          <span>
            <span className="font-medium">Drop ship from the distributor</span>
            <span className="block text-xs text-gray-500">
              The quantity is the distributor&rsquo;s stock (Indiana + Nevada) plus whatever we have on our shelf, and stays that way after it is
              listed. Untick it and the quantity is our own stock only.
            </span>
          </span>
        </label>
        <p className="text-xs text-gray-500">
          Quantity is not typed here: it comes from stock. eBay may refuse a listing that is identical to a live one in the same store; change
          the title if it does.
        </p>
      </div>

      {(copy.terms ?? []).length > 0 || copy.terms_problem ? (
        <div className="space-y-2 rounded border border-gray-200 bg-white p-4 text-sm">
          <h2 className="font-semibold">Shipping, handling and returns</h2>
          {copy.terms_problem && <Notice kind="error">{copy.terms_problem}</Notice>}
          <ul className="list-disc space-y-0.5 pl-5 text-xs text-gray-700">
            {(copy.terms ?? []).map((t, i) => <li key={i}>{t}</li>)}
          </ul>
          <p className="text-xs text-gray-500">Taken from the model listing, the way the older listings carry them. Change them on eBay or choose a different model listing.</p>
        </div>
      ) : null}

      <div className="space-y-2 rounded border border-gray-200 bg-white p-4 text-sm">
        <h2 className="font-semibold">Compatible vehicles ({compat.length})</h2>
        <p className="text-xs text-gray-500">
          {copy.compat_edited
            ? "Edited by hand, so choosing a store again will not rebuild it."
            : "Built from the fitment saved on the website, matched to eBay's own vehicle list. Add or remove vehicles freely."}
        </p>
        {fromProduct && !!copy.category_id && (
          <div className="flex flex-wrap items-center gap-2">
            <button type="button" className={btnCls} disabled={off || busy !== ""} onClick={() => void rebuildCompat("add")}>
              {busy === "compat" ? "Working…" : "Add all vehicles from the product's fitment"}
            </button>
            <button type="button" className={btn2Cls} disabled={off || busy !== ""} onClick={() => void rebuildCompat("replace")}>Replace table with fitment</button>
          </div>
        )}
        <CompatEditor
          categoryId={copy.category_id}
          rows={compat}
          disabled={off}
          lockedReason={fromProduct && !copy.category_id ? "Choose a store first so the eBay category is known." : null}
          onChange={(r) => {
            setCompat(r);
            setCompatTouched(true);
            touch();
          }}
        />
      </div>

      <div className="space-y-2 rounded border border-gray-200 bg-white p-4 text-sm">
        <h2 className="font-semibold">Photos ({photos.length})</h2>
        <div className="flex flex-wrap gap-3">
          {photos.map((u, n) => (
            <div key={u} className="w-28 space-y-1">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={u} alt="" className="h-28 w-28 rounded border border-gray-200 object-cover" />
              <div className="flex gap-1 text-xs">
                {n > 0 && (
                  <button type="button" className="text-blue-700 hover:underline" disabled={off} onClick={() => { setPhotos([u, ...photos.filter((x) => x !== u)]); touch(); }}>
                    Make main
                  </button>
                )}
                {photos.length > 1 && (
                  <button type="button" className="text-red-700 hover:underline" disabled={off} onClick={() => { setPhotos(photos.filter((x) => x !== u)); touch(); }}>
                    Remove
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="space-y-2 rounded border border-gray-200 bg-white p-4 text-sm">
        <h2 className="font-semibold">Description</h2>
        <DescriptionEditor
          label=""
          value={description}
          disabled={off}
          onChange={(h) => {
            if (squash(h) === squash(description)) return;
            setDescription(h);
            touch();
          }}
        />
      </div>

      <div className="space-y-2 rounded border border-gray-200 bg-white p-4 text-sm">
        <div className="flex items-center justify-between">
          <h2 className="font-semibold">Item specifics</h2>
          <button type="button" className={btn2Cls} disabled={off} onClick={() => { setSpecs([...specs, { name: "", value: "" }]); touch(); }}>
            Add
          </button>
        </div>
        {specs.map((s, n) => (
          <div key={n} className="flex flex-wrap gap-2">
            <input className={`${inputCls} w-48`} placeholder="Name" value={s.name} disabled={off} onChange={(e) => { setSpecs(specs.map((x, i) => (i === n ? { ...x, name: e.target.value } : x))); touch(); }} />
            <input className={`${inputCls} min-w-[16rem] flex-1`} placeholder="Value (separate several with ;)" value={s.value} disabled={off} onChange={(e) => { setSpecs(specs.map((x, i) => (i === n ? { ...x, value: e.target.value } : x))); touch(); }} />
            <button type="button" className="text-red-700 hover:underline" disabled={off} onClick={() => { setSpecs(specs.filter((_, i) => i !== n)); touch(); }}>
              Remove
            </button>
          </div>
        ))}
      </div>

      {hints.length > 0 && !listed && (
        <div className="space-y-2 rounded border border-gray-200 bg-white p-4 text-sm">
          <h2 className="font-semibold">Item specifics the model listing has</h2>
          <p className="text-xs text-gray-500">These are that other listing&rsquo;s values. Add the ones that are also true for this part and correct the value if it differs.</p>
          {hints.map((h) => (
            <div key={h.name} className="flex flex-wrap items-center gap-2">
              <span className="w-48 font-medium">{h.name}</span>
              <span className="min-w-[10rem] flex-1 text-gray-600">{h.value}</span>
              <button
                type="button"
                className="text-blue-700 hover:underline"
                disabled={off}
                onClick={() => {
                  setSpecs([...specs, { name: h.name, value: h.value }]);
                  setHints(hints.filter((x) => x.name !== h.name));
                  touch();
                }}
              >
                Add
              </button>
            </div>
          ))}
        </div>
      )}

      {pv && (
        <div className="space-y-2 rounded border border-gray-200 bg-white p-4 text-sm">
          <h2 className="font-semibold">What eBay says (nothing was listed)</h2>
          {pv.errors.map((e, i) => <Notice key={i} kind="error">{e.message} (eBay {e.code})</Notice>)}
          {pv.warnings.map((e, i) => <Notice key={i}>{e.message} (eBay {e.code})</Notice>)}
          {pv.ok && (
            <p>
              eBay accepts this listing. Quantity {pv.quantity}{pv.quantity_own !== undefined ? ` (${pv.quantity_own} on our shelf${pv.distributor_counted ? ` + ${pv.quantity_distributor ?? 0} at the distributor` : ", distributor not counted"})` : ""}.
              {pv.fees.length > 0 ? ` Fees: ${pv.fees.map((f) => `${f.name} ${f.amount}`).join(", ")}.` : " No listing fee shown."}
            </p>
          )}
          {pv.distributor_note && <Notice>{pv.distributor_note}</Notice>}
          {pv.duplicates.length > 0 && (
            <Notice kind="error">
              This store already has {pv.duplicates.length === 1 ? "a listing" : "listings"} with exactly this title ({pv.duplicates.join(", ")}). eBay may
              refuse an identical duplicate. Change the title or photos if it does.
            </Notice>
          )}
          {pv.ok && !pv.live_pushes_enabled && (
            <Notice kind="error">Live pushes are OFF for {copy.store}, so it cannot be listed. Turn them on in Store API → Stores.</Notice>
          )}
        </div>
      )}

      {!listed && (
        <div className="flex flex-wrap items-center gap-3">
          <button type="button" className={btn2Cls} disabled={off || !dirty} onClick={() => void save()}>
            {busy === "save" ? "Saving…" : "Save draft"}
          </button>
          <button type="button" className={btn2Cls} disabled={off || notReady} onClick={() => void check()}>
            {busy === "check" ? "Asking eBay…" : "Check with eBay"}
          </button>
          <button type="button" className={btnCls} disabled={off || !pv?.ok || !pv.fingerprint || !pv.live_pushes_enabled || dirty} onClick={() => void listIt()}>
            {busy === "list" ? "Listing…" : "List on eBay"}
          </button>
          <button
            type="button"
            className={`${dangerCls} ml-auto`}
            disabled={off}
            onClick={() => void discard()}
          >
            Discard draft
          </button>
        </div>
      )}
    </div>
  );
}

type FoundProduct = { id: number; title: string; sku: string | null; custom_label: string | null; price: number | null; listings: number };
type Existing = { store: string; status: string | null; item_id: string; title: string };
type Checked = {
  product: { id: number; title: string; sku: string | null; custom_label: string | null; price: number | null };
  listings: Existing[];
  drafts: { id: number; store: string; title: string }[];
};
type DraftRow = { id: number; store: string; source_item_id: string; title: string };

const go = (id: number) => {
  window.location.href = `/admin/ebay-copy?id=${id}`;
};

/* Start here: pick a product. If it already has eBay listings they are listed and Sell similar is used; if not, a draft is
   made from the product itself and the store is chosen afterwards. */
function NewFromProduct() {
  const [q, setQ] = useState("");
  const [found, setFound] = useState<FoundProduct[] | null>(null);
  const [checked, setChecked] = useState<Checked | null>(null);
  const [drafts, setDrafts] = useState<DraftRow[]>([]);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);

  useEffect(() => {
    api<{ drafts: DraftRow[] }>("/ebay-copy")
      .then((r) => setDrafts(r.drafts))
      .catch(() => {});
  }, []);

  async function search() {
    if (q.trim().length < 2) return;
    setBusy("search");
    setMsg(null);
    setChecked(null);
    try {
      setFound((await api<{ products: FoundProduct[] }>(`/ebay-draft/products?q=${encodeURIComponent(q.trim())}`)).products);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  async function pick(id: number) {
    setBusy("check");
    setMsg(null);
    try {
      setChecked(await api<Checked>(`/ebay-draft/check/${id}`));
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  async function sellSimilar(store: string, item: string) {
    setBusy("similar" + item);
    setMsg(null);
    try {
      go((await api<{ id: number }>("/ebay-copy/create", { method: "POST", body: { store, item_id: item } })).id);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
      setBusy("");
    }
  }

  async function makeDraft(productId: number) {
    setBusy("create");
    setMsg(null);
    try {
      go((await api<{ id: number }>("/ebay-draft/create", { method: "POST", body: { product_id: productId } })).id);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
      setBusy("");
    }
  }

  return (
    <div className="space-y-5">
      <h1 className="text-2xl font-semibold">New eBay listing</h1>
      <p className="max-w-3xl text-sm text-gray-600">
        Pick a product. If it has no eBay listing yet, a draft is made from the product (title, description, price, photos, item specifics and a
        compatibility table converted from our fitment), and you then choose which store to list it in. If it already has listings, they are shown here and
        you use Sell similar from one of them. Nothing goes to eBay until you check the draft and list it.
      </p>
      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}

      <div className="space-y-3 rounded border border-gray-200 bg-white p-4 text-sm">
        <div className="flex flex-wrap items-end gap-3">
          <label className="block">
            <span className="mb-1 block font-semibold">Find a product (title, EI number, label, part number or id)</span>
            <input className={`${inputCls} w-96 max-w-full`} value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") void search(); }} />
          </label>
          <button type="button" className={btnCls} disabled={busy !== "" || q.trim().length < 2} onClick={() => void search()}>
            {busy === "search" ? "Searching…" : "Search"}
          </button>
        </div>
        {found && found.length === 0 && <p className="text-gray-500">No product matched.</p>}
        {found && found.length > 0 && (
          <ul className="divide-y divide-gray-100">
            {found.map((f) => (
              <li key={f.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                <span>
                  <span className="font-medium">{f.title}</span>{" "}
                  <span className="text-xs text-gray-500">
                    {f.sku ?? ""}{f.custom_label ? ` · label ${f.custom_label}` : ""} · id {f.id}{f.price !== null ? ` · $${f.price.toFixed(2)}` : ""}
                  </span>
                </span>
                <span className="flex items-center gap-3">
                  <span className={`text-xs ${f.listings ? "text-amber-700" : "text-gray-500"}`}>
                    {f.listings ? `${f.listings} eBay listing${f.listings === 1 ? "" : "s"}` : "no eBay listing"}
                  </span>
                  <button type="button" className={btn2Cls} disabled={busy !== ""} onClick={() => void pick(f.id)}>Choose</button>
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>

      {checked && (
        <div className="space-y-3 rounded border border-gray-200 bg-white p-4 text-sm">
          <h2 className="font-semibold">
            {checked.product.title} <span className="text-xs font-normal text-gray-500">id {checked.product.id}</span>
          </h2>
          {checked.drafts.length > 0 && (
            <Notice>
              This product already has {checked.drafts.length === 1 ? "a draft" : "drafts"} in progress:{" "}
              {checked.drafts.map((d, i) => (
                <span key={d.id}>{i > 0 ? ", " : ""}<Link className="underline" href={`/admin/ebay-copy?id=${d.id}`}>#{d.id}{d.store ? ` (${d.store})` : ""}</Link></span>
              ))}
              .
            </Notice>
          )}
          {checked.listings.length > 0 ? (
            <>
              <p>
                This product already has {checked.listings.length} eBay listing{checked.listings.length === 1 ? "" : "s"}, so use <strong>Sell similar</strong> from one of them. It
                copies that listing, including its category and compatibility table.
              </p>
              <ul className="divide-y divide-gray-100">
                {checked.listings.map((l) => (
                  <li key={l.item_id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                    <span>
                      <span className="font-medium">{l.store}</span> · item {l.item_id} · {l.status ?? "status unknown"}
                      <span className="block text-xs text-gray-500">{l.title}</span>
                    </span>
                    <button type="button" className={btn2Cls} disabled={busy !== "" || (l.status ?? "").toLowerCase() !== "active"} onClick={() => void sellSimilar(l.store, l.item_id)}
                      title={(l.status ?? "").toLowerCase() !== "active" ? "Only an active listing can be copied" : ""}>
                      {busy === "similar" + l.item_id ? "Copying…" : "Sell similar"}
                    </button>
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <div className="flex flex-wrap items-center gap-3">
              <p>This product has no eBay listing yet.</p>
              <button type="button" className={btnCls} disabled={busy !== ""} onClick={() => void makeDraft(checked.product.id)}>
                {busy === "create" ? "Making the draft…" : "Make a draft from this product"}
              </button>
            </div>
          )}
        </div>
      )}

      {drafts.length > 0 && (
        <div className="space-y-2 rounded border border-gray-200 bg-white p-4 text-sm">
          <h2 className="font-semibold">Drafts in progress</h2>
          <ul className="divide-y divide-gray-100">
            {drafts.map((d) => (
              <li key={d.id} className="py-2">
                <Link className="text-blue-700 hover:underline" href={`/admin/ebay-copy?id=${d.id}`}>{d.title || `Draft ${d.id}`}</Link>
                <span className="ml-2 text-xs text-gray-500">{d.source_item_id ? `copy of ${d.source_item_id}` : "from a product"} · {d.store || "no store yet"}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function Inner() {
  const p = useSearchParams();
  const id = Number(p.get("id"));
  if (!id) return <NewFromProduct />;
  return <CopyEditor key={id} id={id} />;
}

export default function EbayCopyPage() {
  return (
    <Suspense fallback={null}>
      <Inner />
    </Suspense>
  );
}
