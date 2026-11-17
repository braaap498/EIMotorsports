"use client";

import { useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Notice, btnCls, btn2Cls, inputCls } from "./ui";
import FitmentReview from "./FitmentReview";

/* TEST pathway on the New listing page: look a part up on eBay and see what eBay will tell us about a listing that is not ours
   (item specifics and the compatible-vehicle table). Read only: nothing is saved here and nothing is sent to eBay except reads. */

type Hit = { item_id: string; title: string; price: string | null; image: string | null; seller: string | null; condition: string | null; url: string | null };
type Aspect = { name: string; value: string };
type ItemInfo = {
  item_id: string;
  title: string | null;
  seller: string | null;
  condition: string | null;
  price: string | null;
  category_id: string | null;
  category_path: string | null;
  epid: string | null;
  aspects: Aspect[];
  product_aspects: { name: string; values: string[] }[];
  keys_returned: string[];
  compat_fields: string[];
  url: string | null;
};
type Probe = {
  ok: boolean;
  ack: string;
  errors: { code: string; severity: string; message: string }[];
  seller?: string;
  belongs_to_store?: boolean;
  title?: string;
  category_id?: string;
  category_name?: string;
  specifics: { name: string; values: string[] }[];
  compat_declared_count?: number | null;
  compat_count: number;
  compat: { values: Record<string, string>; notes: string }[];
  compat_truncated?: boolean;
};
type StoreChoice = { store: string; seller: string | null; live: boolean };
type FoundProduct = { id: number; title: string; sku: string | null; custom_label: string | null; price: number | null; listings: number };
type CatProduct = { epid: string; title: string; image: string | null; brand: string | null; mpn: string[] };
type CatCompat = {
  rows: { values: Record<string, string>; notes: string }[];
  count: number;
  truncated: boolean;
  declared_entries: number;
  response_keys: string[];
  paging: Record<string, unknown>;
};
type Made = { id: number; store_done: boolean; compat_rows: number; compat_from_listing: number; specifics_added: string[]; warnings: string[] };

