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
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const [showAdd, setShowAdd] = useState(false);
  const [storesOpen, setStoresOpen] = useState(false);
  const [picked, setPicked] = useState<Set<number>>(new Set()); // listings ticked on this page
  const [actMsg, setActMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const [mainStore, setMainStore] = useState(""); // the store opened first on a first visit
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
      setMainStore(r.main_store);
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
    const saved = loadSaved<{ store: string; q: string; sort: string; offset: number; statusF?: "all" | "active" | "inactive" }>("ebay-listings");
    const names = stores.map((x) => x.store);
    const first = names.includes(mainStore) ? mainStore : names[0];
    if (saved && names.includes(saved.store)) {
      setStore(saved.store);
      setQ(saved.q);
      setSort(saved.sort);
      const st = saved.statusF ?? "all";
      setStatusF(st);
      void loadRepo(saved.offset, { store: saved.store, q: saved.q, sort: saved.sort, statusF: st });
    } else {
      setStore(first);
      void loadRepo(0, { store: first, q: "", sort: "newest", statusF: "all" });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stores]);

  // Inactivate or Activate the ticked listings ON EBAY, one after another, using the same calls as the buttons on the edit screen
  // (Inactivate = end the listing; Activate = relist it, which gives it a new item number). Each one is logged like a single push.
  async function bulkSet(want: "inactive" | "active") {
    if (!repo || picked.size === 0) return;
    const st = stores.find((x) => x.store === repo.store);
    if (!st?.authorized) {
      setActMsg({ kind: "error", text: `${repo.store} is not connected to eBay yet, so nothing can be changed there. Nothing was sent.` });
      return;
    }
    if (!st.live_pushes_enabled) {
      setActMsg({ kind: "error", text: `Live pushes are OFF for ${repo.store}. Turn them on in the Stores box above first. Nothing was sent.` });
      return;
    }
    const chosen = repo.items.filter((x) => picked.has(x.listing_id));
    const todo = chosen.filter((x) => (want === "inactive" ? x.active : !x.active));
    const skipped = chosen.length - todo.length;
    if (todo.length === 0) {
      setActMsg({ kind: "error", text: `The ${chosen.length} selected listing${chosen.length === 1 ? " is" : "s are"} already ${want} in our records. Nothing was sent.` });
      return;
    }
    const n = todo.length;
    const many = `${n} listing${n === 1 ? "" : "s"}`;
    const skipNote = skipped ? `\n\n${skipped} selected ${skipped === 1 ? "is" : "are"} already ${want} and will be skipped.` : "";
    const ask =
      want === "inactive"
        ? `Inactivate ${many} on eBay (${repo.store})?\n\nThis ENDS them on eBay. Buyers will no longer be able to buy them. You can bring them back later with Activate; eBay gives each one a new item number.${skipNote}\n\nContinue?`
        : `Activate ${many} on eBay (${repo.store})?\n\nThis puts them LIVE on eBay. Buyers will be able to buy them, eBay gives each one a new item number, and eBay may charge its usual listing fees. Each one needs a quantity of 1 or more.${skipNote}\n\nContinue?`;
    if (!window.confirm(ask)) return;
    setBusy("status");
    let done = 0;
    let streak = 0;
    let stopped = false;
    const failed: string[] = [];
    for (let i = 0; i < n; i++) {
      const it = todo[i];
      setActMsg({ kind: "success", text: `Working… ${i + 1} of ${n} (item ${it.item_id}). Keep this page open.` });
      try {
        if (want === "inactive") {
          await api("/ebay-edit/end", { method: "POST", body: { store: repo.store, item_id: it.item_id } });
        } else {
          const body = { store: repo.store, item_id: it.item_id, changes: {}, action: "relist" };
          const pv = await api<{ fingerprint: string }>("/ebay-edit/preview", { method: "POST", body });
          await api("/ebay-edit/push", { method: "POST", body: { ...body, fingerprint: pv.fingerprint } });
        }
        done++;
        streak = 0;
      } catch (e) {
        failed.push(`${it.item_id}: ${errMsg(e)}`);
        streak++;
        if (streak >= 3 && i < n - 1) {
          stopped = true;
          break;
        }
      }
    }
    const verb = want === "inactive" ? "ended" : "made live";
    let text = `${done} of ${n} ${verb} on eBay.`;
    if (skipped) text += ` ${skipped} skipped (already ${want}).`;
    if (failed.length) text += ` ${failed.length} failed: ${failed.slice(0, 5).join(" · ")}${failed.length > 5 ? ` · and ${failed.length - 5} more` : ""}.`;
    if (stopped) text += " Stopped after 3 failures in a row; the rest were not tried.";
    setActMsg({ kind: failed.length ? "error" : "success", text });
    setBusy("");
    await loadRepo(repo.offset);
  }

  async function loadRepo(offset: number, from?: { store: string; q: string; sort: string; statusF: "all" | "active" | "inactive" }) {
    const useStore = from ? from.store : store;
    const useQ = from ? from.q : q;
    const useSort = from ? from.sort : sort;
    const useStatus = from ? from.statusF : statusF;
    setLoading(true);
    setError("");
    setPicked(new Set());
    try {
      // the database list sorts by title / price / newest; eBay's own "newest" and price orders map onto these
      const params = new URLSearchParams({ q: useQ.trim(), sort: useSort, status: useStatus, offset: String(offset), limit: String(PAGE_SIZE) });
      if (useStore.trim()) params.set("store", useStore.trim());
      saveSaved("ebay-listings", { store: useStore, q: useQ, sort: useSort, offset, statusF: useStatus });
      setRepo(await api<RepoResp>(`/ebay-listings/repo?${params.toString()}`));
    } catch (e) {
      setRepo(null);
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

      {/* ---- store, status and search */}
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          void loadRepo(0);
        }}
      >
        <label className="block w-64 text-sm font-medium text-gray-700">
          <span className="mb-1 block">Store</span>
          <select
            className={inputCls}
            value={store}
            onChange={(e) => {
              setStore(e.target.value);
              void loadRepo(0, { store: e.target.value, q, sort, statusF });
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
            <option value="all">Both{repo ? ` (${repo.counts.all})` : ""}</option>
          </select>
        </label>
        <label className="block w-64 text-sm font-medium text-gray-700">
          <span className="mb-1 block">Search title, item number or custom label</span>
          <input className={inputCls} value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. Tusk 1.8 bar" />
        </label>
        <label className="block text-sm font-medium text-gray-700">
          <span className="mb-1 block">Order</span>
          <select className={inputCls} value={sort} onChange={(e) => setSort(e.target.value)}>
            <option value="newest">Newest first</option>
            <option value="title">Title, A to Z</option>
            <option value="price_low">Price, low to high</option>
            <option value="price_high">Price, high to low</option>
          </select>
        </label>
        <button type="submit" className={btnCls} disabled={loading}>
          {loading ? "Reading…" : "Show listings"}
        </button>
      </form>

      {/* ---- listings from our database */}
      {repo && (
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
          {actMsg && <Notice kind={actMsg.kind}>{actMsg.text}</Notice>}
          {picked.size > 0 && (
            <div className="flex flex-wrap items-center gap-3 rounded border border-gray-200 bg-gray-50 px-3 py-2 text-sm">
              <span className="font-medium">{picked.size} selected</span>
              <button type="button" className={btn2Cls} disabled={busy === "status"} onClick={() => void bulkSet("active")}>
                Activate on eBay
              </button>
              <button type="button" className={btn2Cls} disabled={busy === "status"} onClick={() => void bulkSet("inactive")}>
                Inactivate on eBay
              </button>
              <button type="button" className="text-xs underline" onClick={() => setPicked(new Set())}>
                Clear
              </button>
              <span className="text-xs text-gray-500">Sends to eBay one at a time (needs live pushes ON for this store).</span>
            </div>
          )}
          {repo.items.length === 0 ? (
            <Notice>No listings{statusF !== "all" ? ` marked ${statusF}` : ""}{q.trim() ? " match that search" : ""} for this store.</Notice>
          ) : (
            <div className="overflow-x-auto rounded border border-gray-200 bg-white">
              <table className="w-full text-left text-sm">
                <thead className="bg-gray-50 text-xs uppercase text-gray-500">
                  <tr>
                    <th className="w-8 px-3 py-2">
                      <input
                        type="checkbox"
                        aria-label="Select all on this page"
                        checked={repo.items.length > 0 && repo.items.every((x) => picked.has(x.listing_id))}
                        onChange={(e) => setPicked(e.target.checked ? new Set(repo.items.map((x) => x.listing_id)) : new Set())}
                      />
                    </th>
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
                          <input
                            type="checkbox"
                            aria-label={`Select ${it.item_id}`}
                            checked={picked.has(it.listing_id)}
                            onChange={(e) =>
                              setPicked((cur) => {
                                const n = new Set(cur);
                                if (e.target.checked) n.add(it.listing_id);
                                else n.delete(it.listing_id);
                                return n;
                              })
                            }
                          />
                        </td>
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

    </div>
  );
}
