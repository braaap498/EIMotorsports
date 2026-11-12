"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, errMsg } from "../../../lib/adminApi";
import { Badge, Notice, Pager, btn2Cls, inputCls } from "../ui";
import ClearableInput from "../ClearableInput";
import { useAccess } from "../AdminAccess";
import ScanBox from "./ScanBox";
import ScanSession from "./ScanSession";
import WebOrderShip from "./WebOrderShip";
import EbayOrdersToShip from "./EbayOrdersToShip";
import { beep } from "./shared";

/* Inventory > Pick & ship: ONE list of every order waiting to be picked or shipped, from every sales channel,
   oldest first, so no channel gets priority and the team works the list from the top.
   - Filter by sales channel, or search by order #, customer, item, label, title or tracking number.
   - Open an order by clicking it or by scanning its order number. That marks it "being picked by <you>" for
     everyone else (kept alive while it is open, dropped when you go back, or after 10 minutes of silence).
   - Website orders open the website pick screen; eBay orders open the scan-out screen plus the label / tracking
     controls. Orders that need a human look (short pick, oversold) are marked in red but stay in date order.
   - URGENT orders (too big for our shelf and a big share of the distributor's stock, or nothing can fill them) are pinned
     above the list in red until a person approves a drop ship or chooses our own stock. Recording the choice does not
     place the distributor order. */

type Row = {
  channel: string;
  order_ref: string;
  placed_at: string | null;
  units: number;
  summary: string | null;
  sub: string | null;
  status: "pick" | "ship" | "shipped";
  attention: string[];
  claimed_by: string | null; // the person's name
  claimed_by_email: string | null; // shown under the name
  fulfillment: { kind: "dropship" | "warehouse" | "undecided"; source: string; po_number: number | null; po_pending: boolean };
  urgent: boolean; // waiting for a person to decide: drop ship or our own stock
  urgent_reasons: string[];
  decision: { decision: "dropship" | "stock"; by: string; at: string } | null;
};
type Resp = { total: number; channels: string[]; items: Row[] };

const LIMIT = 50;
const STATUS: Record<Row["status"], { label: string; tone: "amber" | "blue" | "green" }> = {
  pick: { label: "To pick", tone: "amber" },
  ship: { label: "Picked · needs tracking", tone: "blue" },
  shipped: { label: "Shipped · waiting to be picked", tone: "green" },
};

