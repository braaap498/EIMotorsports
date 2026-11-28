"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import ClickRow from "../../../components/admin/ClickRow";
import { useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { loadSaved, saveSaved } from "../../../lib/savedState";
import { Badge, Notice, Pager, btnCls, btn2Cls, inputCls } from "../../../components/admin/ui";
import { SortTh, type SortState } from "../../../components/admin/SortHeader";
import ClearableInput from "../../../components/admin/ClearableInput";
import PiecesBox from "../../../components/admin/PiecesBox";
import ProductPicker, { type PickRow } from "../../../components/admin/inventory/ProductPicker";

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
  product_label?: string | null; // the custom label of the product this listing points at
  price: number | null;
  currency: string;
  quantity: number | null;
  pieces_per_sale?: number; // units of the product one sale of this listing uses (3 for a 3-pack); applies to every order
  quantity_from_ebay?: boolean; // true when our stock is 0 and this number is what eBay shows as available
  status: string | null;
  active: boolean;
  unsent_edits?: boolean; // edits saved on the website that have not been pushed to eBay
  api_type: "trading" | "inventory" | null;
  product_id: number;
  image_url: string | null;
  is_product_source: boolean;
  url: string;
};
type DelItem = { listing_id: number; store: string; item_id: string; title: string | null; status: string | null; active: boolean; product_id: number | null; sku: string | null; other_listings: number };
type ListingDeletePreview = { requested: number; missing: number[]; deletable: DelItem[]; skipped_active: DelItem[] };
type RepoResp = {
  store: string;
  seller: string;
  known_store: boolean;
  status: string;
  total: number; // exact once the counts have arrived (totalExact), until then an estimate that is only used to enable Next
  counts?: { all: number; active: number; inactive: number }; // arrive just after the rows
  statuses?: { status: string; count: number }[];
  offset: number;
  limit: number;
  has_more?: boolean;
  items: RepoItem[];
  scope?: string; // set here: which store / status / search this page belongs to
  storeKey?: string;
  totalExact?: boolean;
};
type RepoMeta = { scope: string; storeKey: string; total: number; counts: NonNullable<RepoResp["counts"]>; statuses: NonNullable<RepoResp["statuses"]> };

type SyncRow = {
  store: string;
  connected: boolean;
  status: string | null;
  finished_at: string | null;
  marked_active: number | null;
  marked_ended: number | null;
  message: string | null;
  running: boolean;
};
type SyncResp = { ready: boolean; stores: SyncRow[] };

function ago(iso: string | null): string {
  if (!iso) return "never";
  const mins = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (mins < 2) return "just now";
  if (mins < 90) return `${mins} minutes ago`;
  const h = Math.round(mins / 60);
  return h < 48 ? `${h} hours ago` : `${Math.round(h / 24)} days ago`;
}

const PAGE_SIZE = 50;
// Column sorting, like the Orders list. The order is kept as the text the server takes: "newest" (nothing chosen) or "key:asc" / "key:desc".
type ListSortKey = "title" | "store" | "label" | "price" | "qty" | "status";
function parseSort(v: string): SortState<ListSortKey> {
  if (v === "title") return { key: "title", dir: "asc" };
  if (v === "price_low") return { key: "price", dir: "asc" };
  if (v === "price_high") return { key: "price", dir: "desc" };
  const [k, d] = v.split(":");
  if (["title", "store", "label", "price", "qty", "status"].includes(k)) return { key: k as ListSortKey, dir: d === "desc" ? "desc" : "asc" };
  return { key: null, dir: "asc" };
}

const ALL = "__all__"; // the "All stores" choice in the store drop-down

function money(v: string | number | null | undefined, cur?: string | null) {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (Number.isNaN(n)) return String(v);
  return `${cur && cur !== "USD" ? cur + " " : "$"}${n.toFixed(2)}`;
}

/* ------------------------------------------------------------------- page */

