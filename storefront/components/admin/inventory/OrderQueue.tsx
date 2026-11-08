"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, errMsg } from "../../../lib/adminApi";
import { Badge, Notice, Pager, btn2Cls, inputCls } from "../ui";
import ClearableInput from "../ClearableInput";
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
     controls. Orders that need a human look (short pick, oversold) are marked in red but stay in date order. */

type Row = {
  channel: string;
  order_ref: string;
  placed_at: string | null;
  units: number;
  summary: string | null;
  sub: string | null;
  status: "pick" | "ship" | "shipped";
  attention: string[];
  claimed_by: string | null;
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
  const [reloadKey, setReloadKey] = useState(0);
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

  async function openRow(r: Row, takeOver = false) {
    setMsg(null);
    try {
      await api("/inventory/claims", {
        method: "POST",
        body: { channel: r.channel, order_ref: r.order_ref, take_over: takeOver },
      });
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
        order number, to work on it. <strong>Being picked by</strong> shows who already has an order open.
      </p>
      {msg && <Notice kind="error">{msg}</Notice>}

      <div className="max-w-xl">
        <ScanBox ref={scanRef} onScan={(c) => void scanOrder(c)} placeholder="Scan or type an order number to open it…" />
      </div>

      <div className="flex flex-wrap items-end gap-2">
        <ClearableInput
          className="w-[36rem] max-w-full rounded border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900"
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

      <Pager total={total} limit={LIMIT} offset={offset} onChange={setOffset} />

      <div className="overflow-x-auto rounded border border-gray-200 bg-white">
        {/* fixed column widths, sized for the longest content, so nothing shifts from page to page */}
        <table className="w-full min-w-[82rem] table-fixed text-left text-sm">
          <colgroup>
            <col className="w-[11.5rem]" />
            <col className="w-[7rem]" />
            <col className="w-[11rem]" />
            <col />
            <col className="w-[5rem]" />
            <col className="w-[16rem]" />
            <col className="w-[11rem]" />
          </colgroup>
          <thead className="bg-gray-50 text-xs uppercase text-gray-500">
            <tr>
              <th className="whitespace-nowrap px-3 py-2">Date</th>
              <th className="whitespace-nowrap px-3 py-2">Channel</th>
              <th className="whitespace-nowrap px-3 py-2">Order #</th>
              <th className="whitespace-nowrap px-3 py-2">Items</th>
              <th className="whitespace-nowrap px-3 py-2">Units</th>
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
                  className={`cursor-pointer hover:bg-gray-50 ${
                    r.attention.length ? "bg-red-50" : r.status === "shipped" ? "bg-green-50" : ""
                  }`}
                >
                  <td className="whitespace-nowrap px-3 py-2 text-gray-600">
                    {r.placed_at ? new Date(r.placed_at).toLocaleString() : "—"}
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
                    {r.attention.map((t) => (
                      <div key={t} className="text-xs font-semibold text-red-700">
                        ⚠ {t}
                      </div>
                    ))}
                  </td>
                  <td className="px-3 py-2">{r.units}</td>
                  <td className="px-3 py-2">
                    <Badge tone={st.tone}>{st.label}</Badge>
                  </td>
                  <td className="truncate px-3 py-2">
                    {r.claimed_by ? <Badge tone="amber">{r.claimed_by}</Badge> : <span className="text-gray-400">—</span>}
                  </td>
                </tr>
              );
            })}
            {rows.length === 0 && (
              <tr>
                <td colSpan={7} className="px-3 py-6 text-center text-gray-500">
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