export default function OrderQueue({ active }: { active: boolean }) {
  const [rows, setRows] = useState<Row[]>([]);
  const [total, setTotal] = useState(0);
  const [channels, setChannels] = useState<string[]>(["Website", "eBay"]);
  const [offset, setOffset] = useState(0);
  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState("");
  const [channel, setChannel] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState<Row | null>(null);
  const [takenBy, setTakenBy] = useState<string | null>(null); // someone took the open order over
  const [me, setMe] = useState<string | null>(null); // my name as the others see it on the list
  const [reloadKey, setReloadKey] = useState(0);
  const [deciding, setDeciding] = useState<string | null>(null);
  const { isAdmin } = useAccess();
  // eBay order numbers ticked for a bulk Mark picked / Release. Admins only; website orders are picked by scanning, so they have no tick box.
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const [okMsg, setOkMsg] = useState<string | null>(null);
  const scanRef = useRef<HTMLInputElement>(null);
  const openRef = useRef<Row | null>(null);
  openRef.current = open;

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const p = new URLSearchParams({ offset: String(offset), limit: String(LIMIT) });
      if (q) p.set("q", q);
      if (channel) p.set("channel", channel);
      const r = await api<Resp>(`/inventory/queue?${p.toString()}`);
      setRows(r.items);
      setTotal(r.total);
      // keep only ticks for orders still in the list, so a bulk action never touches an order you cannot see
      const here = new Set(r.items.filter((i) => i.channel === "eBay").map((i) => i.order_ref));
      setSelected((prev) => (prev.size === 0 ? prev : new Set([...prev].filter((id) => here.has(id)))));
      if (r.channels?.length) setChannels(r.channels);
    } catch (e) {
      setMsg(errMsg(e));
    } finally {
      setLoading(false);
    }
  }, [offset, q, channel]);

  // Read the list when the tab is shown, and keep it fresh while nobody is working inside an order.
  useEffect(() => {
    if (!active || open) return;
    void load();
    const t = setInterval(() => void load(), 15000);
    const onFocus = () => void load();
    window.addEventListener("focus", onFocus);
    return () => {
      clearInterval(t);
      window.removeEventListener("focus", onFocus);
    };
  }, [active, open, load]);

  // Wait for a pause in typing before searching.
  useEffect(() => {
    if (qInput.trim() === q) return;
    const t = setTimeout(() => {
      setQ(qInput.trim());
      setOffset(0);
    }, 300);
    return () => clearTimeout(t);
  }, [qInput, q]);

  // While an order is open, tell the server every minute that it is still being worked on.
  useEffect(() => {
    if (!open) return;
    const body = { channel: open.channel, order_ref: open.order_ref };
    const t = setInterval(() => {
      api<{ held: boolean; by: string | null }>("/inventory/claims/heartbeat", { method: "POST", body })
        .then((r) => setTakenBy(r.held ? null : r.by ?? "Someone else"))
        .catch(() => {});
    }, 60000);
    return () => clearInterval(t);
  }, [open]);

  // Leaving the page with an order open: let go of it (the 10 minute timeout covers a closed browser).
  useEffect(
    () => () => {
      const o = openRef.current;
      if (o) void api("/inventory/claims/release", { method: "POST", body: { channel: o.channel, order_ref: o.order_ref } }).catch(() => {});
    },
    []
  );

  async function decide(r: Row, decision: "dropship" | "stock") {
    const what =
      decision === "dropship"
        ? "APPROVE A DROP SHIP for"
        : "SHIP FROM OUR OWN STOCK instead for";
    if (!window.confirm(`${what} ${r.channel} order ${r.order_ref}?\n\n${r.urgent_reasons.join("\n")}\n\nThis records your decision. It does not place the distributor order.`)) return;
    setMsg(null);
    setDeciding(`${r.channel}:${r.order_ref}`);
    try {
      await api("/inventory/fulfillment/decision", { method: "POST", body: { channel: r.channel, order_ref: r.order_ref, decision } });
      await load();
    } catch (e) {
      setMsg(errMsg(e));
    } finally {
      setDeciding(null);
    }
  }

  const canSelect = (r: Row) => isAdmin && r.channel === "eBay";
  const selectable = rows.filter(canSelect);
  const allTicked = selectable.length > 0 && selectable.every((r) => selected.has(r.order_ref));
  const someTicked = selectable.some((r) => selected.has(r.order_ref));

  function toggleOne(ref: string) {
    setOkMsg(null);
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(ref)) next.delete(ref);
      else next.add(ref);
      return next;
    });
  }

  function toggleAll() {
    setOkMsg(null);
    setSelected(allTicked ? new Set() : new Set(selectable.map((r) => r.order_ref)));
  }

  async function bulk(action: "pick" | "release") {
    const ids = [...selected];
    if (ids.length === 0) return;
    const n = `${ids.length} selected eBay order${ids.length === 1 ? "" : "s"}`;
    const q =
      action === "pick"
        ? `Mark ALL parts of ${n} as picked? This removes them from the shelf count now. Use this only if the parts have already left and were never scanned out.`
        : `Release the hold on ALL parts of ${n}? They become available again and the shelf count stays the same. Use this only if the parts are still on the shelf.`;
    if (!window.confirm(q)) return;
    setMsg(null);
    setOkMsg(null);
    setBulkBusy(true);
    try {
      const r = await api<{ lines: number; orders: number; failed: { order_id: string; detail: string }[] }>("/inventory/ebay-orders/lines/bulk", {
        method: "POST",
        body: { order_ids: ids, action },
      });
      if (r.lines > 0) setOkMsg(`${action === "pick" ? "Marked picked" : "Released"}: ${r.lines} line${r.lines === 1 ? "" : "s"} on ${r.orders} order${r.orders === 1 ? "" : "s"}.`);
      if (r.failed.length) setMsg(`Could not do ${r.failed.length} line${r.failed.length === 1 ? "" : "s"}: ${r.failed.map((f) => `${f.order_id} (${f.detail})`).join("; ")}`);
      else if (r.lines === 0) setMsg("Nothing to do: those orders have no parts set aside any more.");
      setSelected(new Set());
      await load();
    } catch (e) {
      setMsg(errMsg(e));
    } finally {
      setBulkBusy(false);
    }
  }

  async function openRow(r: Row, takeOver = false) {
    setMsg(null);
    if (r.urgent && !takeOver && !window.confirm(`This order is URGENT and still waiting for a decision (drop ship or our own stock).\n\nOpen it anyway?`)) return;
    try {
      const c = await api<{ ok: boolean; by?: string }>("/inventory/claims", {
        method: "POST",
        body: { channel: r.channel, order_ref: r.order_ref, take_over: takeOver },
      });
      setMe(c.by ?? null);
      setTakenBy(null);
      setReloadKey(0);
      setOpen(r);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && !takeOver) {
        if (window.confirm(`${errMsg(e)} Open it anyway and take it over?`)) await openRow(r, true);
        else void load();
      } else {
        setMsg(errMsg(e));
      }
    }
  }

  const closeOrder = useCallback(() => {
    const o = openRef.current;
    if (!o) return;
    openRef.current = null;
    setOpen(null);
    setTakenBy(null);
    void api("/inventory/claims/release", { method: "POST", body: { channel: o.channel, order_ref: o.order_ref } }).catch(() => {});
    setTimeout(() => scanRef.current?.focus(), 0);
  }, []);

  /** A scanned (or typed) order number opens that order; anything else just filters the list. */
  async function scanOrder(code: string) {
    setMsg(null);
    try {
      const p = new URLSearchParams({ q: code, limit: "10" });
      const r = await api<Resp>(`/inventory/queue?${p.toString()}`);
      const exact = r.items.filter((i) => i.order_ref.toLowerCase() === code.toLowerCase());
      const hit = exact.length === 1 ? exact[0] : r.items.length === 1 ? r.items[0] : null;
      if (hit) {
        beep(true);
        await openRow(hit);
        return;
      }
      beep(false);
      setQInput(code);
      setMsg(
        r.items.length === 0
          ? `No order waiting for “${code}”.`
          : `More than one order matches “${code}”. Pick the right one from the list.`
      );
    } catch (e) {
      beep(false);
      setMsg(errMsg(e));
    }
  }

  if (open) {
    return (
      <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-3">
          <button type="button" className={btn2Cls} onClick={closeOrder}>
            ← Back to all orders
          </button>
          <span className="flex items-center gap-2 text-sm text-gray-700">
            <Badge tone={open.channel === "Website" ? "blue" : "amber"}>{open.channel}</Badge>
            order <strong>{open.order_ref}</strong>
          </span>
          {!takenBy && <Badge tone="amber">Being picked by you{me ? ` (${me})` : ""}</Badge>}
        </div>
        {takenBy && (
          <Notice kind="error">
            {takenBy} has taken this order over. Check with them before you commit anything, so you don’t both pick it.
          </Notice>
        )}
        {open.channel === "Website" ? (
          <WebOrderShip active={active} embedded openNumber={open.order_ref} onClosed={closeOrder} />
        ) : (
          <div className="space-y-6">
            <ScanSession
              mode="ship-other"
              active={active}
              initialReference={open.order_ref}
              onCommitted={() => setReloadKey((k) => k + 1)}
            />
            <EbayOrdersToShip active={active} embedded initialQ={open.order_ref} reloadKey={reloadKey} onEmpty={closeOrder} />
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <p className="text-xs text-gray-500">
        Every order waiting to be picked or shipped, from all sales channels, oldest first. Click an order, or scan its
        order number, to work on it. <strong>Being picked by</strong> shows who already has an order open. <strong>Fulfillment source</strong> shows
        whether the parts come from one of our warehouses or from a drop ship.
      </p>
      {msg && <Notice kind="error">{msg}</Notice>}
      {okMsg && <Notice kind="success">{okMsg}</Notice>}

      <div>
        <ScanBox ref={scanRef} onScan={(c) => void scanOrder(c)} placeholder="Scan or type an order number to open it…" />
      </div>

      <div className="flex flex-wrap items-end gap-2">
        <ClearableInput
          className="min-w-[16rem] flex-1 rounded border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900"
          placeholder="Search title, order number, item number, custom label or SKU"
          value={qInput}
          onChange={(e) => setQInput(e.target.value)}
          onClear={() => {
            setQInput("");
            setQ("");
            setOffset(0);
          }}
        />
        <select
          className={`${inputCls} !w-48`}
          value={channel}
          onChange={(e) => {
            setChannel(e.target.value.toLowerCase());
            setOffset(0);
          }}
          aria-label="Sales channel"
        >
          <option value="">All sales channels</option>
          {channels.map((c) => (
            <option key={c} value={c.toLowerCase()}>
              {c}
            </option>
          ))}
        </select>
        <button type="button" className={btn2Cls} disabled={loading} onClick={() => { setMsg(null); void load(); }}>
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>

      {rows.some((r) => r.urgent) && (
        <div className="rounded border-2 border-red-600 bg-red-50 px-3 py-2 text-sm font-semibold text-red-800">
          {rows.filter((r) => r.urgent).length} order{rows.filter((r) => r.urgent).length === 1 ? "" : "s"} waiting for a human decision. Approve a
          drop ship or choose our own stock; they stay pinned at the top until you do.
        </div>
      )}

      <Pager total={total} limit={LIMIT} offset={offset} onChange={setOffset} />

      {isAdmin && selected.size > 0 && (
        <div className="flex flex-wrap items-center gap-3 rounded border border-gray-300 bg-gray-50 px-3 py-2 text-sm">
          <span className="font-medium">{selected.size} eBay order{selected.size === 1 ? "" : "s"} selected</span>
          <button type="button" className={btn2Cls} disabled={bulkBusy} onClick={() => void bulk("pick")}>
            {bulkBusy ? "Working…" : "Mark picked"}
          </button>
          <button type="button" className={btn2Cls} disabled={bulkBusy} onClick={() => void bulk("release")}>
            Release
          </button>
          <button type="button" className="text-xs underline" disabled={bulkBusy} onClick={() => setSelected(new Set())}>
            Clear selection
          </button>
        </div>
      )}

      <div className="overflow-x-auto rounded border border-gray-200 bg-white">
        {/* fixed column widths, sized for the longest content, so nothing shifts from page to page */}
        <table className="w-full min-w-[98rem] table-fixed text-left text-sm">
          <colgroup>
            {isAdmin && <col className="w-[2.5rem]" />} {/* tick box (admin only) */}
            <col className="w-[7rem]" /> {/* Date (date, time under it) */}
            <col className="w-[7rem]" /> {/* Channel */}
            <col className="w-[11rem]" /> {/* Order # */}
            <col /> {/* Items: takes the rest */}
            <col className="w-[5rem]" /> {/* Units */}
            <col className="w-[12rem]" /> {/* Fulfillment source */}
            <col className="w-[16rem]" /> {/* Status */}
            <col className="w-[16rem]" /> {/* Being picked by (name, email under it) */}
          </colgroup>
          <thead className="bg-gray-50 text-xs uppercase text-gray-500">
            <tr>
              {isAdmin && (
                <th className="px-3 py-2">
                  <input
                    type="checkbox"
                    aria-label="Select all eBay orders on this page"
                    disabled={selectable.length === 0 || bulkBusy}
                    checked={allTicked}
                    ref={(el) => {
                      if (el) el.indeterminate = someTicked && !allTicked;
                    }}
                    onChange={toggleAll}
                  />
                </th>
              )}
              <th className="whitespace-nowrap px-3 py-2">Date</th>
              <th className="whitespace-nowrap px-3 py-2">Channel</th>
              <th className="whitespace-nowrap px-3 py-2">Order #</th>
              <th className="whitespace-nowrap px-3 py-2">Items</th>
              <th className="whitespace-nowrap px-3 py-2">Units</th>
              <th className="whitespace-nowrap px-3 py-2">Fulfillment source</th>
              <th className="whitespace-nowrap px-3 py-2">Status</th>
              <th className="whitespace-nowrap px-3 py-2">Being picked by</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {rows.map((r) => {
              const st = STATUS[r.status] ?? STATUS.pick;
              return (
                <tr
                  key={`${r.channel}:${r.order_ref}`}
                  tabIndex={0}
                  onClick={() => void openRow(r)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") void openRow(r);
                  }}
                  className={`cursor-pointer ${
                    r.urgent
                      ? "bg-red-100 hover:bg-red-200"
                      : `hover:bg-gray-50 ${r.attention.length ? "bg-red-50" : r.status === "shipped" ? "bg-green-50" : ""}`
                  }`}
                >
                  {isAdmin && (
                    <td
                      className={`px-3 py-2 ${r.urgent ? "border-l-4 border-red-600" : ""}`}
                      onClick={(e) => e.stopPropagation()}
                      onKeyDown={(e) => e.stopPropagation()}
                    >
                      {canSelect(r) && (
                        <input
                          type="checkbox"
                          aria-label={`Select eBay order ${r.order_ref}`}
                          disabled={bulkBusy}
                          checked={selected.has(r.order_ref)}
                          onChange={() => toggleOne(r.order_ref)}
                        />
                      )}
                    </td>
                  )}
                  <td className={`whitespace-nowrap px-3 py-2 text-gray-600 ${r.urgent && !isAdmin ? "border-l-4 border-red-600" : ""}`}>
                    {r.placed_at ? (
                      <>
                        <div>{new Date(r.placed_at).toLocaleDateString()}</div>
                        <div>{new Date(r.placed_at).toLocaleTimeString()}</div>
                      </>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="px-3 py-2">
                    <Badge tone={r.channel === "Website" ? "blue" : "amber"}>{r.channel}</Badge>
                  </td>
                  <td className="truncate px-3 py-2 font-medium" title={r.order_ref}>
                    {r.order_ref}
                  </td>
                  <td className="px-3 py-2">
                    <div className="truncate" title={r.summary ?? ""}>
                      {r.summary ?? "(no title)"}
                    </div>
                    {r.sub && <div className="truncate text-xs text-gray-500">{r.sub}</div>}
                    {r.urgent_reasons.map((t) => (
                      <div key={t} className="whitespace-normal text-xs font-semibold text-red-800">
                        URGENT: {t}
                      </div>
                    ))}
                    {r.attention.map((t) => (
                      <div key={t} className="text-xs font-semibold text-red-700">
                        ⚠ {t}
                      </div>
                    ))}
                  </td>
                  <td className="px-3 py-2">{r.units}</td>
                  <td className="whitespace-nowrap px-3 py-2">
                    {r.fulfillment?.kind === "dropship" ? (
                      <>
                        <Badge tone="blue">{r.fulfillment.source}</Badge>
                        <div className="text-xs text-gray-500">{r.fulfillment.po_number ? `PO ${r.fulfillment.po_number}` : "PO not made yet"}</div>
                      </>
                    ) : r.fulfillment?.kind === "undecided" ? (
                      <span className="text-xs font-semibold text-red-700">{r.fulfillment.source}</span>
                    ) : (
                      <Badge tone="gray">{r.fulfillment?.source ?? "—"}</Badge>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    {r.urgent ? (
                      <div className="space-y-1" onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
                        <span className="inline-block rounded bg-red-600 px-2 py-0.5 text-xs font-bold text-white">URGENT · needs a decision</span>
                        <div>
                          <Badge tone={st.tone}>{st.label}</Badge>
                        </div>
                        <div className="flex flex-wrap gap-1">
                          <button
                            type="button"
                            className="rounded bg-black px-2 py-1 text-xs font-medium text-white disabled:opacity-50"
                            disabled={deciding === `${r.channel}:${r.order_ref}`}
                            onClick={() => void decide(r, "dropship")}
                          >
                            Approve drop ship
                          </button>
                          <button
                            type="button"
                            className="rounded border border-gray-400 bg-white px-2 py-1 text-xs font-medium text-gray-800 disabled:opacity-50"
                            disabled={deciding === `${r.channel}:${r.order_ref}`}
                            onClick={() => void decide(r, "stock")}
                          >
                            Ship from our stock
                          </button>
                        </div>
                      </div>
                    ) : (
                      <div className="space-y-1">
                        <Badge tone={st.tone}>{st.label}</Badge>
                        {r.decision && (
                          <div>
                            <Badge tone="blue">
                              {r.decision.decision === "dropship" ? "Drop ship approved" : "Ship from our stock"} · {r.decision.by}
                            </Badge>
                          </div>
                        )}
                      </div>
                    )}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2">
                    {r.claimed_by ? (
                      <>
                        <Badge tone="amber">{r.claimed_by}</Badge>
                        {r.claimed_by_email && <div className="text-xs text-gray-500">{r.claimed_by_email}</div>}
                      </>
                    ) : (
                      <span className="text-gray-400">—</span>
                    )}
                  </td>
                </tr>
              );
            })}
            {rows.length === 0 && (
              <tr>
                <td colSpan={isAdmin ? 9 : 8} className="px-3 py-6 text-center text-gray-500">
                  {loading ? "Loading…" : q || channel ? "No orders match." : "No orders waiting. Everything has been picked and shipped."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <Pager total={total} limit={LIMIT} offset={offset} onChange={setOffset} />
    </div>
  );
}
