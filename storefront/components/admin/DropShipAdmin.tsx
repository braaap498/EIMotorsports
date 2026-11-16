"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Badge, Notice, Pager, btnCls, btn2Cls, inputCls } from "./ui";
import { PoReview, type DsJob } from "./DropShipReview";
import DropShipActivity from "./DropShipActivity";

/* Drop ship: every drop ship setting in one place.
   - the switches (count the distributor's stock, raise listings, make POs automatically)
   - Products and listings: each product folds open to show its eBay listings, each with its own Drop ship tick (the same
     tick as on the New listing and Live listing screens). Switching the whole product ON ticks every listing; switching it
     OFF either puts the listings back as they were or unticks them all.
   - Orders: the drop ship orders in progress, with the temporary check popups. */

type Settings = { auto_po: boolean; manual_checks: boolean; last_scan_at: string | null; last_scan_note: string | null };
type Avail = { use_distributor_stock: boolean; auto_raise: boolean; display_default: string };
type Listing = { listing_id: number; store: string; item_id: string; dropship_ok: boolean };
type Item = {
  product_id: number;
  sku: string | null;
  title: string | null;
  custom_label: string | null;
  on_hand: number;
  own_available: number;
  distributor: number;
  piece_count: number;
  dropship_ok: boolean;
  has_prev: boolean;
  ticked: number;
  listings: Listing[];
};
type Page = { total: number; items: Item[] };

const PER = 40;
const REVIEW = ["review_po", "po_confirmed", "review_quote", "review_tracking"];
const STAGE: Record<string, string> = {
  review_po: "Waiting for you to check the PO",
  po_confirmed: "PO checked; quote not fetched",
  review_quote: "Waiting for you to check the quote",
  placing: "Placing the order…",
  placed: "Order placed; waiting for tracking",
  review_tracking: "Waiting for you to check the tracking",
  done: "Done: eBay told it shipped",
  held: "On hold",
  problem: "Needs attention",
};