export default function EbayLiveListingsPage() {
  const router = useRouter();
  const [stores, setStores] = useState<Store[]>([]);
  const [store, setStore] = useState("");
  const [q, setQ] = useState("");
  const [sort, setSort] = useState("newest");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const [picked, setPicked] = useState<Set<number>>(new Set()); // listings ticked on this page (or all matching, after "select all matching")
  const [allMatch, setAllMatch] = useState<RepoItem[] | null>(null); // every listing matching the current search, loaded by "select all matching"
  const [loadingAll, setLoadingAll] = useState(false);
  const [actMsg, setActMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  // delete the ticked listing RECORDS (our database only; nothing is sent to eBay): preview first, then a typed confirmation
  const [delOpen, setDelOpen] = useState(false);
  const [delBusy, setDelBusy] = useState(false);
  const [delErr, setDelErr] = useState("");
  const [delPv, setDelPv] = useState<ListingDeletePreview | null>(null);
  const [delTyped, setDelTyped] = useState("");
  const [delActive, setDelActive] = useState(false);
  const [relink, setRelink] = useState<RepoItem | null>(null); // the listing whose product is being changed
  const [relinkTo, setRelinkTo] = useState<PickRow | null>(null); // the product chosen, waiting for the confirm
  const [relinkBusy, setRelinkBusy] = useState(false);
  const [afterRelink, setAfterRelink] = useState<{ product_id: number; sku: string | null; label: string } | null>(null); // offered right after a product change
  const [mainStore, setMainStore] = useState(""); // the store opened first on a first visit
  const [statusF, setStatusF] = useState<string>("all");
  const [repo, setRepo] = useState<RepoResp | null>(null);
  const reqSeq = useRef(0); // only the newest list request may update the screen (typing fast sends several)
  const pageCache = useRef(new Map<string, { t: number; r: RepoResp }>()); // pages already read (the one shown and its neighbours), good for a minute
  const metaRef = useRef<RepoMeta | null>(null); // the last counts read; they arrive after the rows
  const lastQ = useRef(""); // the search text the list on screen was read with
  const [busy, setBusy] = useState("");
  const [priceOpen, setPriceOpen] = useState(false); // the "Change price" box under the selection bar
  const [priceText, setPriceText] = useState("");
  const [groupDone, setGroupDone] = useState(""); // the custom label whose whole group was just selected
  const [setSite, setSetSite] = useState(true); // also set the website's single-unit price (leave off to change eBay only)
  const [others, setOthers] = useState<RepoItem[]>([]); // listings with the same custom label in OTHER stores than the ticked ones
  const [includeOthers, setIncludeOthers] = useState(false);
  const othersSeq = useRef(0);
  const [drafts, setDrafts] = useState<{ id: number; store: string; source_item_id: string; title: string }[]>([]);
  useEffect(() => {
    api<{ drafts: { id: number; store: string; source_item_id: string; title: string }[] }>("/ebay-copy")
      .then((r) => setDrafts(r.drafts))
      .catch(() => setDrafts([]));
  }, []);
  const [heldMap, setHeldMap] = useState<Record<string, boolean>>({}); // "store|item" -> set to 0 on purpose (sales do not push stock onto it)
  const [zeroMap, setZeroMap] = useState<Record<string, number>>({}); // "store|item" -> days at quantity 0 (listings Set 0 Qty put there)
  const [sync, setSync] = useState<SyncRow[]>([]);
  const [syncNote, setSyncNote] = useState(""); // set when a background check finished and changed statuses
  const live = useRef({ picked: 0, busy: "", offset: 0 });
  live.current = { picked: picked.size, busy, offset: repo?.offset ?? 0 };
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

  // Statuses are kept true by a check against eBay that runs in the background on the server. Opening this tab only asks
  // whether one is due (the answer comes back at once); the list above never waits for it. While one runs, ask how it is
  // doing every few seconds, and when it changed anything show the fresh statuses (unless something is ticked or running).
  async function runSyncNow() {
    try {
      const r = await api<SyncResp>("/ebay-listings/status-sync/run", { method: "POST", body: {} });
      setSync(r.stores);
    } catch {
      /* nothing to do: the daily check still runs */
    }
  }
  const syncAsked = useRef(false);
  useEffect(() => {
    if (syncAsked.current || stores.length === 0) return;
    syncAsked.current = true;
    api<SyncResp>("/ebay-listings/status-sync/start", { method: "POST", body: {} })
      .then((r) => setSync(r.stores))
      .catch(() => {
        /* a convenience: the list works without it */
      });
  }, [stores]);

  // While a check runs, ask how it is doing every few seconds so the "checking…" line stays true. This NEVER reloads the
  // list or clears ticked rows: new statuses show the next time the list is read (arriving on this tab, coming back from a
  // listing, a refresh, or going to another page). When a check finishes with changes a quiet note says so.
  // `wasRunning` always holds the stores that were running at the last answer (a plain copy captured when the timer was
  // made would go stale and fire again on every tick).
  const syncRunning = sync.some((x) => x.running);
  const wasRunning = useRef<string[]>([]);
  useEffect(() => {
    wasRunning.current = sync.filter((x) => x.running).map((x) => x.store);
  }, [sync]);
  useEffect(() => {
    if (!syncRunning) return;
    const t = setInterval(() => {
      api<SyncResp>("/ebay-listings/status-sync")
        .then((r) => {
          const finished = r.stores.filter((x) => wasRunning.current.includes(x.store) && !x.running);
          setSync(r.stores);
          const ended = finished.reduce((n, x) => n + (x.marked_ended ?? 0), 0);
          const madeActive = finished.reduce((n, x) => n + (x.marked_active ?? 0), 0);
          if (ended + madeActive > 0) {
            setSyncNote(`The eBay check finished: ${ended} listing${ended === 1 ? "" : "s"} ended, ${madeActive} marked active.`);
          }
        })
        .catch(() => {});
    }, 5000);
    return () => clearInterval(t);
  }, [syncRunning]);

  // Coming back from another admin page: put the store, search and page back and show the same listings.
  // Waits for the store list, because the dropdown can only hold a store that exists; a first visit opens the first store.
  const restored = useRef(false);
  useEffect(() => {
    if (restored.current || stores.length === 0) return;
    restored.current = true;
    const saved = loadSaved<{ store: string; q: string; sort: string; offset: number; statusF?: string }>("ebay-listings");
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

  // The list follows the search box: a short pause after the last keystroke reads it again from the first page.
  // The store, Show and Order boxes read it as soon as they change. Nothing happens until the first read is done.
  useEffect(() => {
    if (!restored.current || q.trim() === lastQ.current) return;
    const t = setTimeout(() => {
      if (q.trim() !== lastQ.current) void loadRepo(0, { store, q, sort, statusF }); // Enter may already have loaded it
    }, 350);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q]);

  // The ticked listings: the ones on this page, or, after "select all matching", every listing matching the search.
  function selectedItems(): RepoItem[] {
    if (!repo) return [];
    return (allMatch ?? repo.items).filter((x) => picked.has(x.listing_id));
  }

  // Tick every listing that matches the current store / status / search, not just this page. Read from the database only
  // (nothing goes to eBay). Capped so a huge store cannot lock the page up.
  const SELECT_ALL_MAX = 2000;
  async function selectAllMatching() {
    if (!repo || loadingAll) return;
    setLoadingAll(true);
    setActMsg(null);
    try {
      const rows: RepoItem[] = [];
      let offset = 0;
      let grand = 0;
      for (;;) {
        const params = new URLSearchParams({ q: lastQ.current, sort, status: repo.status || "all", offset: String(offset), limit: "200" });
        if (repo.store === "") params.set("all_stores", "1");
        else params.set("store", repo.store);
        const r = await api<RepoResp>(`/ebay-listings/repo?${params.toString()}`);
        rows.push(...r.items);
        offset += r.items.length;
        grand = r.total;
        if (r.items.length === 0 || offset >= r.total || rows.length >= SELECT_ALL_MAX) break;
      }
      const capped = rows.slice(0, SELECT_ALL_MAX);
      setAllMatch(capped);
      setPicked(new Set(capped.map((x) => x.listing_id)));
      if (grand > capped.length) setActMsg({ kind: "error", text: `Only the first ${capped.length} of ${grand} matching listings were selected (the limit). Narrow the search to get the rest.` });
    } catch (e) {
      setActMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setLoadingAll(false);
    }
  }

  // Tick every listing (any status) that has exactly this custom label, in the store being shown (or all stores in the All stores view).
  // The search box matches part of a label, so the rows are filtered to the exact label afterwards. Database only; nothing goes to eBay.
  async function selectLabelGroup(label: string) {
    if (!repo || loadingAll) return;
    setLoadingAll(true);
    setActMsg(null);
    try {
      const rows: RepoItem[] = [];
      let offset = 0;
      for (;;) {
        const params = new URLSearchParams({ q: label, sort, status: "all", offset: String(offset), limit: "200" });
        if (repo.store === "") params.set("all_stores", "1");
        else params.set("store", repo.store);
        const r = await api<RepoResp>(`/ebay-listings/repo?${params.toString()}`);
        rows.push(...r.items);
        offset += r.items.length;
        if (r.items.length === 0 || offset >= r.total || rows.length >= SELECT_ALL_MAX) break;
      }
      const want = label.trim().toLowerCase();
      const group = rows.filter((x) => (x.custom_label ?? "").trim().toLowerCase() === want);
      if (group.length === 0) return;
      setAllMatch(group);
      setPicked(new Set(group.map((x) => x.listing_id)));
      setGroupDone(label);
      setActMsg({ kind: "success", text: `Selected all ${group.length} listing${group.length === 1 ? "" : "s"} with custom label ${label} (every status).` });
    } catch (e) {
      setActMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setLoadingAll(false);
    }
  }

  // Sell similar: copy the one ticked listing into a NEW listing: it is saved as a draft on the website, and the edit screen opens.
  async function copyOne() {
    const it = selectedItems()[0];
    if (!it) return;
    setBusy("copy");
    setActMsg(null);
    try {
      const r = await api<{ id: number }>("/ebay-copy/create", { method: "POST", body: { store: it.store, item_id: it.item_id } });
      router.push(`/admin/ebay-copy?id=${r.id}`);
    } catch (e) {
      setActMsg({ kind: "error", text: errMsg(e) });
      setBusy("");
    }
  }

  // End listing or Relist the ticked listings ON EBAY, one after another, with the same warnings and checks as the buttons on the
  // edit screen. End listing = end it on eBay. Relist = bring it back (preview first, then confirm, then send; eBay gives a new item number).
  // Each one is logged like a single push.
  async function bulkSet(want: "inactive" | "active") {
    if (!repo || picked.size === 0) return;
    const todo = selectedItems().filter((x) => (want === "inactive" ? x.active : !x.active));
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
      if (!window.confirm(`End listing will END ${one ? "this listing" : `these ${n} listings`} on eBay (${store}, item${one ? "" : "s"} ${idList(todo)}). Buyers will no longer be able to buy ${one ? "it" : "them"}.\n\nUse this for parts you will not sell again. If you only want to stop sales for now, use Set 0 Qty instead. An ended listing can be brought back with Relist, but eBay removes ended listings (and their photos) about 90 days after they end, and a relist gets a new item number.\n\nContinue?`)) return;
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
        setActMsg({ kind: "error", text: `None of the ${n} could be relisted, so nothing was sent. ${failed.slice(0, 5).join(" · ")}${failed.length > 5 ? ` · and ${failed.length - 5} more` : ""}` });
        setBusy("");
        return;
      }
      const lines = ready
        .slice(0, 8)
        .map((r) => `Item ${r.it.item_id}\n` + r.diff.map((d) => `  ${d.field}: ${d.before ?? "—"} → ${d.after}`).join("\n"))
        .join("\n\n");
      const more = ready.length > 8 ? `\n\n…and ${ready.length - 8} more like these.` : "";
      const bad = failed.length
        ? `\n\nThese cannot be relisted and will be skipped:\n${failed.slice(0, 5).join("\n")}${failed.length > 5 ? `\n…and ${failed.length - 5} more` : ""}`
        : "";
      const rn = ready.length;
      if (
        !window.confirm(
          `Relist will make ${rn === 1 ? "this listing" : `these ${rn} listings`} LIVE on eBay (${store}). Buyers will be able to buy ${rn === 1 ? "it" : "them"}, and eBay will give ${rn === 1 ? "it" : "each"} a new item number.\n\n${lines}${more}${bad}\n\nContinue?`
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

  async function loadDeletePreview(includeActive: boolean) {
    setDelBusy(true);
    setDelErr("");
    try {
      setDelPv(await api<ListingDeletePreview>("/ebay-listings/repo/delete/preview", {
        method: "POST", body: { listing_ids: [...picked], include_active: includeActive },
      }));
    } catch (e) {
      setDelErr(errMsg(e));
    } finally {
      setDelBusy(false);
    }
  }

  function openDelete() {
    if (picked.size === 0) return;
    setDelOpen(true);
    setDelPv(null);
    setDelTyped("");
    setDelActive(false);
    void loadDeletePreview(false);
  }

  async function confirmDelete() {
    if (!repo || !delPv || delPv.deletable.length === 0 || delTyped.trim().toUpperCase() !== "DELETE") return;
    setDelBusy(true);
    setDelErr("");
    try {
      const r = await api<{ deleted: number; skipped_active: number; products_without_listings: { id: number; sku: string | null }[] }>(
        "/ebay-listings/repo/delete",
        { method: "POST", body: { listing_ids: [...picked], include_active: delActive, confirm: delPv.deletable.length } },
      );
      const bare = r.products_without_listings;
      setActMsg({
        kind: "success",
        text: `Deleted ${r.deleted} listing record${r.deleted === 1 ? "" : "s"} from the website. Nothing was changed on eBay.${r.skipped_active ? ` ${r.skipped_active} active listing${r.skipped_active === 1 ? " was" : "s were"} left alone.` : ""}${bare.length ? ` Now without any listing: ${bare.slice(0, 5).map((b) => b.sku || `#${b.id}`).join(", ")}${bare.length > 5 ? ` and ${bare.length - 5} more` : ""}. You can delete ${bare.length === 1 ? "that product" : "those products"} from the Products page.` : ""}`,
      });
      setDelOpen(false);
      await loadRepo(repo.offset);
    } catch (e) {
      setDelErr(errMsg(e));
    } finally {
      setDelBusy(false);
    }
  }

  // Set the ticked listings to quantity 0 ON EBAY. The listing stays alive (hidden from search while eBay's out-of-stock setting
  // is on) and keeps its item number, photos and history. Bring it back by raising the quantity. One at a time, each one logged.
  async function bulkZero() {
    if (!repo || picked.size === 0) return;
    const todo = selectedItems().filter((x) => x.active && !isZero(x));
    const n = todo.length;
    if (n === 0) return;
    const names = todo.map((x) => x.store).filter((v, i, a) => a.indexOf(v) === i);
    const stFor = (nm: string) => stores.find((x) => x.store.toLowerCase() === nm.toLowerCase());
    const notConnected = names.filter((nm) => !stFor(nm)?.authorized);
    if (notConnected.length) {
      setActMsg({ kind: "error", text: `${notConnected.join(", ")} ${notConnected.length === 1 ? "is" : "are"} not connected to eBay yet, so nothing can be changed there. Nothing was sent.` });
      return;
    }
    const pushesOff = names.filter((nm) => !stFor(nm)?.live_pushes_enabled);
    if (pushesOff.length) {
      setActMsg({ kind: "error", text: `Live pushes are OFF for ${pushesOff.join(", ")}, so nothing can go to eBay. Turn them on in Store API → Stores. Nothing was sent.` });
      return;
    }
    // eBay only accepts quantity 0 when the account's out-of-stock setting is ON. Ask once per store now, instead of once per listing.
    const oosOk = new Set<string>();
    setBusy("status");
    setActMsg({ kind: "success", text: "Checking eBay's out-of-stock setting…" });
    for (const nm of names) {
      try {
        const o = await api<{ enabled: boolean | null }>(`/ebay-edit/oos?store=${encodeURIComponent(nm)}`);
        if (o.enabled === false) {
          setActMsg({ kind: "error", text: `The out-of-stock setting is OFF in the ${nm} eBay account, so quantity 0 would not be allowed. Turn it on first (eBay: Account settings › Selling › Selling preferences, or on the Stores page). Nothing was sent.` });
          setBusy("");
          return;
        }
        if (o.enabled === true) oosOk.add(nm.toLowerCase());
        // null = eBay would not say: leave it to the per-listing check on the server
      } catch {
        /* could not ask: the per-listing check on the server still runs */
      }
    }
    setBusy("");
    setActMsg(null);
    const one = n === 1;
    const ids = todo.slice(0, 10).map((x) => x.item_id).join(", ") + (n > 10 ? ` and ${n - 10} more` : "");
    if (
      !window.confirm(
        `Set Qty 0 will set the quantity to 0 on ${one ? "this listing" : `these ${n} listings`} on eBay (item${one ? "" : "s"} ${ids}).\n\nThe listing is NOT ended: it keeps its item number, photos and history, and buyers cannot buy ${one ? "it" : "them"}. While held at 0, sales of the part will not push stock back onto ${one ? "it" : "them"}. Receiving or adjusting stock on hand releases the hold, or use Restore.\n\nThis only works if the out-of-stock setting is ON in the eBay account (Account settings › Selling › Selling preferences).\n\nContinue?`
      )
    )
      return;
    setBusy("status");
    setActMsg(null);
    let done = 0;
    let streak = 0;
    let stopped = false;
    const failed: string[] = [];
    for (let i = 0; i < n; i++) {
      const it = todo[i];
      setActMsg({ kind: "success", text: `Working… ${i + 1} of ${n} (item ${it.item_id}). Keep this page open.` });
      try {
        await api("/ebay-edit/set-zero", { method: "POST", body: { store: it.store, item_id: it.item_id, oos_checked: oosOk.has(it.store.toLowerCase()) } });
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
    let text = `${done} of ${n} set to quantity 0 on eBay.`;
    if (failed.length) text += ` ${failed.length} failed: ${failed.slice(0, 5).join(" · ")}${failed.length > 5 ? ` · and ${failed.length - 5} more` : ""}.`;
    if (stopped) text += " Stopped after 3 failures in a row; the rest were not tried.";
    setActMsg({ kind: failed.length ? "error" : "success", text });
    setBusy("");
    await loadRepo(repo.offset);
  }

  // Take the ticked listings OFF quantity 0 on eBay: each gets the website's stock on hand, or 1 when keepAlive is true
  // (a quantity of 1 on eBay restarts its zero-days count). A part with no stock on hand is left at 0 and reported.
  async function bulkRestore(keepAlive: boolean) {
    if (!repo || picked.size === 0) return;
    const todo = selectedItems().filter((x) => x.active && isZero(x));
    const n = todo.length;
    if (n === 0) return;
    const names = todo.map((x) => x.store).filter((v, i, a) => a.indexOf(v) === i);
    const stFor = (nm: string) => stores.find((x) => x.store.toLowerCase() === nm.toLowerCase());
    const pushesOff = names.filter((nm) => !stFor(nm)?.live_pushes_enabled);
    if (pushesOff.length) {
      setActMsg({ kind: "error", text: `Live pushes are OFF for ${pushesOff.join(", ")}, so nothing can go to eBay. Turn them on in Store API → Stores. Nothing was sent.` });
      return;
    }
    const one = n === 1;
    const others = selectedItems().length - n;
    const what = keepAlive ? "quantity 1" : "the available stock shown on the website";
    if (!window.confirm(`This will put ${one ? "this listing" : `these ${n} listings`} back on sale on eBay at ${what}.${others > 0 ? `\n\nOnly the ${n} selected listing${one ? "" : "s"} at 0 ${one ? "is" : "are"} changed; the other ${others} ${others === 1 ? "is" : "are"} left alone.` : ""}${keepAlive ? "\n\nBuyers can buy the part while it is at 1, so only do this for a part you could ship." : "\n\nA part with nothing available is left at 0."}\n\nContinue?`)) return;
    setBusy("status");
    setActMsg(null);
    let done = 0;
    let streak = 0;
    let stopped = false;
    const failed: string[] = [];
    for (let i = 0; i < n; i++) {
      const it = todo[i];
      setActMsg({ kind: "success", text: `Working… ${i + 1} of ${n} (item ${it.item_id}). Keep this page open.` });
      try {
        await api("/ebay-edit/restore-qty", { method: "POST", body: { store: it.store, item_id: it.item_id, ...(keepAlive ? { qty: 1 } : {}) } });
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
    let text = `${done} of ${n} put back on sale on eBay.`;
    if (failed.length) text += ` ${failed.length} not changed: ${failed.slice(0, 5).join(" · ")}${failed.length > 5 ? ` · and ${failed.length - 5} more` : ""}.`;
    if (stopped) text += " Stopped after 3 failures in a row; the rest were not tried.";
    setActMsg({ kind: failed.length ? "error" : "success", text });
    setBusy("");
    await loadRepo(repo.offset);
  }

  // Every listing (any status, any store) whose custom label is exactly this one. Database only.
  async function rowsWithLabel(label: string): Promise<RepoItem[]> {
    const rows: RepoItem[] = [];
    let offset = 0;
    for (;;) {
      const params = new URLSearchParams({ q: label, sort: "newest", status: "all", offset: String(offset), limit: "200", all_stores: "1" });
      const r = await api<RepoResp>(`/ebay-listings/repo?${params.toString()}`);
      rows.push(...r.items);
      offset += r.items.length;
      if (r.items.length === 0 || offset >= r.total || rows.length >= SELECT_ALL_MAX) break;
    }
    const want = label.trim().toLowerCase();
    return rows.filter((x) => (x.custom_label ?? "").trim().toLowerCase() === want);
  }

  // Change price: one new price for every ticked listing. ONE confirmation up front, then each active listing is checked against eBay
  // and sent straight away (only the price changes, so there is nothing else to review), one at a time, each logged like a single push.
  // Needs live pushes ON. Ended listings are not sent: the price is saved on the website and fills the price box when the listing is opened
  // or relisted. Optionally the website's single-unit price is set too. Listings with variations are left alone.
  async function bulkPrice() {
    if (!repo || picked.size === 0) return;
    const clean = priceText.trim().replace(/^\$/, "").replace(/,/g, "");
    const num = Number(clean);
    if (!clean || !Number.isFinite(num) || num <= 0 || num > 99999.99) {
      setActMsg({ kind: "error", text: "Type a price above 0 and below 100,000, for example 24.99. Nothing was changed." });
      return;
    }
    const priceStr = num.toFixed(2);
    const seen = new Set<number>();
    const todo = [...selectedItems(), ...(includeOthers ? others : [])].filter((x) => (seen.has(x.listing_id) ? false : (seen.add(x.listing_id), true)));
    if (todo.length === 0) return;
    const active = todo.filter((x) => x.active);
    const ended = todo.filter((x) => !x.active);
    const names = active.map((x) => x.store).filter((v, i, a) => a.indexOf(v) === i);
    const stFor = (nm: string) => stores.find((x) => x.store.toLowerCase() === nm.toLowerCase());
    const notConnected = names.filter((nm) => !stFor(nm)?.authorized);
    if (notConnected.length) {
      setActMsg({ kind: "error", text: `${notConnected.join(", ")} ${notConnected.length === 1 ? "is" : "are"} not connected to eBay yet (connect in Store API → Stores), so the active listings cannot be changed there. Nothing was changed.` });
      return;
    }
    const pushesOff = names.filter((nm) => !stFor(nm)?.live_pushes_enabled);
    if (pushesOff.length) {
      setActMsg({ kind: "error", text: `Live pushes are OFF for ${pushesOff.join(", ")}, so the new price cannot go to eBay. Turn them on in Store API → Stores. Nothing was changed.` });
      return;
    }
    const prices = todo.map((x) => x.price).filter((v): v is number => typeof v === "number");
    const lo = prices.length ? Math.min(...prices) : null;
    const hi = prices.length ? Math.max(...prices) : null;
    const n = todo.length;
    const lines: string[] = [`Change the price to $${priceStr} on ${n} listing${n === 1 ? "" : "s"}.`];
    if (lo !== null && hi !== null) lines.push(`Current price${lo === hi ? "" : "s"}: ${lo === hi ? money(lo) : `${money(lo)} to ${money(hi)}`}.`);
    if (lo !== null && hi !== null && (num > hi * 3 || num < lo / 3)) lines.push(`WARNING: $${priceStr} is far from the current price${lo === hi ? "" : "s"}. Check it is not a typo.`);
    if (active.length) lines.push(`${active.length} active: sent to eBay now (${names.join(", ")}), one at a time.`);
    if (ended.length) lines.push(`${ended.length} ended: NOT sent to eBay. The price is saved on the website and shows when you open or relist ${ended.length === 1 ? "it" : "them"}.`);
    if (includeOthers && others.length) lines.push(`Includes ${others.length} listing${others.length === 1 ? "" : "s"} with the same custom label in other stores.`);
    lines.push(setSite ? "The website's single-unit price for each part is set to this price too." : "The website price is left as it is.");
    lines.push("Listings with variations cannot be changed here and are left alone. Keep this page open until it finishes.");
    if (!window.confirm(lines.join("\n\n") + "\n\nContinue?")) {
      setActMsg({ kind: "success", text: "Cancelled. Nothing was changed." });
      return;
    }

    setBusy("status");
    setActMsg(null);
    let done = 0;
    let streak = 0;
    let stopped = false;
    const failed: string[] = [];
    const warns: string[] = [];
    const skipped: string[] = [];
    const same: RepoItem[] = []; // already at this price on eBay
    const pushedOk: RepoItem[] = [];
    const failedIts: RepoItem[] = [];
    for (let i = 0; i < active.length; i++) {
      const it = active[i];
      setActMsg({ kind: "success", text: `Changing… ${i + 1} of ${active.length} (item ${it.item_id}). Keep this page open.` });
      try {
        const pv = await api<{ fingerprint: string; live_pushes_enabled: boolean }>("/ebay-edit/preview", {
          method: "POST",
          body: { store: it.store, item_id: it.item_id, changes: { price: priceStr }, action: "revise" },
        });
        if (!pv.live_pushes_enabled) throw new Error(`Live pushes are OFF for ${it.store}.`);
        const res = await api<{ warnings: { message: string }[] }>("/ebay-edit/push", {
          method: "POST",
          body: { store: it.store, item_id: it.item_id, changes: { price: priceStr }, action: "revise", fingerprint: pv.fingerprint },
        });
        if (res.warnings?.length) warns.push(`${it.item_id}: ${res.warnings.map((w) => w.message).join("; ")}`);
        done++;
        pushedOk.push(it);
        streak = 0;
      } catch (e) {
        const m = errMsg(e);
        if (/nothing is different/i.test(m)) {
          same.push(it);
          streak = 0;
        } else if (/variations/i.test(m)) {
          skipped.push(`${it.item_id}: has variations`);
        } else {
          failed.push(`${it.item_id}: ${m}`);
          failedIts.push(it);
          if (++streak >= 3 && i < active.length - 1) {
            stopped = true;
            break;
          }
        }
      }
    }
    // the website side: listings that were sent, were already at the price, or are ended; plus those whose push failed
    // (their price is saved as an unsent edit so the list shows them as edited and nothing is lost)
    const handled = [...pushedOk, ...same, ...ended, ...failedIts];
    const unsent = [...ended, ...failedIts];
    let site = "";
    if (handled.length) {
      try {
        const r = await api<{ products_updated: number; saved_edits: number; note?: string | null }>("/ebay-edit/bulk-price", {
          method: "POST",
          body: { price: priceStr, listing_ids: handled.map((x) => x.listing_id), unsent_ids: unsent.map((x) => x.listing_id), set_website_price: setSite },
        });
        if (setSite) site = ` Website price set on ${r.products_updated} part${r.products_updated === 1 ? "" : "s"}.`;
        if (r.note) site += ` ${r.note}`;
      } catch (e) {
        failed.push(`website side: ${errMsg(e)}`);
      }
    }
    let text = `Price $${priceStr}: ${done} of ${active.length} active listing${active.length === 1 ? "" : "s"} sent to eBay.`;
    if (same.length) text += ` ${same.length} already at that price.`;
    if (ended.length) text += ` ${ended.length} ended (saved on the website only).`;
    text += site;
    if (failed.length) text += ` ${failed.length} problem${failed.length === 1 ? "" : "s"}: ${failed.slice(0, 5).join(" · ")}${failed.length > 5 ? ` · and ${failed.length - 5} more` : ""}.`;
    if (skipped.length) text += ` ${skipped.length} left alone (variation listings).`;
    if (warns.length) text += ` eBay warnings: ${warns.slice(0, 5).join(" · ")}${warns.length > 5 ? ` · and ${warns.length - 5} more` : ""}.`;
    if (stopped) text += " Stopped after 3 failures in a row; the rest were not tried.";
    setActMsg({ kind: failed.length ? "error" : "success", text });
    setBusy("");
    setPriceOpen(false);
    setPriceText("");
    setIncludeOthers(false);
    await loadRepo(repo.offset);
  }

  async function doRelink() {
    if (!relink || !relinkTo) return;
    setRelinkBusy(true);
    setActMsg(null);
    try {
      const r = await api<{
        item_id: string;
        old: { sku: string | null; label: string | null } | null;
        new: { sku: string | null; title: string; label: string | null };
        note: string;
      }>("/ebay-listings/repo/relink", { method: "POST", body: { listing_id: relink.listing_id, product_id: relinkTo.id } });
      const lab = r.new.label ? `its custom label is ${r.new.label}` : "it has no custom label";
      setActMsg({
        kind: "success",
        text: `Listing ${r.item_id} now points at ${r.new.sku ?? relinkTo.id} (${r.old?.sku ?? "no product"} before); ${lab}.${r.note ? " " + r.note : ""}`,
      });
      // show the change right away without reloading the page
      setRepo((cur) =>
        cur
          ? {
              ...cur,
              items: cur.items.map((x) =>
                x.listing_id === relink.listing_id ? { ...x, product_id: relinkTo.id, sku: r.new.sku, product_label: r.new.label } : x,
              ),
            }
          : cur,
      );
      setAfterRelink(r.new.label ? { product_id: relinkTo.id, sku: r.new.sku, label: r.new.label } : null);
      setRelink(null);
      setRelinkTo(null);
    } catch (e) {
      setActMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setRelinkBusy(false);
    }
  }

  // Send the product's custom label to every eBay listing linked to it (the same send the product page does), then
  // show the label on the rows that now match.
  async function sendLabelFor(productId: number) {
    setRelinkBusy(true);
    try {
      const s = await api<{ cls: string; results: { item_id: string; status: string; detail: string }[] }>(
        `/kits/${productId}/sync`,
        { method: "POST" },
      );
      const good = new Set(s.results.filter((x) => x.status === "updated" || x.status === "already_current").map((x) => x.item_id.replace(/[^0-9]/g, "")));
      const bad = s.results.filter((x) => x.status === "error" || x.status === "not_sent" || x.status === "skipped");
      setRepo((cur) =>
        cur ? { ...cur, items: cur.items.map((x) => (good.has(x.item_id) ? { ...x, custom_label: s.cls } : x)) } : cur,
      );
      setActMsg({
        kind: bad.length ? "error" : "success",
        text: `Custom label ${s.cls} on eBay: ${good.size} of ${s.results.length} listing(s) correct${bad.length ? `. Problems: ${bad.map((b) => b.detail).join(" | ")}` : ""}`,
      });
      setAfterRelink(null);
    } catch (e) {
      setActMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setRelinkBusy(false);
    }
  }

  async function sendLabelNow() {
    if (!afterRelink) return;
    await sendLabelFor(afterRelink.product_id);
  }

  // The "Update eBay label" button on a row whose eBay label differs from its product's label. Asks first, because it
  // changes eBay and goes to every listing linked to that product, not just this row.
  async function fixRowLabel(it: RepoItem) {
    if (!it.product_label) return;
    if (!window.confirm(`Change the custom label on eBay from ${it.custom_label ?? "(none)"} to ${it.product_label}?\n\nThis goes to every eBay listing linked to ${it.sku ?? `product ${it.product_id}`}.`)) return;
    await sendLabelFor(it.product_id);
  }

  async function loadZero() {
    try {
      const z = await api<{ items: { store: string; item_id: string; days_at_zero: number; held?: boolean }[] }>("/ebay-edit/zero/status");
      const m: Record<string, number> = {};
      const h: Record<string, boolean> = {};
      for (const x of z.items) {
        m[`${x.store.toLowerCase()}|${x.item_id}`] = x.days_at_zero;
        if (x.held) h[`${x.store.toLowerCase()}|${x.item_id}`] = true;
      }
      setZeroMap(m);
      setHeldMap(h);
    } catch {
      /* the badge is a convenience; the list still works without it */
    }
  }
  const isZero = (x: { store: string; item_id: string }) => `${x.store.toLowerCase()}|${x.item_id}` in zeroMap;

  const sortState = parseSort(sort);
  const onSort = (k: ListSortKey) => {
    const next = sortState.key === k ? `${k}:${sortState.dir === "asc" ? "desc" : "asc"}` : `${k}:asc`;
    setSort(next);
    void loadRepo(0, { store, q, sort: next, statusF });
  };

  // ---- reading the list: rows first, counts after, neighbouring pages ahead of time
  function scopeOf(st: string, qq: string, stat: string) {
    const storeKey = st === ALL ? "*all*" : st.trim().toLowerCase();
    return { storeKey, scope: `${storeKey}|${stat}|${qq.trim().toLowerCase()}` };
  }
  function scopeParams(st: string, qq: string, stat: string) {
    const params = new URLSearchParams({ q: qq.trim(), status: stat });
    if (st === ALL) params.set("all_stores", "1");
    else if (st.trim()) params.set("store", st.trim());
    return params;
  }
  // The rows of one page, nothing else (the server skips the whole-store counts for these).
  async function fetchPage(st: string, qq: string, so: string, stat: string, offset: number): Promise<RepoResp> {
    const params = scopeParams(st, qq, stat);
    params.set("sort", so);
    params.set("offset", String(offset));
    params.set("limit", String(PAGE_SIZE));
    params.set("fast", "1");
    const r = await api<RepoResp>(`/ebay-listings/repo?${params.toString()}`);
    const { scope, storeKey } = scopeOf(st, qq, stat);
    return { ...r, scope, storeKey };
  }
  const pageKey = (st: string, qq: string, so: string, stat: string, offset: number) => `${scopeOf(st, qq, stat).scope}|${so}|${offset}`;
  // Put the last known counts on a page of rows: exact when they were read for this same search, otherwise an estimate.
  function withMeta(r: RepoResp): RepoResp {
    const m = metaRef.current;
    const exact = !!m && m.scope === r.scope;
    const sameStore = !!m && m.storeKey === r.storeKey;
    return {
      ...r,
      total: exact ? m!.total : r.offset + r.items.length + (r.has_more ? 1 : 0),
      totalExact: exact,
      counts: sameStore ? m!.counts : undefined,
      statuses: sameStore ? m!.statuses : undefined,
    };
  }
  async function loadCounts(st: string, qq: string, stat: string) {
    const { scope, storeKey } = scopeOf(st, qq, stat);
    try {
      const c = await api<{ total: number; counts: RepoMeta["counts"]; statuses: RepoMeta["statuses"] }>(`/ebay-listings/repo/counts?${scopeParams(st, qq, stat).toString()}`);
      metaRef.current = { scope, storeKey, total: c.total, counts: c.counts, statuses: c.statuses };
      setRepo((prev) => (prev && prev.scope === scope ? withMeta(prev) : prev)); // only if that search is still the one on screen
    } catch {
      /* the rows are already showing; the numbers just stay as they were */
    }
  }
  // Quietly read the page after this one and the page before it, so Next / Previous are instant.
  async function prefetchNeighbours(r: RepoResp, st: string, qq: string, so: string, stat: string, myReq: number) {
    const wanted = [r.has_more ? r.offset + PAGE_SIZE : -1, r.offset > 0 ? Math.max(0, r.offset - PAGE_SIZE) : -1].filter((o) => o >= 0);
    for (const o of wanted) {
      if (myReq !== reqSeq.current) return; // something newer was asked for: stop
      const key = pageKey(st, qq, so, stat, o);
      if (pageCache.current.has(key)) continue;
      try {
        const pr = await fetchPage(st, qq, so, stat, o);
        pageCache.current.set(key, { t: Date.now(), r: pr });
      } catch {
        return;
      }
    }
  }

  // `cached: true` (used by Previous / Next / page box) may show a page read ahead of time; every other call reads fresh.
  async function loadRepo(offset: number, from?: { store: string; q: string; sort: string; statusF: string }, opts?: { cached?: boolean }) {
    const useStore = from ? from.store : store;
    const useQ = from ? from.q : q;
    const useSort = from ? from.sort : sort;
    const useStatus = from ? from.statusF : statusF;
    const myReq = ++reqSeq.current;
    lastQ.current = useQ.trim();
    setLoading(true);
    setError("");
    setSyncNote("");
    setPicked(new Set());
    setAllMatch(null);
    setGroupDone("");
    try {
      saveSaved("ebay-listings", { store: useStore, q: useQ, sort: useSort, offset, statusF: useStatus });
      const key = pageKey(useStore, useQ, useSort, useStatus, offset);
      const hit = opts?.cached ? pageCache.current.get(key) : undefined;
      let r: RepoResp;
      if (hit && Date.now() - hit.t < 60_000) {
        r = hit.r;
      } else {
        if (!opts?.cached) pageCache.current.clear(); // a fresh read means things may have changed: forget the old pages
        r = await fetchPage(useStore, useQ, useSort, useStatus, offset);
        pageCache.current.set(key, { t: Date.now(), r });
      }
      if (myReq === reqSeq.current) {
        setRepo(withMeta(r));
        void loadZero();
        // counts: skip when paging inside a search whose numbers are already known; otherwise read them now (rows are already showing)
        if (!opts?.cached || !metaRef.current || metaRef.current.scope !== r.scope) void loadCounts(useStore, useQ, useStatus);
        void prefetchNeighbours(r, useStore, useQ, useSort, useStatus, myReq);
      }
    } catch (e) {
      if (myReq === reqSeq.current) {
        setRepo(null);
        setError(errMsg(e));
      }
    } finally {
      if (myReq === reqSeq.current) setLoading(false);
    }
  }

  const allMode = !!repo && repo.store === ""; // the "All stores" view

  // which bulk buttons make sense: End listing for ticked listings that are active, Relist for ticked listings that are not
  const pickedItems = selectedItems();
  const nActivePicked = pickedItems.filter((x) => x.active).length;
  const nZeroPicked = pickedItems.filter((x) => x.active && isZero(x)).length;
  const nLivePicked = nActivePicked - nZeroPicked;
  const nInactivePicked = pickedItems.length - nActivePicked;
  const pickedLabels = pickedItems.map((x) => (x.custom_label ?? "").trim()).filter((v, i, a) => v && a.indexOf(v) === i);
  const oneLabel = pickedLabels.length === 1 ? pickedLabels[0] : "";

  // While the price box is open: look for the same custom labels in OTHER stores (database only). Only when they exist is the
  // "change those too" choice shown. Checks at most 30 labels.
  const pickedKey = priceOpen ? pickedItems.map((x) => x.listing_id).join(",") : "";
  useEffect(() => {
    if (!priceOpen || pickedLabels.length === 0) {
      setOthers([]);
      return;
    }
    const mySeq = ++othersSeq.current;
    const mine = pickedItems;
    void (async () => {
      const found: RepoItem[] = [];
      for (const label of pickedLabels.slice(0, 30)) {
        const lc = label.toLowerCase();
        const myStores = new Set(mine.filter((x) => (x.custom_label ?? "").trim().toLowerCase() === lc).map((x) => x.store.toLowerCase()));
        for (const x of await rowsWithLabel(label)) if (!myStores.has(x.store.toLowerCase())) found.push(x);
      }
      if (mySeq === othersSeq.current) setOthers(found);
    })().catch(() => {
      if (mySeq === othersSeq.current) setOthers([]);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [priceOpen, pickedKey]);
  const othersByStore = others.reduce<Record<string, number>>((m, x) => ((m[x.store] = (m[x.store] ?? 0) + 1), m), {});

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-semibold">Listings</h1>
        <p className="mt-1 max-w-3xl text-sm text-gray-600">
          Shows a store&rsquo;s eBay listings and which ones are products on the website. Opening a listing to edit it, or using
          Relist / End listing here, changes the real listing on eBay (only when live pushes are ON for that store). Stores
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
        <label className="block w-64 shrink-0 text-sm font-medium text-gray-700">
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
        <label className="block w-44 shrink-0 text-sm font-medium text-gray-700">
          <span className="mb-1 block">Show</span>
          <select
            className={inputCls}
            value={statusF}
            onChange={(e) => {
              const v = e.target.value as string;
              setStatusF(v);
              void loadRepo(0, { store, q, sort, statusF: v });
            }}
          >
            <option value="all">All{repo?.counts ? ` (${repo.counts.all})` : ""}</option>
            {(() => {
              // The usual eBay statuses are always offered (with 0 when none are on file), followed by any other
              // status found on the store's listings.
              const found = repo?.statuses ?? [];
              const usual = ["active", "ended", "draft"];
              const names = [...usual, ...found.map((x) => x.status).filter((n) => !usual.includes(n))];
              if (statusF !== "all" && !names.includes(statusF)) names.push(statusF);
              return names.map((n) => (
                <option key={n} value={n}>
                  {n.charAt(0).toUpperCase() + n.slice(1)} ({found.find((x) => x.status === n)?.count ?? 0})
                </option>
              ));
            })()}
          </select>
        </label>
        <label className="block w-full max-w-full text-sm font-medium text-gray-700 sm:w-[28rem] sm:shrink-0">
          <span className="mb-1 block whitespace-nowrap">Search title, item number, custom label or SKU</span>
          <ClearableInput
            className={inputCls}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onClear={() => {
              setQ("");
              void loadRepo(0, { store, q: "", sort, statusF });
            }}
            placeholder="e.g. Tusk 1.8 bar"
          />
        </label>
        <div className="block text-sm font-medium text-gray-700">
          <span className="mb-1 block">Order</span>
          <div className="flex items-center gap-2 py-2 text-sm font-normal text-gray-600">
            <span>{sortState.key ? `By ${{ title: "listing", store: "store", label: "custom label", price: "price", qty: "quantity", status: "status" }[sortState.key]}, ${sortState.dir === "asc" ? "low to high" : "high to low"}` : "Newest first"}</span>
            {sortState.key && (
              <button
                type="button"
                className="text-xs underline"
                onClick={() => {
                  setSort("newest");
                  void loadRepo(0, { store, q, sort: "newest", statusF });
                }}
              >
                Newest first
              </button>
            )}
          </div>
        </div>
      </form>

      {/* ---- listings from our database */}
      {repo && (
        <div className="space-y-3">
          <Pager total={repo.total} limit={repo.limit} offset={repo.offset} onChange={(o) => void loadRepo(o, undefined, { cached: true })} />
          <p className="text-sm text-gray-700">
            <span className="font-medium">{allMode ? "All stores" : repo.store}</span> in our database: {repo.counts ? <>{repo.counts.all} listing{repo.counts.all === 1 ? "" : "s"}, {repo.counts.active} active, {repo.counts.inactive} inactive.</> : "counting…"}
            {repo.statuses && repo.statuses.length > 0 && (
              <span className="ml-1 text-xs text-gray-500">
                Statuses on file: {repo.statuses.map((x) => `${x.status} (${x.count})`).join(", ")}.
              </span>
            )}
          </p>
          {(() => {
            const rel = sync.filter((x) => (allMode || store === ALL ? true : x.store.toLowerCase() === (repo.store || store).toLowerCase()));
            if (rel.length === 0) return null;
            const running = rel.some((x) => x.running);
            const failed = rel.filter((x) => !x.running && x.connected && x.status === "failed");
            const idle = rel.filter((x) => !x.connected);
            const done = rel.filter((x) => x.connected && x.finished_at);
            const oldest = done.length ? done.map((x) => x.finished_at as string).sort()[0] : null;
            return (
              <p className="text-xs text-gray-500">
                {running
                  ? "Checking statuses with eBay in the background…"
                  : oldest
                    ? `Statuses last checked with eBay ${ago(oldest)}.`
                    : "Statuses have not been checked with eBay yet (a check runs once a day)."}
                {failed.length > 0 && ` The last check of ${failed.map((x) => x.store).join(", ")} did not finish (${failed[0].message ?? "unknown reason"}).`}
                {idle.length > 0 && ` Not connected, so not checked: ${idle.map((x) => x.store).join(", ")}.`}
                {!running && (
                  <>
                    {" "}
                    <button type="button" className="underline" onClick={() => void runSyncNow()}>
                      Check with eBay now
                    </button>
                  </>
                )}
              </p>
            );
          })()}
          {syncNote && (
            <p className="text-xs font-medium text-amber-700">
              {syncNote}{" "}
              <button type="button" className="underline" onClick={() => void loadRepo(repo.offset)}>
                Refresh the list
              </button>
            </p>
          )}
          {actMsg && <Notice kind={actMsg.kind}>{actMsg.text}</Notice>}
          {afterRelink && !relink && (
            <div className="flex flex-wrap items-center gap-3 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm">
              <span>
                The Custom label column still shows the label last read from eBay. To make eBay match <b>{afterRelink.sku ?? `product ${afterRelink.product_id}`}</b>, send its
                label <b>{afterRelink.label}</b> now. This goes to every eBay listing linked to that product.
              </span>
              <button type="button" className={btnCls} disabled={relinkBusy} onClick={() => void sendLabelNow()}>
                {relinkBusy ? "Sending…" : "Send label to eBay"}
              </button>
              <button type="button" className={btn2Cls} disabled={relinkBusy} onClick={() => setAfterRelink(null)}>
                Not now
              </button>
            </div>
          )}
          {relink && (
            <div className="space-y-2 rounded-lg border-2 border-amber-300 bg-amber-50 p-3">
              <div className="flex items-start justify-between gap-3">
                <p className="text-sm">
                  Change which product listing <b>{relink.item_id}</b> points at. It now points at <b>{relink.sku ?? `product ${relink.product_id}`}</b>
                  {relink.product_label ? ` (label ${relink.product_label})` : ""}. The label last read from eBay for this listing is{" "}
                  <b>{relink.custom_label ?? "not known"}</b> (a saved copy, so it can be out of date until the label is sent or eBay is read again).
                </p>
                <button type="button" className={btn2Cls} disabled={relinkBusy} onClick={() => { setRelink(null); setRelinkTo(null); }}>
                  Cancel
                </button>
              </div>
              {relinkTo ? (
                <div className="flex flex-wrap items-center gap-2 rounded border border-gray-300 bg-white p-2 text-sm">
                  <span>
                    Point it at <b>{relinkTo.sku}</b> — {relinkTo.title}?
                  </span>
                  <button type="button" className={btnCls} disabled={relinkBusy} onClick={() => void doRelink()}>
                    {relinkBusy ? "Saving…" : "Change product"}
                  </button>
                  <button type="button" className={btn2Cls} disabled={relinkBusy} onClick={() => setRelinkTo(null)}>
                    Pick a different one
                  </button>
                </div>
              ) : (
                <ProductPicker onPick={setRelinkTo} />
              )}
              <p className="text-xs text-gray-600">Only the link on this site changes. Nothing is sent to eBay, and past sales and orders stay as they are.</p>
            </div>
          )}
          {!allMatch && repo.totalExact && repo.total > repo.items.length && repo.items.length > 0 && repo.items.every((x) => picked.has(x.listing_id)) && (
            <div className="rounded border border-blue-200 bg-blue-50 px-3 py-2 text-sm">
              All {repo.items.length} listings on this page are selected.{" "}
              <button type="button" className="font-medium text-blue-800 underline" disabled={loadingAll} onClick={() => void selectAllMatching()}>
                {loadingAll ? "Selecting…" : `Select all ${repo.total} matching listings`}
              </button>
            </div>
          )}
          {drafts.length > 0 && (
            <details className="rounded border border-gray-200 bg-white px-3 py-2 text-sm">
              <summary className="cursor-pointer font-medium">Sell similar drafts in progress ({drafts.length})</summary>
              <ul className="mt-2 space-y-1">
                {drafts.map((d) => (
                  <li key={d.id}>
                    <Link href={`/admin/ebay-copy?id=${d.id}`} className="text-blue-700 hover:underline">{d.title || `Draft ${d.id}`}</Link>
                    <span className="ml-2 text-xs text-gray-500">{d.store} · copy of {d.source_item_id}</span>
                  </li>
                ))}
              </ul>
            </details>
          )}
          {picked.size > 0 && (
            <div className="flex flex-wrap items-center gap-3 rounded border border-gray-200 bg-gray-50 px-3 py-2 text-sm">
              <span className="font-medium">{pickedItems.length} selected{allMatch ? " (all matching)" : ""}</span>
              <button type="button" className={btnCls} disabled={busy === "status"} onClick={() => setPriceOpen((v) => !v)} title="Set one new price on everything selected. Active listings are sent to eBay after you confirm (live pushes must be ON).">
                Change price
              </button>
              {oneLabel && groupDone !== oneLabel && (
                <button type="button" className={btn2Cls} disabled={loadingAll || busy === "status"} onClick={() => void selectLabelGroup(oneLabel)} title="Ticks every listing (any status) with exactly this custom label">
                  {loadingAll ? "Selecting…" : `Select all with label ${oneLabel}`}
                </button>
              )}
              {pickedItems.length === 1 && (
                <button type="button" className={btn2Cls} disabled={busy === "copy" || busy === "status"} onClick={() => void copyOne()} title="Opens a draft copy of this listing to edit, then list as a new eBay listing linked to the same product">
                  {busy === "copy" ? "Opening…" : "Sell similar"}
                </button>
              )}
              {nInactivePicked > 0 && (
                <button type="button" className={btn2Cls} disabled={busy === "status"} onClick={() => void bulkSet("active")}>
                  Relist on eBay ({nInactivePicked})
                </button>
              )}
              {nLivePicked > 0 && (
                <button type="button" className={btn2Cls} disabled={busy === "status"} onClick={() => void bulkZero()} title="Keeps the listing alive at quantity 0 (needs eBay's out-of-stock setting ON)">
                  Set 0 Qty on eBay ({nLivePicked})
                </button>
              )}
              {nZeroPicked > 0 && (
                <>
                  <button type="button" className={btn2Cls} disabled={busy === "status"} onClick={() => void bulkRestore(false)} title="Puts the listing back on sale at the available stock shown on the website. Only the selected listings that are at 0 are changed.">
                    Restore stock on eBay ({nZeroPicked})
                  </button>
                  <button type="button" className={btn2Cls} disabled={busy === "status"} onClick={() => void bulkRestore(true)} title="Sets quantity 1 on eBay, which restarts the zero-days count. Only the selected listings that are at 0 are changed.">
                    Restart Zero-Days ({nZeroPicked})
                  </button>
                </>
              )}
              {nActivePicked > 0 && (
                <button type="button" className={`${btn2Cls} text-red-700`} disabled={busy === "status"} onClick={() => void bulkSet("inactive")} title="Ends the listing on eBay. For parts you will not sell again.">
                  End listing on eBay ({nActivePicked})
                </button>
              )}
              <button type="button" className={`${btn2Cls} text-red-700`} disabled={busy === "status"} onClick={() => openDelete()} title="Deletes the website's record of the listing. Nothing is sent to eBay: a listing that is live there stays live. A preview and a typed confirmation come first.">
                Delete ({pickedItems.length})
              </button>
              <button type="button" className="text-xs underline" onClick={() => { setPicked(new Set()); setAllMatch(null); setGroupDone(""); setPriceOpen(false); }}>
                Clear
              </button>
              <span className="text-xs text-gray-500">Sends to eBay one at a time (needs live pushes ON for each store involved).</span>
            </div>
          )}
          {delOpen && (
            <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" role="dialog" aria-modal="true">
              <div className="flex max-h-[90vh] w-full max-w-2xl flex-col space-y-3 overflow-hidden rounded-lg bg-white p-4 shadow-xl">
                <h2 className="text-lg font-semibold text-red-700">Delete listing records</h2>
                <Notice kind="error">This removes the listing from this website&apos;s database only. It does NOT end or delete anything on eBay.</Notice>
                {!delPv && delBusy && <p className="text-sm text-gray-600">Checking what would be deleted&hellip;</p>}
                {delPv && (
                  <div className="min-h-0 flex-1 space-y-3 overflow-y-auto text-sm">
                    {delPv.deletable.length > 0 ? (
                      <div>
                        <p className="font-medium">These {delPv.deletable.length} listing record{delPv.deletable.length === 1 ? "" : "s"} will be deleted:</p>
                        <ul className="mt-1 max-h-48 space-y-0.5 overflow-y-auto rounded border border-gray-200 bg-gray-50 p-2">
                          {delPv.deletable.map((d) => (
                            <li key={d.listing_id}>
                              <span className="font-mono text-xs text-gray-500">{d.item_id}</span> {d.title || "(no title)"}
                              <span className="text-xs text-gray-500"> · {d.store} · {d.status || "no status"}{d.sku ? ` · product ${d.sku}` : ""}</span>
                              {d.active && <span className="text-xs font-medium text-red-700"> · STILL ACTIVE: stays live on eBay</span>}
                            </li>
                          ))}
                        </ul>
                        <p className="mt-1 text-xs text-gray-600">
                          Also removed: each listing&apos;s unsent edits and its saved eBay copy. The product, its photos and its stock are kept
                          (delete the product afterwards from the Products page if you want it gone). This cannot be undone.
                        </p>
                      </div>
                    ) : (
                      <Notice kind="error">None of the ticked listings will be deleted.</Notice>
                    )}
                    {delPv.skipped_active.length > 0 && (
                      <div className="space-y-1">
                        <p className="font-medium text-amber-800">{delPv.skipped_active.length} still marked active, so they are being left alone:</p>
                        <ul className="max-h-32 space-y-0.5 overflow-y-auto rounded border border-amber-200 bg-amber-50 p-2">
                          {delPv.skipped_active.map((d) => (
                            <li key={d.listing_id}>
                              <span className="font-mono text-xs text-gray-500">{d.item_id}</span> {d.title || "(no title)"}
                              <span className="text-xs text-amber-800"> · {d.store}</span>
                            </li>
                          ))}
                        </ul>
                        <p className="text-xs text-gray-600">Best: use End listing on eBay first, then delete the record.</p>
                        <label className="flex items-start gap-2 text-xs">
                          <input type="checkbox" className="mt-0.5" checked={delActive} disabled={delBusy}
                            onChange={(e) => { setDelActive(e.target.checked); setDelTyped(""); void loadDeletePreview(e.target.checked); }} />
                          <span>Delete these records anyway. The listings stay live on eBay, and a later eBay sync or import may add them back.</span>
                        </label>
                      </div>
                    )}
                    {delPv.missing.length > 0 && <p className="text-xs text-gray-500">{delPv.missing.length} ticked listing(s) no longer exist.</p>}
                    {delPv.deletable.length > 0 && (
                      <label className="block text-sm font-medium text-gray-700">
                        <span className="mb-1 block">Type DELETE to confirm</span>
                        <input className={`${inputCls} w-48`} value={delTyped} autoFocus placeholder="DELETE" onChange={(e) => setDelTyped(e.target.value)} disabled={delBusy} />
                      </label>
                    )}
                  </div>
                )}
                {delErr && <Notice kind="error">{delErr}</Notice>}
                <div className="flex justify-end gap-2">
                  <button type="button" className="rounded border border-gray-300 px-3 py-2 text-sm" disabled={delBusy} onClick={() => setDelOpen(false)}>Cancel</button>
                  {delPv && delPv.deletable.length > 0 && (
                    <button type="button" className="rounded bg-red-700 px-3 py-2 text-sm font-semibold text-white hover:bg-red-800 disabled:opacity-50"
                      disabled={delBusy || delTyped.trim().toUpperCase() !== "DELETE"} onClick={() => void confirmDelete()}>
                      {delBusy ? "Working…" : `Delete ${delPv.deletable.length} record${delPv.deletable.length === 1 ? "" : "s"}`}
                    </button>
                  )}
                </div>
              </div>
            </div>
          )}
          {picked.size > 0 && priceOpen && (
            <div className="space-y-2 rounded border border-blue-200 bg-blue-50 px-3 py-2 text-sm">
              <div className="flex flex-wrap items-end gap-3">
                <label className="block text-sm font-medium text-gray-700">
                  <span className="mb-1 block">New price for the {pickedItems.length} selected listing{pickedItems.length === 1 ? "" : "s"}</span>
                  <input
                    className={`${inputCls} w-32`}
                    inputMode="decimal"
                    placeholder="24.99"
                    value={priceText}
                    onChange={(e) => setPriceText(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter" && priceText.trim() && busy !== "status") void bulkPrice(); }}
                  />
                </label>
                <button type="button" className={btnCls} disabled={busy === "status" || !priceText.trim()} onClick={() => void bulkPrice()}>
                  {busy === "status" ? "Working…" : "Continue"}
                </button>
                <button type="button" className="text-xs underline" onClick={() => setPriceOpen(false)}>Cancel</button>
              </div>
              <label className="flex items-center gap-2 text-gray-700">
                <input type="checkbox" checked={setSite} onChange={(e) => setSetSite(e.target.checked)} />
                Also set the website&rsquo;s single-unit price for these parts (leave unticked to change eBay only)
              </label>
              {others.length > 0 && (
                <label className="flex items-center gap-2 font-medium text-gray-800">
                  <input type="checkbox" checked={includeOthers} onChange={(e) => setIncludeOthers(e.target.checked)} />
                  <span>
                    The same custom label is also on {others.length} listing{others.length === 1 ? "" : "s"} in other stores (
                    {Object.entries(othersByStore).map(([nm, c]) => `${nm}: ${c}`).join(", ")}). Change those to this price too?
                  </span>
                </label>
              )}
              <p className="text-xs text-gray-500">One confirmation, then active listings are sent to eBay one at a time. Ended listings only get the price saved on the website.</p>
            </div>
          )}
          {repo.items.length === 0 ? (
            <Notice>No listings{statusF !== "all" ? ` with status "${statusF}"` : ""}{q.trim() ? " match that search" : ""} for {allMode ? "these stores" : "this store"}.</Notice>
          ) : (
            <div className="rounded border border-gray-200 bg-white">
              {/* Fits the screen with no sideways scroll. Every column has a fixed width sized for its longest content (same on every page, nothing wraps),
                  and only the Listing column (the flexible one) cuts off long text. To widen or narrow a column, change its width in this list. */}
              <table className="w-full table-fixed text-left text-sm">
                <colgroup>
                  <col className="w-[2rem]" /> {/* checkbox */}
                  <col className="w-[4rem]" /> {/* photo */}
                  <col /> {/* Listing: takes the rest */}
                  <col className="w-[9.5rem]" /> {/* Store */}
                  <col className="w-[19rem]" /> {/* Custom label (room for a 3-part kit label) */}
                  <col className="w-[6.5rem]" /> {/* Price */}
                  <col className="w-[10.5rem]" /> {/* Quantity */}
                  <col className="w-[11.5rem]" /> {/* Status */}
                </colgroup>
                <thead className="bg-gray-50 text-xs uppercase text-gray-500">
                  <tr>
                    <th className="px-2 py-2">
                      <input
                        type="checkbox"
                        aria-label="Select all on this page"
                        checked={repo.items.length > 0 && repo.items.every((x) => picked.has(x.listing_id))}
                        onChange={(e) => {
                          setAllMatch(null);
                          setPicked(e.target.checked ? new Set(repo.items.map((x) => x.listing_id)) : new Set());
                        }}
                      />
                    </th>
                    <th className="px-2 py-2"></th>
                    <SortTh label="Listing" k="title" sort={sortState} onSort={onSort} className="px-2 py-2" />
                    <SortTh label="Store" k="store" sort={sortState} onSort={onSort} className="px-2 py-2" />
                    <SortTh label="Custom label" k="label" sort={sortState} onSort={onSort} className="px-2 py-2" />
                    <SortTh label="Price" k="price" sort={sortState} onSort={onSort} className="px-2 py-2" />
                    <SortTh label="Quantity" k="qty" sort={sortState} onSort={onSort} className="px-2 py-2" />
                    <th className="px-2 py-2" title="How many units of the product one sale of this listing uses (a 3-pack = 3). Applies to every order, drop shipped or not.">Pieces per sale</th>
                    <SortTh label="Status" k="status" sort={sortState} onSort={onSort} className="px-2 py-2" />
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {repo.items.map((it) => {
                    const rowStore = stores.find((x) => x.store.toLowerCase() === it.store.toLowerCase());
                    const canEdit = rowStore?.authorized;
                    const editHref = `/admin/ebay-edit?store=${encodeURIComponent(it.store)}&item=${it.item_id}`;
                    return (
                      <ClickRow key={it.listing_id} className="align-top" href={canEdit ? editHref : undefined}>
                        <td className="px-2 py-2">
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
                        <td className="px-2 py-2">
                          {it.image_url ? (
                            // eslint-disable-next-line @next/next/no-img-element
                            <img src={it.image_url} alt="" className="h-12 w-12 rounded object-cover" />
                          ) : (
                            <div className="h-12 w-12 rounded bg-gray-100" />
                          )}
                        </td>
                        <td className="overflow-hidden px-2 py-2">
                          <div className="truncate" title={it.title}>{it.title}</div>
                          <div className="truncate">
                          <a href={it.url} target="_blank" rel="noreferrer" className="text-blue-700 hover:underline">
                            {it.item_id}
                          </a>
                          <Link href={`/admin/products/${it.product_id}`} className="ml-3 text-blue-700 hover:underline">
                            {it.sku ?? `Product ${it.product_id}`}
                          </Link>
                          <button
                            type="button"
                            className="ml-2 text-xs text-gray-600 underline hover:text-black"
                            title="Point this listing at a different product"
                            onClick={() => { setRelink(it); setRelinkTo(null); setActMsg(null); window.scrollTo({ top: 0, behavior: "smooth" }); }}
                          >
                            Change product
                          </button>
                          {it.product_label && it.custom_label && it.product_label.trim().toLowerCase() !== it.custom_label.trim().toLowerCase() && (
                            <span className="ml-2" title={`The label last read from eBay is ${it.custom_label}, but the product it points at has ${it.product_label}. The saved copy can be out of date; sending the label from the product page refreshes it.`}>
                              <Badge tone="amber">Label differs from product</Badge>
                            </span>
                          )}
                          {it.is_product_source && (
                            <span className="ml-3" title="The product's photo gallery is taken from this listing">
                              <Badge tone="gray">Product photos</Badge>
                            </span>
                          )}
                          </div>
                        </td>
                        <td className="whitespace-nowrap px-2 py-2">{rowStore?.display_name || it.store}</td>
                        <td className="whitespace-nowrap px-2 py-2">{it.custom_label ?? "—"}</td>
                        <td className="whitespace-nowrap px-2 py-2">{money(it.price, it.currency)}</td>
                        <td className="whitespace-nowrap px-2 py-2">
                          {it.quantity ?? "—"}
                          {it.active && isZero(it) && (
                            <span className="ml-1" title="Quantity is 0 on eBay and the listing stays alive. Held = set to 0 on purpose: sales will not push stock onto it until more stock is received or adjusted.">
                              <Badge tone={heldMap[`${it.store.toLowerCase()}|${it.item_id}`] ? "amber" : "gray"}>
                                {heldMap[`${it.store.toLowerCase()}|${it.item_id}`] ? "Held at 0" : "0 on eBay"} · {zeroMap[`${it.store.toLowerCase()}|${it.item_id}`]}d
                              </Badge>
                            </span>
                          )}
                          {it.quantity_from_ebay && <span className="ml-1 text-xs text-gray-500" title="Our stock on hand is 0; this is the quantity eBay shows as available">(eBay)</span>}
                        </td>
                        <td className="whitespace-nowrap px-2 py-2">
                          <PiecesBox
                            listingId={it.listing_id}
                            value={it.pieces_per_sale ?? 1}
                            onError={(t) => setActMsg({ kind: "error", text: t })}
                            onSaved={(pc, queued) => {
                              setRepo((cur) => (cur ? { ...cur, items: cur.items.map((x) => (x.listing_id === it.listing_id ? { ...x, pieces_per_sale: pc } : x)) } : cur));
                              setActMsg({ kind: "success", text: `${it.item_id}: ${pc} piece${pc === 1 ? "" : "s"} per sale.${queued ? " The eBay quantity is queued to update." : ""} Reload to see the new quantity.` });
                            }}
                          />
                        </td>
                        <td className="whitespace-nowrap px-2 py-2">
                          <Badge tone={it.active ? "green" : "gray"}>{it.status || "no status"}</Badge>
                          {it.unsent_edits && (
                            <span className="ml-1">
                              <Badge tone="amber">Unsent edits</Badge>
                            </span>
                          )}
                          {it.product_label && it.custom_label && it.product_label.trim().toLowerCase() !== it.custom_label.trim().toLowerCase() && (
                            <button
                              type="button"
                              className="ml-2 rounded border border-amber-400 bg-amber-50 px-2 py-0.5 text-xs text-amber-900 hover:bg-amber-100 disabled:opacity-50"
                              disabled={relinkBusy}
                              title={`eBay has ${it.custom_label}; the product has ${it.product_label}. Send the product's label to eBay.`}
                              onClick={() => void fixRowLabel(it)}
                            >
                              Update eBay label
                            </button>
                          )}
                        </td>
                      </ClickRow>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          <Pager total={repo.total} limit={repo.limit} offset={repo.offset} onChange={(o) => void loadRepo(o, undefined, { cached: true })} />
        </div>
      )}

    </div>
  );
}
