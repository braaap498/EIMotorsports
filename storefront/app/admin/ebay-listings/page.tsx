"use client";

import Link from "next/link";
import ClickRow from "../../../components/admin/ClickRow";
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
  store: string;
  sku: string | null;
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
const ALL = "__all__"; // the "All stores" choice in the store drop-down

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

  const [picked, setPicked] = useState<Set<number>>(new Set()); // listings ticked on this page
  const [actMsg, setActMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const [mainStore, setMainStore] = useState(""); // the store opened first on a first visit
  const [statusF, setStatusF] = useState<"all" | "active" | "inactive">("all");
  const [repo, setRepo] = useState<RepoResp | null>(null);
  const [busy, setBusy] = useState("");
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
    if (saved && (saved.store === ALL || names.includes(saved.store))) {
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

  // Inactivate or Activate the ticked listings ON EBAY, one after another, with the same warnings and checks as the buttons on the
  // edit screen. Inactivate = end the listing. Activate = relist it (preview first, then confirm, then send; eBay gives a new item number).
  // Each one is logged like a single push.
  async function bulkSet(want: "inactive" | "active") {
    if (!repo || picked.size === 0) return;
    const todo = repo.items.filter((x) => picked.has(x.listing_id) && (want === "inactive" ? x.active : !x.active));
    const n = todo.length;
    if (n === 0) return;
    // each listing is sent to its own store, so check every store involved first
    const names = todo.map((x) => x.store).filter((v, i, a) => a.indexOf(v) === i);
    const stFor = (nm: string) => stores.find((x) => x.store.toLowerCase() === nm.toLowerCase());
    const notConnected = names.filter((nm) => !stFor(nm)?.authorized);
    if (notConnected.length) {
      setActMsg({ kind: "error", text: `${notConnected.join(", ")} ${notConnected.length === 1 ? "is" : "are"} not connected to eBay yet (connect in Store API → Stores), so nothing can be changed there. Nothing was sent.` });
      return;
    }
    const pushesOff = names.filter((nm) => !stFor(nm)?.live_pushes_enabled);
    if (pushesOff.length) {
      setActMsg({ kind: "error", text: `Live pushes are OFF for ${pushesOff.join(", ")}, so nothing can go to eBay. Turn them on in Store API → Stores. Nothing was sent.` });
      return;
    }
    const one = n === 1;
    const idList = (xs: { item_id: string }[]) => xs.slice(0, 10).map((x) => x.item_id).join(", ") + (xs.length > 10 ? ` and ${xs.length - 10} more` : "");
    const store = names.length === 1 ? names[0] : `${names.length} stores: ${names.join(", ")}`;
    let done = 0;
    let streak = 0;
    let stopped = false;
    const failed: string[] = [];
    const warns: string[] = [];

    if (want === "inactive") {
      if (!window.confirm(`Inactivate will END ${one ? "this listing" : `these ${n} listings`} on eBay (${store}, item${one ? "" : "s"} ${idList(todo)}). Buyers will no longer be able to buy ${one ? "it" : "them"}. You can bring ${one ? "it" : "them"} back later with Activate.\n\nContinue?`)) return;
      setBusy("status");
      setActMsg(null);
      for (let i = 0; i < n; i++) {
        const it = todo[i];
        setActMsg({ kind: "success", text: `Working… ${i + 1} of ${n} (item ${it.item_id}). Keep this page open.` });
        try {
          await api("/ebay-edit/end", { method: "POST", body: { store: it.store, item_id: it.item_id } });
          done++;
          streak = 0;
        } catch (e) {
          failed.push(`${it.item_id}: ${errMsg(e)}`);
          if (++streak >= 3 && i < n - 1) {
            stopped = true;
            break;
          }
        }
      }
    } else {
      // step 1: preview every one (nothing is sent); step 2: show what will happen and ask; step 3: send the ones that previewed fine
      type Diff = { field: string; before: string | null; after: string; note?: string };
      const ready: { it: RepoItem; fingerprint: string; diff: Diff[] }[] = [];
      setBusy("status");
      setActMsg(null);
      for (let i = 0; i < n; i++) {
        const it = todo[i];
        setActMsg({ kind: "success", text: `Checking… ${i + 1} of ${n} (item ${it.item_id}). Nothing is sent yet.` });
        try {
          const pv = await api<{ fingerprint: string; diff: Diff[]; live_pushes_enabled: boolean }>("/ebay-edit/preview", {
            method: "POST",
            body: { store: it.store, item_id: it.item_id, changes: {}, action: "relist" },
          });
          if (!pv.live_pushes_enabled) {
            setActMsg({ kind: "error", text: `Live pushes are OFF for ${store}, so nothing can go to eBay. Turn them on in Store API → Stores. Nothing was sent.` });
            setBusy("");
            return;
          }
          ready.push({ it, fingerprint: pv.fingerprint, diff: pv.diff });
          streak = 0;
        } catch (e) {
          failed.push(`${it.item_id}: ${errMsg(e)}`);
          if (++streak >= 3 && i < n - 1) {
            stopped = true;
            break;
          }
        }
      }
      if (ready.length === 0) {
        setActMsg({ kind: "error", text: `None of the ${n} could be activated, so nothing was sent. ${failed.slice(0, 5).join(" · ")}${failed.length > 5 ? ` · and ${failed.length - 5} more` : ""}` });
        setBusy("");
        return;
      }
      const lines = ready
        .slice(0, 8)
        .map((r) => `Item ${r.it.item_id}\n` + r.diff.map((d) => `  ${d.field}: ${d.before ?? "—"} → ${d.after}`).join("\n"))
        .join("\n\n");
      const more = ready.length > 8 ? `\n\n…and ${ready.length - 8} more like these.` : "";
      const bad = failed.length
        ? `\n\nThese cannot be activated and will be skipped:\n${failed.slice(0, 5).join("\n")}${failed.length > 5 ? `\n…and ${failed.length - 5} more` : ""}`
        : "";
      const rn = ready.length;
      if (
        !window.confirm(
          `Activate will make ${rn === 1 ? "this listing" : `these ${rn} listings`} LIVE on eBay (${store}). Buyers will be able to buy ${rn === 1 ? "it" : "them"}, and eBay will give ${rn === 1 ? "it" : "each"} a new item number.\n\n${lines}${more}${bad}\n\nContinue?`
        )
      ) {
        setActMsg({ kind: "success", text: "Cancelled. Nothing was sent to eBay." });
        setBusy("");
        return;
      }
      streak = 0;
      for (let i = 0; i < rn; i++) {
        const r = ready[i];
        setActMsg({ kind: "success", text: `Working… ${i + 1} of ${rn} (item ${r.it.item_id}). Keep this page open.` });
        try {
          const res = await api<{ warnings: { message: string }[] }>("/ebay-edit/push", {
            method: "POST",
            body: { store: r.it.store, item_id: r.it.item_id, changes: {}, action: "relist", fingerprint: r.fingerprint },
          });
          if (res.warnings?.length) warns.push(`${r.it.item_id}: ${res.warnings.map((w) => w.message).join("; ")}`);
          done++;
          streak = 0;
        } catch (e) {
          failed.push(`${r.it.item_id}: ${errMsg(e)}`);
          if (++streak >= 3 && i < rn - 1) {
            stopped = true;
            break;
          }
        }
      }
    }

    const total = want === "inactive" ? n : done + failed.length;
    let text = `${done} of ${want === "inactive" ? n : total} ${want === "inactive" ? "ended" : "made live"} on eBay.`;
    if (failed.length) text += ` ${failed.length} failed: ${failed.slice(0, 5).join(" · ")}${failed.length > 5 ? ` · and ${failed.length - 5} more` : ""}.`;
    if (warns.length) text += ` eBay warnings: ${warns.slice(0, 5).join(" · ")}${warns.length > 5 ? ` · and ${warns.length - 5} more` : ""}.`;
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
      if (useStore === ALL) params.set("all_stores", "1");
      else if (useStore.trim()) params.set("store", useStore.trim());
      saveSaved("ebay-listings", { store: useStore, q: useQ, sort: useSort, offset, statusF: useStatus });
      setRepo(await api<RepoResp>(`/ebay-listings/repo?${params.toString()}`));
    } catch (e) {
      setRepo(null);
      setError(errMsg(e));
    } finally {
      setLoading(false);
    }
  }

  const allMode = !!repo && repo.store === ""; // the "All stores" view

  // which bulk buttons make sense: Inactivate for ticked listings that are active, Activate for ticked listings that are not
  const pickedItems = repo ? repo.items.filter((x) => picked.has(x.listing_id)) : [];
  const nActivePicked = pickedItems.filter((x) => x.active).length;
  const nInactivePicked = pickedItems.length - nActivePicked;

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-semibold">Listings</h1>
        <p className="mt-1 max-w-3xl text-sm text-gray-600">
          Shows a store&rsquo;s eBay listings and which ones are products on the website. Opening a listing to edit it, or using
          Activate / Inactivate here, changes the real listing on eBay (only when live pushes are ON for that store). Stores
          are connected under Store API &rarr; Stores. Until a store&rsquo;s owner authorizes the app, eBay&rsquo;s public view
          is all that can be seen, so listings that are out of stock or ended do not appear yet.
        </p>
      </div>

      {error && <Notice kind="error">{error}</Notice>}

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
            <option value={ALL}>All stores</option>
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
            <span className="font-medium">{allMode ? "All stores" : repo.store}</span> in our database: {repo.counts.all} listing{repo.counts.all === 1 ? "" : "s"}, {repo.counts.active} active, {repo.counts.inactive} inactive.
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
              {nInactivePicked > 0 && (
                <button type="button" className={btn2Cls} disabled={busy === "status"} onClick={() => void bulkSet("active")}>
                  Activate on eBay ({nInactivePicked})
                </button>
              )}
              {nActivePicked > 0 && (
                <button type="button" className={`${btn2Cls} text-red-700`} disabled={busy === "status"} onClick={() => void bulkSet("inactive")}>
                  Inactivate on eBay ({nActivePicked})
                </button>
              )}
              <button type="button" className="text-xs underline" onClick={() => setPicked(new Set())}>
                Clear
              </button>
              <span className="text-xs text-gray-500">Sends to eBay one at a time (needs live pushes ON for each store involved).</span>
            </div>
          )}
          {repo.items.length === 0 ? (
            <Notice>No listings{statusF !== "all" ? ` marked ${statusF}` : ""}{q.trim() ? " match that search" : ""} for {allMode ? "these stores" : "this store"}.</Notice>
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
                    <th className="px-3 py-2">Store</th>
                    <th className="px-3 py-2">SKU</th>
                    <th className="px-3 py-2">Custom label</th>
                    <th className="px-3 py-2">Price</th>
                    <th className="px-3 py-2">Quantity</th>
                    <th className="px-3 py-2">Status</th>
                    <th className="px-3 py-2"></th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {repo.items.map((it) => {
                    const rowStore = stores.find((x) => x.store.toLowerCase() === it.store.toLowerCase());
                    const canEdit = rowStore?.authorized;
                    const editHref = `/admin/ebay-edit?store=${encodeURIComponent(it.store)}&item=${it.item_id}`;
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
                          <Link href={`/admin/products/${it.product_id}`} className="ml-3 text-xs text-blue-700 hover:underline">
                            Product {it.product_id}
                          </Link>
                        </td>
                        <td className="whitespace-nowrap px-3 py-2 text-xs text-gray-700">{rowStore?.display_name || it.store}</td>
                        <td className="whitespace-nowrap px-3 py-2 font-mono text-xs">{it.sku ?? "—"}</td>
                        <td className="px-3 py-2 font-mono text-xs">{it.custom_label ?? "—"}</td>
                        <td className="px-3 py-2 whitespace-nowrap">{money(it.price, it.currency)}</td>
                        <td className="px-3 py-2 whitespace-nowrap">{it.quantity ?? "—"}</td>
                        <td className="px-3 py-2">
                          <Badge tone={it.active ? "green" : "gray"}>{it.status || "no status"}</Badge>
                        </td>
                        <td className="whitespace-nowrap px-3 py-2 text-right">
                          {canEdit ? (
                            <Link href={editHref} className={`${btn2Cls} !px-3 !py-1`}>
                              Edit
                            </Link>
                          ) : (
                            <span className="text-xs text-gray-400">Not connected</span>
                          )}
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