export default function DropShipAdmin() {
  const [tab, setTab] = useState<"products" | "orders" | "activity">("products");
  const [settings, setSettings] = useState<Settings | null>(null);
  const [avail, setAvail] = useState<Avail | null>(null);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);

  const loadSettings = useCallback(async () => {
    try {
      const [s, a] = await Promise.all([api<Settings>("/dropship/settings"), api<Avail>("/availability/settings")]);
      setSettings(s);
      setAvail(a);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    }
  }, []);
  useEffect(() => {
    void loadSettings();
  }, [loadSettings]);

  async function run<T>(key: string, fn: () => Promise<T>, ok: (r: T) => string, after?: () => Promise<void> | void) {
    setBusy(key);
    setMsg(null);
    try {
      const r = await fn();
      setMsg({ kind: "success", text: ok(r) });
      await loadSettings();
      if (after) await after();
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  const saveAvail = (patch: Partial<Avail>) =>
    run("avail", () => api<Avail>("/availability/settings", { method: "PUT", body: patch }), () => "Saved.");

  const tabBtn = (k: "products" | "orders" | "activity", label: string) => (
    <button
      type="button"
      onClick={() => setTab(k)}
      className={`rounded px-3 py-2 text-sm font-medium ${tab === k ? "bg-black text-white" : "border border-gray-300 bg-white text-gray-800 hover:bg-gray-50"}`}
    >
      {label}
    </button>
  );

  return (
    <div className="max-w-5xl space-y-4">
      <div>
        <h1 className="text-xl font-semibold">Drop ship</h1>
        <p className="text-sm text-gray-600">
          A ticked listing is drop shipped automatically: an order for it that is short on our shelf makes its own PO. An unticked listing
          asks you on Pick &amp; ship whether to drop ship it or ship it from our own stock.
        </p>
      </div>

      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}

      <div className="space-y-3 rounded border border-gray-200 bg-white p-4 text-sm">
        {avail && (
          <>
            <label className="flex items-start gap-2">
              <input type="checkbox" className="mt-1" checked={avail.use_distributor_stock} disabled={busy !== ""} onChange={(e) => void saveAvail({ use_distributor_stock: e.target.checked })} />
              <span>
                <span className="font-medium">Count distributor stock</span>
                <span className="block text-xs text-gray-500">
                  Automatic Distributors, Indiana + Nevada. A single-part product is available if we have it or the distributor does. A kit is
                  available from our shelf only, unless it is ticked below. When this is OFF everything works from our own shelf count. If neither
                  we nor the distributor has it, the listing is 0.
                </span>
              </span>
            </label>
            <label className="flex items-start gap-2">
              <input type="checkbox" className="mt-1" checked={avail.auto_raise} disabled={busy !== "" || !avail.use_distributor_stock} onChange={(e) => void saveAvail({ auto_raise: e.target.checked })} />
              <span>
                <span className="font-medium">Raise listings automatically</span>
                <span className="block text-xs text-gray-500">When the distributor gets stock back, a listing that went to 0 goes back up. When OFF it stays at 0.</span>
              </span>
            </label>
          </>
        )}
        {settings && (
          <>
            <label className="flex items-start gap-2 border-t border-gray-100 pt-3">
              <input
                type="checkbox"
                className="mt-1"
                checked={settings.auto_po}
                disabled={busy !== ""}
                onChange={(e) =>
                  void run("auto", () => api<Settings>("/dropship/settings", { method: "PUT", body: { auto_po: e.target.checked } }), (r) =>
                    r.auto_po ? "Ticked listings will now make their PO by themselves." : "Automatic POs are off."
                  )
                }
              />
              <span>
                <span className="font-medium">Make POs automatically</span>
                <span className="block text-xs text-gray-500">
                  Each time the eBay orders are checked, a paid order from the last 10 days gets its PO when its listing is ticked and our shelf is short.
                  Global Shipping Program orders and incomplete addresses are never made; they show under Orders as needing attention.
                </span>
              </span>
            </label>
            <div className="rounded border border-amber-200 bg-amber-50 p-3">
              <label className="flex items-start gap-2">
                <input
                  type="checkbox"
                  className="mt-1"
                  checked={settings.manual_checks}
                  disabled={busy !== ""}
                  onChange={(e) =>
                    void run("manual", () => api<Settings>("/dropship/settings", { method: "PUT", body: { manual_checks: e.target.checked } }), (r) =>
                      r.manual_checks
                        ? "Manual checks are ON: drop ship orders stop at the PO, quote and tracking steps for you."
                        : "Manual checks are OFF: drop ship orders now run all the way by themselves."
                    )
                  }
                />
                <span>
                  <span className="font-medium">Manual checks (for testing)</span>
                  <span className="block text-xs text-gray-600">
                    OFF (normal): every drop ship order runs by itself, start to finish. ON: it stops at the three checks below and waits for you.
                  </span>
                </span>
              </label>
              <ol className="mt-2 list-decimal space-y-1 pl-9 text-xs text-gray-700">
                <li>
                  <span className="font-medium">PO check.</span> The PO is made from the eBay order and waits for you to look at it. Your OK gets the quote from
                  Automatic Distributors. OFF: the PO is accepted and the quote is fetched at once.
                </li>
                <li>
                  <span className="font-medium">Quote check and place the order.</span> The quote waits for you to look at the price and the shipping option (you can
                  change it), then the button places the real order (owner or admin only). OFF: the shipping option closest to what the buyer picked on eBay is used
                  and the order is placed at once, still under the test/live server and live switch safeguards.
                </li>
                <li>
                  <span className="font-medium">Tracking check.</span> When the distributor's tracking arrives it waits for you to check it, then the button tells
                  eBay the order shipped (eBay notifies the buyer). OFF: eBay is told the moment the tracking arrives, and the order is marked shipped and leaves Pick
                  &amp; ship.
                </li>
              </ol>
              <p className="mt-2 text-xs text-gray-600">
                Either way an order stops for a person if something goes wrong (no matching shipping option, a refused quote or order, eBay refusing the tracking) and
                shows the reason under Orders. It is never retried by itself. "Make POs automatically" above still decides whether new eBay orders start this at all,
                and the automatic tracking check on the Distributor data feed tab still has to be on.
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <button type="button" className={btn2Cls} disabled={busy !== ""} onClick={() => void run("scan", () => api<{ note: string }>("/dropship/scan", { method: "POST" }), (r) => r.note)}>
                {busy === "scan" ? "Checking…" : "Check for new orders and tracking now"}
              </button>
              {settings.last_scan_note && <span className="text-xs text-gray-500">Last check: {settings.last_scan_note}</span>}
            </div>
          </>
        )}
      </div>

      <div className="flex gap-2">
        {tabBtn("products", "Products and listings")}
        {tabBtn("orders", "Orders")}
        {tabBtn("activity", "Activity")}
      </div>

      {tab === "products" ? <ProductsTab avail={avail} setMsg={setMsg} /> : tab === "activity" ? <DropShipActivity /> : <OrdersTab busy={busy} run={run} setMsg={setMsg} />}
    </div>
  );
}

/* ------------------------------------------------------------------ products and listings */

