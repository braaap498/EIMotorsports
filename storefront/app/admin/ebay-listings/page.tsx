"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { loadSaved, saveSaved } from "../../../lib/savedState";
import { Badge, Notice, Pager, btnCls, btn2Cls, inputCls } from "../../../components/admin/ui";

/* ------------------------------------------------------------------ types */

type Store = {
  store: string;
  display_name: string | null;
  seller_username: string | null;
  is_active: boolean;
  authorized: boolean;
  authorized_at: string | null;
  authorized_by: string | null;
  refresh_token_expires_at: string | null;
  live_pushes_enabled: boolean;
  linked_count: number;
};
type StoresResp = { main_store: string; stores: Store[] };

type Item = {
  item_id: string;
  title: string;
  price: string | null;
  currency: string | null;
  image_url: string | null;
  url: string;
  linked: {
    product_id: number;
    product_title: string;
    api_type: "trading" | "inventory";
    store: string | null;
    site_price: number | null;
    site_stock: number | null;
  } | null;
};
type ListResp = {
  store: string;
  seller: string;
  known_store: boolean;
  total: number;
  ebay_total: number;
  offset: number;
  limit: number;
  items: Item[];
};

const PAGE_SIZE = 50;

function money(v: string | number | null | undefined, cur?: string | null) {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (Number.isNaN(n)) return String(v);
  return `${cur && cur !== "USD" ? cur + " " : "$"}${n.toFixed(2)}`;
}

/* ------------------------------------------------------------------- page */

