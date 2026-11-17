"use client";

import { useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Notice, btnCls, btn2Cls, inputCls } from "./ui";

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
    try {
      setInfo(await api<ItemInfo>(`/ebay-lookup/item/${n}`));
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

  async function makeIt() {
    if (!info || !product || !store) return;
    setBusy("make");
    setMsg(null);
    setMade(null);
    try {
      const r = await api<Made>("/ebay-lookup/make-draft", { method: "POST", body: { item_id: info.item_id, product_id: product.id, store } });
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

          <Notice kind={info.compat_fields.length ? "success" : undefined}>
            {info.compat_fields.length
              ? `The Browse answer has compatibility fields: ${info.compat_fields.join(", ")}.`
              : "The Browse answer has no compatibility table. That is expected; use the Trading API check below."}
          </Notice>
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
                        {Array.from(new Set(probe.compat.flatMap((c) => Object.keys(c.values)))).map((k) => (
                          <th key={k} className="px-2 py-1 font-semibold">{k}</th>
                        ))}
                        <th className="px-2 py-1 font-semibold">Notes</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(() => {
                        const cols = Array.from(new Set(probe.compat.flatMap((c) => Object.keys(c.values))));
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
        <div className="space-y-3 rounded border-2 border-gray-900 bg-white p-4 text-sm">
          <h2 className="font-semibold">Make a listing like this one</h2>
          <p className="text-gray-600">
            Pick the product this listing is for. One click makes the draft from our product (title, description, price, photos), takes the category and
            shipping from a similar live listing in <strong>{store || "the chosen store"}</strong>, and adds this listing&apos;s compatibility rows and item specifics that the
            product does not have. The other seller&apos;s title, description and photos are not copied.
          </p>
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
                    <button type="button" className={btn2Cls} disabled={busy !== "" || f.listings > 0} title={f.listings > 0 ? "This product already has eBay listings: use Sell similar from one of them" : ""} onClick={() => { setProduct(f); setMade(null); }}>
                      {product?.id === f.id ? "Chosen" : "Choose"}
                    </button>
                  </span>
                </li>
              ))}
            </ul>
          )}
          {product && (
            <div className="flex flex-wrap items-center gap-3 border-t border-gray-100 pt-3">
              <span>
                For <strong>{product.title}</strong> in <strong>{store}</strong>.
              </span>
              <button type="button" className={btnCls} disabled={busy !== "" || !store} onClick={() => void makeIt()}>
                {busy === "make" ? "Making the draft…" : "Make the draft"}
              </button>
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