export default function EbayLookup() {
  const [q, setQ] = useState("");
  const [gtin, setGtin] = useState("");
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [total, setTotal] = useState(0);
  const [itemNo, setItemNo] = useState("");
  const [info, setInfo] = useState<ItemInfo | null>(null);
  const [probe, setProbe] = useState<Probe | null>(null);
  const [stores, setStores] = useState<StoreChoice[]>([]);
  const [store, setStore] = useState("");
  const [busy, setBusy] = useState("");
  const [pq, setPq] = useState("");
  const [found, setFound] = useState<FoundProduct[] | null>(null);
  const [product, setProduct] = useState<FoundProduct | null>(null);
  const [made, setMade] = useState<Made | null>(null);
  const [catProducts, setCatProducts] = useState<CatProduct[] | null>(null);
  const [catEpid, setCatEpid] = useState<string | null>(null);
  const [catCompat, setCatCompat] = useState<CatCompat | null>(null);
  const [catMsg, setCatMsg] = useState<string>("");
  const [catBusy, setCatBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);

  useEffect(() => {
    api<{ stores: StoreChoice[] }>("/ebay-draft/stores")
      .then((r) => {
        setStores(r.stores);
        if (r.stores.length) setStore(r.stores[0].store);
      })
      .catch(() => {});
  }, []);

  async function search() {
    setBusy("search");
    setMsg(null);
    setHits(null);
    try {
      const r = await api<{ total: number; items: Hit[] }>(`/ebay-lookup/search?q=${encodeURIComponent(q.trim())}&gtin=${encodeURIComponent(gtin.trim())}`);
      setHits(r.items);
      setTotal(r.total);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  async function read(id: string) {
    const n = id.trim();
    if (!/^\d{9,15}$/.test(n)) {
      setMsg({ kind: "error", text: "That does not look like an eBay item number (9 to 15 digits)." });
      return;
    }
    setItemNo(n);
    setBusy("item");
    setMsg(null);
    setInfo(null);
    setProbe(null);
    setCatProducts(null);
    setCatEpid(null);
    setCatCompat(null);
    setCatMsg("");
    try {
      const got = await api<ItemInfo>(`/ebay-lookup/item/${n}`);
      setInfo(got);
      void lookCatalog(got); // the listing's catalog product, if it has one or can be found from its part number / UPC
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
      setBusy("");
      return;
    }
    setBusy("");
    if (store) await runProbe(n); // reading a listing also asks for its compatibility table
  }

  async function runProbe(id?: string) {
    setBusy("probe");
    setMsg(null);
    setProbe(null);
    try {
      setProbe(await api<Probe>("/ebay-lookup/compat-probe", { method: "POST", body: { item_id: id ?? itemNo, store } }));
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  async function findProducts() {
    if (pq.trim().length < 2) return;
    setBusy("find");
    setMsg(null);
    try {
      setFound((await api<{ products: FoundProduct[] }>(`/ebay-draft/products?q=${encodeURIComponent(pq.trim())}`)).products);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  const aspectOf = (i: ItemInfo, ...names: string[]) => {
    const all = [...i.aspects.map((a) => [a.name, a.value] as const), ...i.product_aspects.map((a) => [a.name, a.values[0] ?? ""] as const)];
    for (const n of names) {
      const hit = all.find(([k, v]) => k.toLowerCase() === n && v);
      if (hit) return hit[1];
    }
    return "";
  };

  async function catalogCompat(epid: string) {
    setCatBusy(true);
    setCatMsg("");
    setCatCompat(null);
    setCatEpid(epid);
    try {
      setCatCompat(await api<CatCompat>("/ebay-lookup/catalog/compat", { method: "POST", body: { epid, store } }));
    } catch (e) {
      setCatMsg(errMsg(e));
    } finally {
      setCatBusy(false);
    }
  }

  async function lookCatalog(i: ItemInfo) {
    setCatBusy(true);
    setCatMsg("");
    try {
      if (i.epid) {
        setCatProducts([]);
        await catalogCompat(i.epid);
        return;
      }
      const mpn = aspectOf(i, "manufacturer part number", "mpn", "part number");
      const upc = aspectOf(i, "upc", "ean", "gtin");
      if (!mpn && !upc) {
        setCatProducts([]);
        setCatMsg("This listing has no ePID, part number or UPC to look the catalog up with. Search it by words below.");
        return;
      }
      const r = await api<{ products: CatProduct[] }>(`/ebay-lookup/catalog/search?mpn=${encodeURIComponent(mpn)}&gtin=${encodeURIComponent(upc)}`);
      setCatProducts(r.products);
      if (r.products.length === 1) await catalogCompat(r.products[0].epid);
      else if (r.products.length === 0) setCatMsg("eBay's catalog has no product for this part number or UPC.");
    } catch (e) {
      setCatProducts([]);
      setCatMsg(errMsg(e));
    } finally {
      setCatBusy(false);
    }
  }

  async function searchCatalogWords() {
    if (!q.trim()) return;
    setCatBusy(true);
    setCatMsg("");
    try {
      const r = await api<{ products: CatProduct[] }>(`/ebay-lookup/catalog/search?q=${encodeURIComponent(q.trim())}`);
      setCatProducts(r.products);
      if (r.products.length === 0) setCatMsg("eBay's catalog has no product for those words.");
    } catch (e) {
      setCatMsg(errMsg(e));
    } finally {
      setCatBusy(false);
    }
  }

  function startOver() {
    setInfo(null);
    setProbe(null);
    setProduct(null);
    setFound(null);
    setMade(null);
    setMsg(null);
    setItemNo("");
    setPq("");
    setCatProducts(null);
    setCatEpid(null);
    setCatCompat(null);
    setCatMsg("");
  }

  async function makeIt() {
    if (!info || !product || !store) return;
    setBusy("make");
    setMsg(null);
    setMade(null);
    try {
      const r = await api<Made>("/ebay-lookup/make-draft", { method: "POST", body: { item_id: info.item_id, product_id: product.id, store, epid: catCompat && catCompat.count > 0 ? catEpid : null } });
      if (r.warnings.length === 0 && r.store_done) {
        window.location.href = `/admin/ebay-copy?id=${r.id}`;
        return;
      }
      setMade(r);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="space-y-5">
      <h1 className="text-2xl font-semibold">Look up a part on eBay</h1>
      <p className="max-w-3xl text-sm text-gray-600">
        Search eBay for the same part, open a listing, and see what eBay lets us read from it: its item specifics and its compatible-vehicle table. Reading a listing saves nothing. Once it is read you can make a draft of one of our products with its
        compatibility table and item specifics filled in; nothing is listed on eBay until you check the draft and list it.
      </p>
      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}

      <div className="space-y-3 rounded border border-gray-200 bg-white p-4 text-sm">
        <div className="flex flex-wrap items-end gap-3">
          <label className="block">
            <span className="mb-1 block font-semibold">Part number or words</span>
            <input className={`${inputCls} w-80 max-w-full`} value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") void search(); }} />
          </label>
          <label className="block">
            <span className="mb-1 block font-semibold">UPC / barcode (optional)</span>
            <input className={`${inputCls} w-48 max-w-full`} value={gtin} onChange={(e) => setGtin(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") void search(); }} />
          </label>
          <button type="button" className={btnCls} disabled={busy !== "" || (q.trim().length < 2 && gtin.trim() === "")} onClick={() => void search()}>
            {busy === "search" ? "Searching…" : "Search eBay"}
          </button>
        </div>
        <div className="flex flex-wrap items-end gap-3 border-t border-gray-100 pt-3">
          <label className="block">
            <span className="mb-1 block font-semibold">Or an eBay item number</span>
            <input className={`${inputCls} w-56 max-w-full`} value={itemNo} onChange={(e) => setItemNo(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") void read(itemNo); }} />
          </label>
          <button type="button" className={btn2Cls} disabled={busy !== "" || itemNo.trim() === ""} onClick={() => void read(itemNo)}>
            {busy === "item" ? "Reading…" : "Read this listing"}
          </button>
        </div>
        {hits && hits.length === 0 && <p className="text-gray-500">eBay returned no listings for that search.</p>}
        {hits && hits.length > 0 && (
          <>
            <p className="text-xs text-gray-500">Showing {hits.length} of about {total.toLocaleString()} listings.</p>
            <ul className="divide-y divide-gray-100">
              {hits.map((h) => (
                <li key={h.item_id} className="flex flex-wrap items-center justify-between gap-3 py-2">
                  <span className="flex items-center gap-3">
                    {h.image && /* eslint-disable-next-line @next/next/no-img-element */ <img src={h.image} alt="" className="h-12 w-12 rounded border border-gray-200 object-cover" />}
                    <span>
                      <span className="font-medium">{h.title}</span>
                      <span className="block text-xs text-gray-500">item {h.item_id} · {h.seller ?? "seller unknown"}{h.price ? ` · ${h.price}` : ""}{h.condition ? ` · ${h.condition}` : ""}</span>
                    </span>
                  </span>
                  <button type="button" className={btn2Cls} disabled={busy !== ""} onClick={() => void read(h.item_id)}>Read this listing</button>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>

      {info && (
        <div className="space-y-4 rounded border border-gray-200 bg-white p-4 text-sm">
          <div>
            <h2 className="font-semibold">{info.title ?? `Item ${info.item_id}`}</h2>
            <p className="text-xs text-gray-500">
              item {info.item_id} · {info.seller ?? "seller unknown"}{info.price ? ` · ${info.price}` : ""}{info.condition ? ` · ${info.condition}` : ""}
              {info.category_path ? ` · ${info.category_path}` : info.category_id ? ` · category ${info.category_id}` : ""}
              {info.epid ? ` · eBay catalog product ${info.epid}` : ""}
            </p>
          </div>

          <div>
            <h3 className="mb-1 font-semibold">Item specifics from the Browse API ({info.aspects.length})</h3>
            {info.aspects.length === 0 ? (
              <p className="text-gray-500">eBay returned no item specifics for this listing.</p>
            ) : (
              <table className="w-full max-w-2xl text-left text-sm">
                <tbody>
                  {info.aspects.map((a, i) => (
                    <tr key={i} className="border-t border-gray-100">
                      <th className="w-56 py-1 pr-3 align-top font-medium text-gray-700">{a.name}</th>
                      <td className="py-1">{a.value}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          {info.product_aspects.length > 0 && (
            <p className="text-xs text-gray-600">
              eBay also matched this listing to a catalog product with {info.product_aspects.length} more specific{info.product_aspects.length === 1 ? "" : "s"}:{" "}
              {info.product_aspects.map((a) => `${a.name}: ${a.values.join(", ")}`).join(" · ")}
            </p>
          )}

          <p className="text-xs text-gray-500">Fields eBay returned: {info.keys_returned.join(", ")}</p>

          <div className="space-y-2 border-t border-gray-100 pt-3">
            <h3 className="font-semibold">Compatibility table</h3>
            <p className="text-gray-600">
              Asked for automatically when a listing is read, through eBay&apos;s Trading API using the store below (any of ours works; it only reads). The store chosen here is also the store the new listing goes to.
            </p>
            {stores.length === 0 ? (
              <p className="text-amber-700">No eBay store is connected, so this check cannot run yet. Connect one under Store API.</p>
            ) : (
              <div className="flex flex-wrap items-end gap-3">
                <label className="block">
                  <span className="mb-1 block text-xs font-semibold">Ask as store</span>
                  <select className={inputCls} value={store} onChange={(e) => setStore(e.target.value)}>
                    {stores.map((s) => (
                      <option key={s.store} value={s.store}>{s.store}</option>
                    ))}
                  </select>
                </label>
                <button type="button" className={btn2Cls} disabled={busy !== "" || !store} onClick={() => void runProbe()}>
                  {busy === "probe" ? "Asking eBay…" : "Ask again"}
                </button>
              </div>
            )}
          </div>
        </div>
      )}

      {probe && (
        <div className="space-y-3 rounded border border-gray-200 bg-white p-4 text-sm">
          <h2 className="font-semibold">What the Trading API said</h2>
          {!probe.ok ? (
            <Notice kind="error">
              eBay refused ({probe.ack || "no answer"}): {probe.errors.map((e) => `${e.message}${e.code ? ` (eBay ${e.code})` : ""}`).join("; ") || "no reason given"}
            </Notice>
          ) : (
            <>
              <p>
                Listing by <strong>{probe.seller}</strong>{probe.belongs_to_store ? " (this is one of our own listings)" : " (not ours)"}.{" "}
                {probe.compat_count > 0 ? (
                  <strong className="text-green-700">eBay returned {probe.compat_count} compatibility row{probe.compat_count === 1 ? "" : "s"}.</strong>
                ) : (
                  <strong className="text-amber-700">
                    eBay returned no compatibility rows{probe.compat_declared_count ? ` (it says the listing has ${probe.compat_declared_count})` : ""}.
                  </strong>
                )}
              </p>
              {probe.errors.length > 0 && (
                <Notice>{probe.errors.map((e) => `${e.message}${e.code ? ` (eBay ${e.code})` : ""}`).join("; ")}</Notice>
              )}
              {probe.specifics.length > 0 && (
                <p className="text-xs text-gray-600">
                  Item specifics from the Trading API ({probe.specifics.length}): {probe.specifics.map((s) => `${s.name}: ${s.values.join(", ")}`).join(" · ")}
                </p>
              )}
              {probe.compat.length > 0 && (
                <div className="max-h-96 overflow-auto rounded border border-gray-200">
                  <table className="w-full text-left text-xs">
                    <thead className="sticky top-0 bg-gray-50">
                      <tr>
                        {Array.from(new Set<string>(probe.compat.flatMap((c) => Object.keys(c.values)))).map((k) => (
                          <th key={k} className="px-2 py-1 font-semibold">{k}</th>
                        ))}
                        <th className="px-2 py-1 font-semibold">Notes</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(() => {
                        const cols = Array.from(new Set<string>(probe.compat.flatMap((c) => Object.keys(c.values))));
                        return probe.compat.map((c, i) => (
                          <tr key={i} className="border-t border-gray-100">
                            {cols.map((k) => (
                              <td key={k} className="px-2 py-1">{c.values[k] ?? ""}</td>
                            ))}
                            <td className="px-2 py-1">{c.notes}</td>
                          </tr>
                        ));
                      })()}
                    </tbody>
                  </table>
                </div>
              )}
              {probe.compat_truncated && <p className="text-xs text-gray-500">Only the first rows are shown.</p>}
            </>
          )}
        </div>
      )}

      {info && (
        <div className="space-y-3 rounded border border-gray-200 bg-white p-4 text-sm">
          <h2 className="font-semibold">eBay&apos;s catalog product for this part</h2>
          <p className="text-gray-600">
            eBay keeps its own compatibility table for catalog products, apart from any seller&apos;s listing. It is looked up from the listing&apos;s catalog product, or from its part number or UPC, and
            it is read only. It can be added to the website fitment below, and to a draft.
          </p>
          {catBusy && <p className="text-gray-500">Asking eBay&apos;s catalog…</p>}
          {catMsg && <Notice kind="info">{catMsg}</Notice>}
          {catProducts && catProducts.length > 0 && (
            <ul className="divide-y divide-gray-100">
              {catProducts.map((c) => (
                <li key={c.epid} className="flex flex-wrap items-center justify-between gap-3 py-2">
                  <span className="flex items-center gap-3">
                    {c.image && /* eslint-disable-next-line @next/next/no-img-element */ <img src={c.image} alt="" className="h-12 w-12 rounded border border-gray-200 object-cover" />}
                    <span>
                      <span className="font-medium">{c.title}</span>
                      <span className="block text-xs text-gray-500">ePID {c.epid}{c.brand ? ` · ${c.brand}` : ""}{c.mpn.length ? ` · MPN ${c.mpn.join(", ")}` : ""}</span>
                    </span>
                  </span>
                  <button type="button" className={btn2Cls} disabled={catBusy} onClick={() => void catalogCompat(c.epid)}>
                    {catEpid === c.epid ? "Read again" : "Read its compatibility table"}
                  </button>
                </li>
              ))}
            </ul>
          )}
          <div className="flex flex-wrap items-end gap-2 border-t border-gray-100 pt-2">
            <span className="text-xs text-gray-500">Wrong or no product? Look it up by the words in the search box at the top:</span>
            <button type="button" className={btn2Cls} disabled={catBusy || q.trim().length < 2} onClick={() => void searchCatalogWords()}>Search the catalog</button>
            <button type="button" className={btn2Cls} disabled={catBusy} onClick={() => void lookCatalog(info)}>Look up from this listing again</button>
          </div>
          {catCompat && (
            <div className="space-y-2">
              <p>
                {catCompat.count > 0 ? (
                  <strong className="text-green-700">eBay&apos;s catalog product {catEpid} has {catCompat.count} compatibility row{catCompat.count === 1 ? "" : "s"}.</strong>
                ) : (
                  <strong className="text-amber-700">eBay returned no compatibility rows for catalog product {catEpid}{catCompat.declared_entries ? ` (it sent ${catCompat.declared_entries} entries that held no vehicle)` : ""}.</strong>
                )}
              </p>
              {Object.keys(catCompat.paging).length > 0 && (
                <p className="text-xs text-gray-500">eBay also sent paging details: {JSON.stringify(catCompat.paging)}. If it says there is more than one page, only the first page was read.</p>
              )}
              {catCompat.count === 0 && <p className="text-xs text-gray-500">Fields in eBay&apos;s answer: {catCompat.response_keys.join(", ") || "none"}.</p>}
              {catCompat.rows.length > 0 && (() => {
                const cols = Array.from(new Set<string>(catCompat.rows.flatMap((c) => Object.keys(c.values))));
                return (
                  <div className="max-h-72 overflow-auto rounded border border-gray-200">
                    <table className="w-full text-left text-xs">
                      <thead className="sticky top-0 bg-gray-50">
                        <tr>{cols.map((k) => <th key={k} className="px-2 py-1 font-semibold">{k}</th>)}<th className="px-2 py-1 font-semibold">Notes</th></tr>
                      </thead>
                      <tbody>
                        {catCompat.rows.map((c, i) => (
                          <tr key={i} className="border-t border-gray-100">
                            {cols.map((k) => <td key={k} className="px-2 py-1">{c.values[k] ?? ""}</td>)}
                            <td className="px-2 py-1">{c.notes}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                );
              })()}
              {catCompat.truncated && <p className="text-xs text-gray-500">Only the first rows are shown here; all of them are used when added.</p>}
            </div>
          )}
        </div>
      )}

      {info && (
        <div className="space-y-4 rounded border-2 border-gray-900 bg-white p-4 text-sm">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 className="font-semibold">Use this listing for one of our products</h2>
              <p className="max-w-3xl text-gray-600">
                Pick the product this listing is for. You can then do either or both of two separate things: add the compatibility table to the product&apos;s website fitment (reviewed by you
                first), and make a draft listing. Doing the first does not require the second, and you can stop after it.
              </p>
            </div>
            <button type="button" className={btn2Cls} disabled={busy !== ""} onClick={startOver}>Done / start over</button>
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <label className="block">
              <span className="mb-1 block font-semibold">Find our product (title, EI number, label, part number or id)</span>
              <input className={`${inputCls} w-96 max-w-full`} value={pq} onChange={(e) => setPq(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") void findProducts(); }} />
            </label>
            <button type="button" className={btn2Cls} disabled={busy !== "" || pq.trim().length < 2} onClick={() => void findProducts()}>
              {busy === "find" ? "Searching…" : "Search"}
            </button>
          </div>
          {found && found.length === 0 && <p className="text-gray-500">No product matched.</p>}
          {found && found.length > 0 && (
            <ul className="divide-y divide-gray-100">
              {found.map((f) => (
                <li key={f.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                  <span>
                    <span className="font-medium">{f.title}</span>{" "}
                    <span className="text-xs text-gray-500">{f.sku ?? ""}{f.custom_label ? ` · label ${f.custom_label}` : ""} · id {f.id}</span>
                  </span>
                  <span className="flex items-center gap-3">
                    <span className={`text-xs ${f.listings ? "text-amber-700" : "text-gray-500"}`}>{f.listings ? `${f.listings} eBay listing${f.listings === 1 ? "" : "s"}` : "no eBay listing"}</span>
                    <button type="button" className={btn2Cls} disabled={busy !== ""} onClick={() => { setProduct(f); setMade(null); }}>
                      {product?.id === f.id ? "Chosen" : "Choose"}
                    </button>
                  </span>
                </li>
              ))}
            </ul>
          )}

          {product && (
            <p className="border-t border-gray-100 pt-3">
              Chosen: <strong>{product.title}</strong> <span className="text-xs text-gray-500">(id {product.id})</span>
            </p>
          )}

          {product && (
            <div className="space-y-2 rounded border border-gray-200 p-3">
              <h3 className="font-semibold">1 · Add this compatibility table to the product&apos;s website fitment</h3>
              <p className="text-gray-600">
                Review first: only years that are new for <strong>{product.title}</strong> are offered, you tick what to add, and a make or model we do not have is shown for you to settle or edit
                before anything is created. Nothing is ever removed.
              </p>
              <FitmentReview key={`${product.id}:${info.item_id}`} product={product} itemId={info.item_id} store={store} epid={catCompat && catCompat.count > 0 ? catEpid : null} />
            </div>
          )}

          {product && (
            <div className="space-y-2 rounded border border-gray-200 p-3">
              <h3 className="font-semibold">2 · Make a listing like this one (optional)</h3>
              <p className="text-gray-600">
                One click makes the draft from our product (title, description, price, photos), takes the category and shipping from a similar live listing in <strong>{store || "the chosen store"}</strong>,
                and adds this listing&apos;s compatibility rows and item specifics that the product does not have. The other seller&apos;s title, description and photos are not copied. This is separate from step 1:
                the draft carries the rows from the listing whether or not you added them to the website fitment, and you can trim them on the draft screen.
              </p>
              <div className="flex flex-wrap items-center gap-3">
                <span>
                  For <strong>{product.title}</strong> in <strong>{store}</strong>.
                </span>
                <button type="button" className={btnCls} disabled={busy !== "" || !store || product.listings > 0} onClick={() => void makeIt()}
                  title={product.listings > 0 ? "This product already has eBay listings: use Sell similar from one of them" : ""}>
                  {busy === "make" ? "Making the draft…" : "Make the draft"}
                </button>
                {product.listings > 0 && <span className="text-xs text-amber-700">This product already has eBay listings, so no new draft from here. Use Sell similar.</span>}
              </div>
            </div>
          )}

          {made && (
            <div className="space-y-2">
              <Notice kind={made.store_done ? "success" : "info"}>
                Draft made with {made.compat_rows} compatibility row{made.compat_rows === 1 ? "" : "s"} ({made.compat_from_listing} added from this listing)
                {made.specifics_added.length ? ` and ${made.specifics_added.length} added item specific${made.specifics_added.length === 1 ? "" : "s"}: ${made.specifics_added.join(", ")}` : ""}.
              </Notice>
              {made.warnings.map((w, i) => (
                <Notice key={i}>{w}</Notice>
              ))}
              <a className={btnCls} href={`/admin/ebay-copy?id=${made.id}`}>Open the draft</a>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