export default function EbayLiveListingsPage() {
  const [stores, setStores] = useState<Store[]>([]);
  const [store, setStore] = useState("");
  const [q, setQ] = useState("");
  const [sort, setSort] = useState("newest");
  const [data, setData] = useState<ListResp | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const [showAdd, setShowAdd] = useState(false);
  const [newStore, setNewStore] = useState("");
  const [newName, setNewName] = useState("");
  const [newSeller, setNewSeller] = useState("");
  const [addMsg, setAddMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState("");
  const [connMsg, setConnMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);

  async function loadStores() {
    try {
      const r = await api<StoresResp>("/ebay-listings/stores");
      setStores(r.stores);
    } catch (e) {
      setError(errMsg(e));
    }
  }

  useEffect(() => {
    void loadStores();
  }, []);

  // Coming back from another admin page: put the search and page back and show the same listings.
  const restored = useRef(false);
  useEffect(() => {
    if (restored.current) return;
    restored.current = true;
    const saved = loadSaved<{ store: string; q: string; sort: string; offset: number }>("ebay-listings");
    if (saved) {
      setStore(saved.store);
      setQ(saved.q);
      setSort(saved.sort);
      void load(saved.offset, saved);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function load(offset: number, from?: { store: string; q: string; sort: string }) {
    const useStore = from ? from.store : store;
    const useQ = from ? from.q : q;
    const useSort = from ? from.sort : sort;
    setLoading(true);
    setError("");
    try {
      const params = new URLSearchParams({ q: useQ.trim(), sort: useSort, offset: String(offset), limit: String(PAGE_SIZE) });
      if (useStore.trim()) params.set("store", useStore.trim());
      saveSaved("ebay-listings", { store: useStore, q: useQ, sort: useSort, offset });
      setData(await api<ListResp>(`/ebay-listings/list?${params.toString()}`));
    } catch (e) {
      setData(null);
      setError(errMsg(e));
    } finally {
      setLoading(false);
    }
  }

  async function connect(s: string) {
    setBusy(s);
    setConnMsg(null);
    try {
      const r = await api<{ url: string }>("/ebay-connect/start", { method: "POST", body: { store: s } });
      window.location.href = r.url; // eBay's approval page; it sends the person back to /admin/ebay-connect
    } catch (e) {
      setConnMsg({ kind: "error", text: errMsg(e) });
      setBusy("");
    }
  }

  async function disconnect(s: string) {
    if (!window.confirm(`Disconnect ${s}? Its saved eBay key is deleted and it must be approved again to edit.`)) return;
    setBusy(s);
    setConnMsg(null);
    try {
      await api("/ebay-connect/disconnect", { method: "POST", body: { store: s } });
      setConnMsg({ kind: "success", text: `${s} was disconnected.` });
      await loadStores();
    } catch (e) {
      setConnMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  async function testConn(s: string) {
    setBusy(s);
    setConnMsg(null);
    try {
      const r = await api<{
        ok: boolean;
        ebay_username: string;
        expected_username: string;
        selling_limit: { quantity?: number; amount?: { value?: string } } | null;
        selling_limit_error: string | null;
      }>("/ebay-connect/test", { method: "POST", body: { store: s } });
      const lim = r.selling_limit ? ` Selling limit: ${r.selling_limit.quantity ?? "?"} items / $${r.selling_limit.amount?.value ?? "?"}.` : "";
      setConnMsg({
        kind: r.ok ? "success" : "error",
        text: r.ok
          ? `${s}: connection works, signed in as ${r.ebay_username}.${lim}${r.selling_limit_error ? ` (Limit check failed: ${r.selling_limit_error})` : ""}`
          : `${s}: the key belongs to ${r.ebay_username}, but ${r.expected_username} was expected.`,
      });
    } catch (e) {
      setConnMsg({ kind: "error", text: `${s}: ${errMsg(e)}` });
    } finally {
      setBusy("");
    }
  }

  async function addStore() {
    setAdding(true);
    setAddMsg(null);
    try {
      await api("/ebay-listings/stores", {
        method: "POST",
        body: { store: newStore.trim(), display_name: newName.trim() || null, seller_username: newSeller.trim() || null },
      });
      setAddMsg({ kind: "success", text: `${newStore.trim()} was added.` });
      setNewStore("");
      setNewName("");
      setNewSeller("");
      await loadStores();
    } catch (e) {
      setAddMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setAdding(false);
    }
  }

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-semibold">Live listings</h1>
        <p className="mt-1 max-w-3xl text-sm text-gray-600">
          Shows a store&rsquo;s live eBay listings and which ones are products on the website. This screen only reads;
          nothing here changes eBay. Until a store&rsquo;s owner authorizes the app, eBay&rsquo;s public view is all
          that can be seen, so listings that are out of stock or ended do not appear yet.
        </p>
      </div>

      {error && <Notice kind="error">{error}</Notice>}

      {/* ---- stores */}
      <div className="rounded border border-gray-200 bg-white p-4">
        <div className="flex items-center justify-between gap-3">
          <h2 className="text-sm font-semibold">Stores</h2>
          <button type="button" className={btn2Cls} onClick={() => setShowAdd((v) => !v)}>
            {showAdd ? "Close" : "Add store"}
          </button>
        </div>
        {connMsg && (
          <div className="mt-3">
            <Notice kind={connMsg.kind}>{connMsg.text}</Notice>
          </div>
        )}
        <ul className="mt-3 divide-y divide-gray-100 text-sm">
          {stores.map((s) => (
            <li key={s.store} className="flex flex-wrap items-center gap-2 py-2">
              <button type="button" className="font-medium underline-offset-2 hover:underline" onClick={() => setStore(s.store)}>
                {s.display_name || s.store}
              </button>
              <span className="text-gray-500">{s.store}</span>
              <span className="text-gray-400">· {s.linked_count} linked to products</span>
              <Badge tone={s.authorized ? "green" : "gray"}>{s.authorized ? "Authorized" : "Not authorized yet"}</Badge>
              <Badge tone={s.live_pushes_enabled ? "amber" : "gray"}>
                {s.live_pushes_enabled ? "Live pushes ON" : "Preview only"}
              </Badge>
              {s.authorized && s.refresh_token_expires_at && (
                <span className="text-xs text-gray-500">
                  approval ends {new Date(s.refresh_token_expires_at).toLocaleDateString()}
                </span>
              )}
              <span className="ml-auto flex gap-2">
                <button type="button" className={btn2Cls} disabled={busy === s.store} onClick={() => void connect(s.store)}>
                  {s.authorized ? "Reconnect" : "Connect"}
                </button>
                {s.authorized && (
                  <>
                    <button type="button" className={btn2Cls} disabled={busy === s.store} onClick={() => void testConn(s.store)}>
                      Test
                    </button>
                    <button type="button" className={btn2Cls} disabled={busy === s.store} onClick={() => void disconnect(s.store)}>
                      Disconnect
                    </button>
                  </>
                )}
              </span>
            </li>
          ))}
          {stores.length === 0 && <li className="py-2 text-gray-500">No stores yet.</li>}
        </ul>

        {showAdd && (
          <div className="mt-3 space-y-3 border-t border-gray-100 pt-3">
            <div className="grid gap-3 sm:grid-cols-3">
              <label className="block text-sm font-medium text-gray-700">
                <span className="mb-1 block">Store name</span>
                <input className={inputCls} value={newStore} onChange={(e) => setNewStore(e.target.value)} placeholder="genuine-oem-parts" />
              </label>
              <label className="block text-sm font-medium text-gray-700">
                <span className="mb-1 block">Display name (optional)</span>
                <input className={inputCls} value={newName} onChange={(e) => setNewName(e.target.value)} />
              </label>
              <label className="block text-sm font-medium text-gray-700">
                <span className="mb-1 block">eBay user name (only if different)</span>
                <input className={inputCls} value={newSeller} onChange={(e) => setNewSeller(e.target.value)} />
              </label>
            </div>
            <div className="flex items-center gap-3">
              <button type="button" className={btnCls} disabled={adding || !newStore.trim()} onClick={() => void addStore()}>
                {adding ? "Adding…" : "Add store"}
              </button>
              {addMsg && <span className={`text-sm ${addMsg.kind === "error" ? "text-red-700" : "text-green-700"}`}>{addMsg.text}</span>}
            </div>
          </div>
        )}
      </div>

      {/* ---- search */}
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          void load(0);
        }}
      >
        <label className="block w-64 text-sm font-medium text-gray-700">
          <span className="mb-1 block">eBay store name</span>
          <input
            className={inputCls}
            list="ebay-store-names"
            value={store}
            onChange={(e) => setStore(e.target.value)}
            placeholder="eandimotorsports"
          />
          <datalist id="ebay-store-names">
            {stores.map((s) => (
              <option key={s.store} value={s.store} />
            ))}
          </datalist>
        </label>
        <label className="block w-64 text-sm font-medium text-gray-700">
          <span className="mb-1 block">Search titles (optional)</span>
          <input className={inputCls} value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. Tusk 1.8 bar" />
        </label>
        <label className="block text-sm font-medium text-gray-700">
          <span className="mb-1 block">Order</span>
          <select className={inputCls} value={sort} onChange={(e) => setSort(e.target.value)}>
            <option value="newest">Newest first</option>
            <option value="price_low">Price, low to high</option>
            <option value="price_high">Price, high to low</option>
          </select>
        </label>
        <button type="submit" className={btnCls} disabled={loading}>
          {loading ? "Reading eBay…" : "Show listings"}
        </button>
      </form>
      <p className="-mt-2 text-xs text-gray-500">Leave the store name empty for the main store.</p>

      {/* ---- results */}
      {data && (
        <div className="space-y-3">
          <p className="text-sm text-gray-700">
            Reading <span className="font-medium">{data.seller}</span> on eBay
            {data.ebay_total > data.total ? ` (eBay has ${data.ebay_total} but shows at most ${data.total} per search)` : ""}.
          </p>
          {!data.known_store && (
            <Notice>{data.store} is not in the store list yet, so it can be read but not edited. Use Add store above.</Notice>
          )}
          {data.items.length === 0 ? (
            <Notice>
              eBay returned no listings for {data.seller}
              {q.trim() ? " with that search" : ""}. If this is the main store, leave the store name empty.
            </Notice>
          ) : (
            <div className="overflow-x-auto rounded border border-gray-200 bg-white">
              <table className="w-full text-left text-sm">
                <thead className="bg-gray-50 text-xs uppercase text-gray-500">
                  <tr>
                    <th className="w-16 px-3 py-2"></th>
                    <th className="px-3 py-2">Listing</th>
                    <th className="px-3 py-2">eBay price</th>
                    <th className="px-3 py-2">On the website</th>
                    <th className="px-3 py-2">Site price / stock</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {data.items.map((it) => (
                    <tr key={it.item_id} className="align-top">
                      <td className="px-3 py-2">
                        {it.image_url ? (
                          // eslint-disable-next-line @next/next/no-img-element
                          <img src={it.image_url} alt="" className="h-12 w-12 rounded object-cover" />
                        ) : (
                          <div className="h-12 w-12 rounded bg-gray-100" />
                        )}
                      </td>
                      <td className="px-3 py-2">
                        <div className="max-w-xl">{it.title}</div>
                        <a href={it.url} target="_blank" rel="noreferrer" className="text-xs text-blue-700 hover:underline">
                          {it.item_id}
                        </a>
                      </td>
                      <td className="px-3 py-2 whitespace-nowrap">{money(it.price, it.currency)}</td>
                      <td className="px-3 py-2">
                        {it.linked ? (
                          <div className="space-y-1">
                            <Link href={`/admin/products/${it.linked.product_id}`} className="text-blue-700 hover:underline">
                              Product {it.linked.product_id}
                            </Link>
                            <div>
                              <Badge tone={it.linked.api_type === "inventory" ? "blue" : "gray"}>
                                {it.linked.api_type === "inventory" ? "Newer API" : "Older API"}
                              </Badge>
                            </div>
                          </div>
                        ) : (
                          <Badge tone="amber">Not a product yet</Badge>
                        )}
                      </td>
                      <td className="px-3 py-2 whitespace-nowrap">
                        {it.linked ? `${money(it.linked.site_price)} / ${it.linked.site_stock ?? "—"}` : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <Pager total={data.total} limit={data.limit} offset={data.offset} onChange={(o) => void load(o)} />
        </div>
      )}
    </div>
  );
}
