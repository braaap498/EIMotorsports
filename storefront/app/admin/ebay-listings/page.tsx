"use client";

import Link from "next/link";
import ClickRow from "../../../components/admin/ClickRow";
import EbaySalesPanel from "../../../components/admin/EbaySalesPanel";
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

type RepoItem = {
  item_id: string;
  listing_id: number;
  title: string;
  custom_label: string | null;
  price: number | null;
  currency: string;
  quantity: number | null;
  status: string | null;
  active: boolean;
  api_type: "trading" | "inventory" | null;
  product_id: number;
  image_url: string | null;
  url: string;
};
type RepoResp = {
  store: string;
  seller: string;
  known_store: boolean;
  status: "all" | "active" | "inactive";
  total: number;
  counts: { all: number; active: number; inactive: number };
  statuses: { status: string; count: number }[];
  offset: number;
  limit: number;
  items: RepoItem[];
};

const PAGE_SIZE = 50;
const MAIN = "eandimotorsports"; // opened first when it is in the list

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
  const [storesOpen, setStoresOpen] = useState(false);
  const [source, setSource] = useState<"db" | "ebay">("db"); // our database (all listings) or eBay's public search (active only)
  const [statusF, setStatusF] = useState<"all" | "active" | "inactive">("all");
  const [repo, setRepo] = useState<RepoResp | null>(null);
  const [newStore, setNewStore] = useState("");
  const [newName, setNewName] = useState("");
  const [newSeller, setNewSeller] = useState("");
  const [addMsg, setAddMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState("");
  const [diag, setDiag] = useState<{
    store: string;
    scopes_granted: string[];
    scopes_missing: string[];
    checks: { name: string; ok: boolean; status: number | null; note: string }[];
  } | null>(null);
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

  // Coming back from another admin page: put the store, search and page back and show the same listings.
  // Waits for the store list, because the dropdown can only hold a store that exists; a first visit opens the first store.
  const restored = useRef(false);
  useEffect(() => {
    if (restored.current || stores.length === 0) return;
    restored.current = true;
    const saved = loadSaved<{ store: string; q: string; sort: string; offset: number; source?: "db" | "ebay"; statusF?: "all" | "active" | "inactive" }>("ebay-listings");
    const names = stores.map((x) => x.store);
    const first = names.includes(MAIN) ? MAIN : names[0];
    if (saved && names.includes(saved.store)) {
      setStore(saved.store);
      setQ(saved.q);
      setSort(saved.sort);
      const src = saved.source ?? "db";
      const st = saved.statusF ?? "all";
      setSource(src);
      setStatusF(st);
      if (src === "ebay") void load(saved.offset, saved);
      else void loadRepo(saved.offset, { store: saved.store, q: saved.q, sort: saved.sort, statusF: st });
    } else {
      setStore(first);
      void loadRepo(0, { store: first, q: "", sort: "newest", statusF: "all" });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stores]);

  async function loadRepo(offset: number, from?: { store: string; q: string; sort: string; statusF: "all" | "active" | "inactive" }) {
    const useStore = from ? from.store : store;
    const useQ = from ? from.q : q;
    const useSort = from ? from.sort : sort;
    const useStatus = from ? from.statusF : statusF;
    setLoading(true);
    setError("");
    try {
      // the database list sorts by title / price / newest; eBay's own "newest" and price orders map onto these
      const params = new URLSearchParams({ q: useQ.trim(), sort: useSort, status: useStatus, offset: String(offset), limit: String(PAGE_SIZE) });
      if (useStore.trim()) params.set("store", useStore.trim());
      saveSaved("ebay-listings", { store: useStore, q: useQ, sort: useSort, offset, source: "db", statusF: useStatus });
      setRepo(await api<RepoResp>(`/ebay-listings/repo?${params.toString()}`));
      setData(null);
    } catch (e) {
      setRepo(null);
      setError(errMsg(e));
    } finally {
      setLoading(false);
    }
  }

  async function load(offset: number, from?: { store: string; q: string; sort: string }) {
    const useStore = from ? from.store : store;
    const useQ = from ? from.q : q;
    const useSort = from ? from.sort : sort;
    setLoading(true);
    setError("");
    try {
      const params = new URLSearchParams({ q: useQ.trim(), sort: useSort, offset: String(offset), limit: String(PAGE_SIZE) });
      if (useStore.trim()) params.set("store", useStore.trim());
      saveSaved("ebay-listings", { store: useStore, q: useQ, sort: useSort, offset, source: "ebay" });
      setData(await api<ListResp>(`/ebay-listings/list?${params.toString()}`));
      setRepo(null);
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
      const fmt = (v: unknown) => {
        const n = Number(v);
        return v === undefined || v === null || v === "" || Number.isNaN(n)
          ? "?"
          : n.toLocaleString("en-US", { maximumFractionDigits: 2 });
      };
      const lim = r.selling_limit
        ? ` Selling limit: ${fmt(r.selling_limit.quantity)} items / $${fmt(r.selling_limit.amount?.value)}.`
        : " eBay reported no selling limit.";
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

  async function setPushes(s: string, enabled: boolean) {
    const q = enabled
      ? `Turn live pushes ON for ${s}? After this, "Send to eBay" on the edit page changes real listings on that store.`
      : `Turn live pushes OFF for ${s}? Previews still work; nothing can be sent.`;
    if (!window.confirm(q)) return;
    setBusy(s);
    setConnMsg(null);
    try {
      await api("/ebay-edit/live-pushes", { method: "PUT", body: { store: s, enabled } });
      await loadStores();
    } catch (e) {
      setConnMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  async function checkAccess(s: string) {
    setBusy(s);
    setConnMsg(null);
    setDiag(null);
    try {
      setDiag(await api("/ebay-connect/check", { method: "POST", body: { store: s } }));
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
        <button
          type="button"
          className="flex w-full flex-wrap items-center justify-between gap-2 text-left"
          onClick={() => setStoresOpen((v) => !v)}
          aria-expanded={storesOpen}
        >
          <h2 className="text-sm font-semibold">
            <span className="mr-2 inline-block w-3 text-gray-500">{storesOpen ? "▾" : "▸"}</span>Stores
          </h2>
          {!storesOpen && (
            <span className="text-xs text-gray-500">
              {stores.length} store{stores.length === 1 ? "" : "s"} · {stores.filter((s) => s.authorized).length} authorized ·{" "}
              {stores.filter((s) => s.live_pushes_enabled).length} with live pushes on
            </span>
          )}
        </button>
        {storesOpen && (
          <div className="mt-3 space-y-3">
            <div>
              <button type="button" className={btn2Cls} onClick={() => setShowAdd((v) => !v)}>
                {showAdd ? "Close" : "Add store"}
              </button>
            </div>
            {connMsg && <Notice kind={connMsg.kind}>{connMsg.text}</Notice>}
            {diag && (
              <div className="space-y-1 rounded border border-gray-200 bg-gray-50 p-3 text-sm">
                <div className="font-medium">Access check for {diag.store} (read only)</div>
                {diag.checks.map((c) => (
                  <div key={c.name} className={c.ok ? "text-green-700" : "text-red-700"}>
                    {c.ok ? "✓" : "✗"} {c.name}: {c.note}
                    {c.status && !c.ok ? ` (HTTP ${c.status})` : ""}
                  </div>
                ))}
                <div className="text-gray-600">
                  {diag.scopes_missing.length === 0
                    ? "All requested permissions were granted."
                    : `Not granted: ${diag.scopes_missing.map((x) => x.split("/").pop()).join(", ")}`}
                </div>
              </div>
            )}
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs text-gray-500">
                  <tr>
                    <th className="py-1 pr-6 font-normal">Store</th>
                    <th className="py-1 pr-6 font-normal">eBay name</th>
                    <th className="py-1 pr-6 font-normal">Linked</th>
                    <th className="py-1 pr-6 font-normal">Access</th>
                    <th className="py-1 pr-6 font-normal">Pushes</th>
                    <th className="py-1 pr-6 font-normal">Approval ends</th>
                    <th className="py-1 font-normal">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {stores.map((s) => (
                    <tr key={s.store} className="align-middle">
                      <td className="py-2 pr-6">
                        <button type="button" className="text-left font-medium underline-offset-2 hover:underline" onClick={() => setStore(s.store)}>
                          {s.display_name || s.store}
                        </button>
                      </td>
                      <td className="py-2 pr-6 text-gray-500">{s.store}</td>
                      <td className="py-2 pr-6 text-gray-500">{s.linked_count} linked to products</td>
                      <td className="py-2 pr-6">
                        <Badge tone={s.authorized ? "green" : "gray"}>{s.authorized ? "Authorized" : "Not authorized yet"}</Badge>
                      </td>
                      <td className="py-2 pr-6">
                        <Badge tone={s.live_pushes_enabled ? "amber" : "gray"}>{s.live_pushes_enabled ? "Live pushes ON" : "Preview only"}</Badge>
                      </td>
                      <td className="py-2 pr-6 text-xs text-gray-500">
                        {s.authorized && s.refresh_token_expires_at ? new Date(s.refresh_token_expires_at).toLocaleDateString() : "—"}
                      </td>
                      <td className="py-2">
                        <span className="flex flex-wrap gap-2">
                          <button type="button" className={btn2Cls} disabled={busy === s.store} onClick={() => void connect(s.store)}>
                            {s.authorized ? "Reconnect" : "Connect"}
                          </button>
                          {s.authorized && (
                            <>
                              <button type="button" className={btn2Cls} disabled={busy === s.store} onClick={() => void testConn(s.store)}>
                                Test
                              </button>
                              <button type="button" className={btn2Cls} disabled={busy === s.store} onClick={() => void setPushes(s.store, !s.live_pushes_enabled)}>
                                {s.live_pushes_enabled ? "Turn pushes OFF" : "Turn pushes ON"}
                              </button>
                              <button type="button" className={btn2Cls} disabled={busy === s.store} onClick={() => void checkAccess(s.store)}>
                                Check access
                              </button>
                              <button type="button" className={btn2Cls} disabled={busy === s.store} onClick={() => void disconnect(s.store)}>
                                Disconnect
                              </button>
                            </>
                          )}
                        </span>
                      </td>
                    </tr>
                  ))}
                  {stores.length === 0 && (
                    <tr>
                      <td colSpan={7} className="py-2 text-gray-500">No stores yet.</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {storesOpen && showAdd && (
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

      {/* ---- eBay sales: polling, set-aside, quantity sync */}
      <EbaySalesPanel />

      {/* ---- store, source and search */}
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          if (source === "db") void loadRepo(0);
          else void load(0);
        }}
      >
        <label className="block w-64 text-sm font-medium text-gray-700">
          <span className="mb-1 block">Store</span>
          <select
            className={inputCls}
            value={store}
            onChange={(e) => {
              setStore(e.target.value);
              if (source === "db") void loadRepo(0, { store: e.target.value, q, sort, statusF });
            }}
          >
            {stores.map((s) => (
              <option key={s.store} value={s.store}>
                {s.display_name || s.store}
                {s.authorized ? "" : " (not connected)"}
              </option>
            ))}
          </select>
        </label>
        <label className="block text-sm font-medium text-gray-700">
          <span className="mb-1 block">Listings from</span>
          <select
            className={inputCls}
            value={source}
            onChange={(e) => {
              const v = e.target.value as "db" | "ebay";
              setSource(v);
              setData(null);
              setRepo(null);
              if (v === "ebay" && sort === "title") setSort("newest");
              if (v === "db") void loadRepo(0, { store, q, sort, statusF });
            }}
          >
            <option value="db">Our database (all listings)</option>
            <option value="ebay">eBay right now (active only)</option>
          </select>
        </label>
        {source === "db" && (
          <label className="block text-sm font-medium text-gray-700">
            <span className="mb-1 block">Show</span>
            <select
              className={inputCls}
              value={statusF}
              onChange={(e) => {
                const v = e.target.value as "all" | "active" | "inactive";
                setStatusF(v);
                void loadRepo(0, { store, q, sort, statusF: v });
              }}
            >
              <option value="active">Active{repo ? ` (${repo.counts.active})` : ""}</option>
              <option value="inactive">Inactive{repo ? ` (${repo.counts.inactive})` : ""}</option>
              <option value="all">All{repo ? ` (${repo.counts.all})` : ""}</option>
            </select>
          </label>
        )}
        <label className="block w-64 text-sm font-medium text-gray-700">
          <span className="mb-1 block">{source === "db" ? "Search title, item number or custom label" : "Search titles (optional)"}</span>
          <input className={inputCls} value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. Tusk 1.8 bar" />
        </label>
        <label className="block text-sm font-medium text-gray-700">
          <span className="mb-1 block">Order</span>
          <select className={inputCls} value={sort} onChange={(e) => setSort(e.target.value)}>
            <option value="newest">Newest first</option>
            {source === "db" && <option value="title">Title, A to Z</option>}
            <option value="price_low">Price, low to high</option>
            <option value="price_high">Price, high to low</option>
          </select>
        </label>
        <button type="submit" className={btnCls} disabled={loading}>
          {loading ? "Reading…" : "Show listings"}
        </button>
      </form>

      {/* ---- results */}
      {/* ---- results from our database */}
      {source === "db" && repo && (
        <div className="space-y-3">
          <Pager total={repo.total} limit={repo.limit} offset={repo.offset} onChange={(o) => void loadRepo(o)} />
          <p className="text-sm text-gray-700">
            <span className="font-medium">{repo.store}</span> in our database: {repo.counts.all} listing{repo.counts.all === 1 ? "" : "s"}, {repo.counts.active} active, {repo.counts.inactive} inactive.
            {repo.statuses.length > 0 && (
              <span className="ml-1 text-xs text-gray-500">
                Statuses on file: {repo.statuses.map((x) => `${x.status} (${x.count})`).join(", ")}.
              </span>
            )}
          </p>
          {repo.items.length === 0 ? (
            <Notice>No listings{statusF !== "all" ? ` marked ${statusF}` : ""}{q.trim() ? " match that search" : ""} for this store.</Notice>
          ) : (
            <div className="overflow-x-auto rounded border border-gray-200 bg-white">
              <table className="w-full text-left text-sm">
                <thead className="bg-gray-50 text-xs uppercase text-gray-500">
                  <tr>
                    <th className="w-16 px-3 py-2"></th>
                    <th className="px-3 py-2">Listing</th>
                    <th className="px-3 py-2">Custom label</th>
                    <th className="px-3 py-2">Price</th>
                    <th className="px-3 py-2">Quantity</th>
                    <th className="px-3 py-2">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {repo.items.map((it) => {
                    const canEdit = stores.find((x) => x.store === repo.store)?.authorized;
                    const editHref = `/admin/ebay-edit?store=${encodeURIComponent(repo.store)}&item=${it.item_id}`;
                    return (
                      <ClickRow key={it.listing_id} className="align-top" href={canEdit ? editHref : undefined}>
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
                          {canEdit && (
                            <Link href={editHref} className="ml-3 text-xs font-medium text-blue-700 hover:underline">
                              Edit
                            </Link>
                          )}
                          <Link href={`/admin/products/${it.product_id}`} className="ml-3 text-xs text-blue-700 hover:underline">
                            Product {it.product_id}
                          </Link>
                        </td>
                        <td className="px-3 py-2 font-mono text-xs">{it.custom_label ?? "—"}</td>
                        <td className="px-3 py-2 whitespace-nowrap">{money(it.price, it.currency)}</td>
                        <td className="px-3 py-2 whitespace-nowrap">{it.quantity ?? "—"}</td>
                        <td className="px-3 py-2">
                          <Badge tone={it.active ? "green" : "gray"}>{it.status || "no status"}</Badge>
                        </td>
                      </ClickRow>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          <Pager total={repo.total} limit={repo.limit} offset={repo.offset} onChange={(o) => void loadRepo(o)} />
        </div>
      )}

      {source === "ebay" && data && (
        <div className="space-y-3">
          <Pager total={data.total} limit={data.limit} offset={data.offset} onChange={(o) => void load(o)} />
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
              {q.trim() ? " with that search" : ""}.
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
                    <ClickRow
                      key={it.item_id}
                      className="align-top"
                      href={stores.find((x) => x.store === data.store)?.authorized ? `/admin/ebay-edit?store=${encodeURIComponent(data.store)}&item=${it.item_id}` : undefined}
                    >
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
                        {stores.find((x) => x.store === data.store)?.authorized && (
                          <Link
                            href={`/admin/ebay-edit?store=${encodeURIComponent(data.store)}&item=${it.item_id}`}
                            className="ml-3 text-xs font-medium text-blue-700 hover:underline"
                          >
                            Edit
                          </Link>
                        )}
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
                    </ClickRow>
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