function ProductsTab({ avail, setMsg }: { avail: Avail | null; setMsg: (m: { kind: "error" | "success"; text: string } | null) => void }) {
  const [q, setQ] = useState("");
  const [applied, setApplied] = useState("");
  const [show, setShow] = useState<"all" | "on" | "off" | "zero">("all");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<Page | null>(null);
  const [open, setOpen] = useState<Set<number>>(new Set());
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const qs = new URLSearchParams({ show, page: String(Math.floor(offset / PER) + 1), per_page: String(PER) });
      if (applied.trim()) qs.set("q", applied.trim());
      setData(await api<Page>(`/availability/products?${qs}`));
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setLoading(false);
    }
  }, [show, applied, offset, setMsg]);
  useEffect(() => {
    void load();
  }, [load]);

  const toggleOpen = (id: number) =>
    setOpen((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  async function post(path: string, body: unknown, ok: (r: { queued?: number }) => string) {
    setBusy(true);
    setMsg(null);
    try {
      const r = await api<{ queued?: number }>(path, { method: "POST", body });
      setMsg({ kind: "success", text: ok(r) });
      await load();
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy(false);
    }
  }
  const queuedText = (r: { queued?: number }) => (r.queued ? ` ${r.queued} eBay listing${r.queued === 1 ? "" : "s"} queued to update.` : "");

  const tickListing = (l: Listing, on: boolean) =>
    post(`/availability/listings/${l.listing_id}/dropship`, { on }, (r) => `Drop ship ${on ? "on" : "off"} for ${l.store} ${l.item_id}.${queuedText(r)}`);
  const productOn = (it: Item) => {
    if (it.ticked > 0 && it.ticked < it.listings.length && !window.confirm("This ticks EVERY listing of the product and overwrites the ones set one by one. Their earlier ticks are remembered so you can put them back. Continue?")) return;
    void post(`/availability/products/${it.product_id}/dropship`, { on: true }, (r) => `Every listing of this product is now drop shipped.${queuedText(r)}`);
  };
  const productOff = (it: Item, mode: "restore" | "reset") =>
    post(`/availability/products/${it.product_id}/dropship`, { on: false, off_mode: mode }, (r) =>
      (mode === "restore" ? "Listings put back the way they were." : "Every listing of this product is unticked.") + queuedText(r)
    );

  async function applyZeros() {
    if (!window.confirm("Queue an eBay quantity update of 0 for every listing whose product has nothing to sell now? Only stores with live pushes and sales quantity updates ON are touched.")) return;
    await post("/availability/apply-zeros", {}, () => "Queued. They are sent within about a minute.");
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-2">
        <select className={`${inputCls} w-auto`} value={show} onChange={(e) => { setShow(e.target.value as typeof show); setOffset(0); }}>
          <option value="all">All products with an eBay listing</option>
          <option value="on">Drop ship ticked</option>
          <option value="off">Not ticked</option>
          <option value="zero">At 0 (nothing to sell)</option>
        </select>
        <input
          className={`${inputCls} w-64`}
          placeholder="Search title, SKU, label"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") { setApplied(q); setOffset(0); } }}
        />
        <button type="button" className={btn2Cls} onClick={() => { setApplied(q); setOffset(0); }}>Search</button>
        <button type="button" className={btn2Cls} disabled={busy || !avail?.use_distributor_stock} title={avail?.use_distributor_stock ? "" : "Turn on Count distributor stock first"} onClick={() => void applyZeros()}>
          Set listings to 0 now
        </button>
        <button type="button" className="text-xs underline" onClick={() => setOpen(open.size ? new Set() : new Set((data?.items ?? []).map((i) => i.product_id)))}>
          {open.size ? "Fold all" : "Open all on this page"}
        </button>
      </div>

      {data && <Pager total={data.total} limit={PER} offset={offset} onChange={setOffset} />}
      {loading && <p className="text-sm text-gray-500">Loading…</p>}

      <div className="space-y-2">
        {data?.items.map((it) => {
          const isOpen = open.has(it.product_id);
          const n = it.listings.length;
          return (
            <div key={it.product_id} className="rounded border border-gray-200 bg-white text-sm">
              <button type="button" className="flex w-full items-start gap-2 p-3 text-left" onClick={() => toggleOpen(it.product_id)} aria-expanded={isOpen}>
                <span className="mt-0.5 w-4 text-gray-500">{isOpen ? "▾" : "▸"}</span>
                <span className="min-w-0 flex-1">
                  <span className="block font-medium">{it.title}</span>
                  <span className="block text-xs text-gray-500">
                    {it.sku}
                    {it.custom_label ? ` · ${it.custom_label}` : ""} · shelf {it.on_hand} · distributor {it.distributor}
                  </span>
                </span>
                <span className="flex flex-wrap justify-end gap-1">
                  <Badge tone="gray">{it.piece_count > 1 ? `Kit of ${it.piece_count}` : it.piece_count === 1 ? "Single part" : "No distributor part"}</Badge>
                  {it.dropship_ok ? <Badge tone="amber">Whole product</Badge> : it.ticked > 0 ? <Badge tone="amber">{it.ticked} of {n} ticked</Badge> : <Badge tone="gray">Not ticked</Badge>}
                </span>
              </button>
              {isOpen && (
                <div className="space-y-2 border-t border-gray-100 p-3">
                  {it.listings.map((l) => (
                    <label key={l.listing_id} className="flex items-center gap-2">
                      <input type="checkbox" checked={l.dropship_ok} disabled={busy} onChange={(e) => void tickListing(l, e.target.checked)} />
                      <span>
                        Drop ship <span className="text-gray-600">{l.store} · item {l.item_id}</span>
                      </span>
                    </label>
                  ))}
                  <div className="flex flex-wrap items-center gap-2 border-t border-gray-100 pt-2">
                    {!it.dropship_ok ? (
                      <button type="button" className={btn2Cls} disabled={busy || n === 0} onClick={() => productOn(it)}>
                        Drop ship the whole product (every listing)
                      </button>
                    ) : (
                      <>
                        {it.has_prev && (
                          <button type="button" className={btn2Cls} disabled={busy} onClick={() => void productOff(it, "restore")}>
                            Turn off and put the listings back as they were
                          </button>
                        )}
                        <button type="button" className={btn2Cls} disabled={busy} onClick={() => void productOff(it, "reset")}>
                          Turn off and untick all
                        </button>
                      </>
                    )}
                  </div>
                </div>
              )}
            </div>
          );
        })}
        {data && data.items.length === 0 && !loading && <p className="text-sm text-gray-500">Nothing here.</p>}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ orders */

function OrdersTab({
  busy,
  run,
  setMsg,
}: {
  busy: string;
  run: <T>(key: string, fn: () => Promise<T>, ok: (r: T) => string, after?: () => Promise<void> | void) => Promise<void>;
  setMsg: (m: { kind: "error" | "success"; text: string } | null) => void;
}) {
  const [jobs, setJobs] = useState<DsJob[]>([]);
  const [order, setOrder] = useState("");
  const [open, setOpen] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      setJobs((await api<{ jobs: DsJob[] }>("/dropship/jobs")).jobs);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    }
  }, [setMsg]);
  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 rounded border border-gray-200 bg-white p-3 text-sm">
        <input className={`${inputCls} max-w-xs`} placeholder="eBay order number, to test with" value={order} onChange={(e) => setOrder(e.target.value)} />
        <button
          type="button"
          className={btnCls}
          disabled={busy !== "" || order.trim().length < 3}
          onClick={() =>
            void run("order", () => api<{ po_number: number }>("/dropship/jobs/from-order", { method: "POST", body: { order: order.trim() } }), (r) => {
              setOrder("");
              return `PO ${r.po_number} made. It is waiting for your check.`;
            }, load)
          }
        >
          {busy === "order" ? "Making…" : "Make the PO now"}
        </button>
        <span className="text-xs text-gray-500">Needs the listing ticked, or a drop ship you approved on Pick &amp; ship.</span>
      </div>

      {jobs.length === 0 && <p className="text-sm text-gray-500">No drop ship orders yet.</p>}
      {jobs.map((j) => (
        <div key={j.id} className="rounded border border-gray-200 bg-white p-3 text-sm">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
            <span className="font-mono">{j.ebay_order_id}</span>
            {j.po_number != null && <span>PO {j.po_number}</span>}
            {j.buyer_name && <span>{j.buyer_name}</span>}
            {j.ship_service && <span className="text-gray-600">{j.ship_service}</span>}
            <span className={`ml-auto rounded px-2 py-0.5 text-xs ${j.stage === "problem" ? "bg-red-50 text-red-700" : REVIEW.includes(j.stage) ? "bg-amber-50 text-amber-800" : "bg-gray-100 text-gray-700"}`}>
              {STAGE[j.stage] ?? j.stage}
            </span>
          </div>
          {j.problem && <p className="mt-1 text-xs text-red-700">{j.problem}</p>}
          <div className="mt-2 flex flex-wrap gap-2">
            {REVIEW.includes(j.stage) && (
              <button type="button" className={btn2Cls} onClick={() => setOpen(open === j.id ? null : j.id)}>
                {open === j.id ? "Close" : "Review"}
              </button>
            )}
            {j.stage === "problem" && (
              <button type="button" className={btn2Cls} disabled={busy !== ""} onClick={() => void run("retry", () => api(`/dropship/jobs/${j.id}/retry`, { method: "POST" }), () => "It will be tried again at the next check.", load)}>
                Try again
              </button>
            )}
          </div>
          {open === j.id && REVIEW.includes(j.stage) && (
            <div className="mt-3 border-t border-gray-100 pt-3">
              <PoReview job={j} onDone={() => { setOpen(null); void load(); }} />
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
