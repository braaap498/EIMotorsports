"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Badge, Notice, Pager, btn2Cls, inputCls } from "./ui";

/* Drop ship > Activity: what was drop shipped in a time frame (last hour, today, yesterday, last 7 days, or your own dates).
   Everything is counted by the server over the chosen frame, and the list is paged, so it stays quick with thousands of orders a day.
   "Received" = the eBay order reached us, "Ordered" = we placed it with the distributor, "Shipped" = eBay was told it shipped. */

type Basis = "received" | "placed" | "shipped";
type Range = "hour" | "today" | "yesterday" | "7d" | "custom";
type Row = {
  id: number;
  ebay_order_id: string;
  store: string | null;
  po_number: number | null;
  stage: string;
  buyer_name: string | null;
  ship_service: string | null;
  supplier_order: string | null;
  supplier_mode: string | null;
  tracking_number: string | null;
  tracking_carrier: string | null;
  problem: string | null;
  created_at: string | null;
  placed_at: string | null;
  shipped_at: string | null;
  units: number;
  title: string | null;
};
type Resp = {
  since: string;
  until: string;
  basis: Basis;
  total: number;
  rows: Row[];
  summary: { received: number; placed: number; shipped: number; problems: number; waiting: Record<string, number> };
};

const LIMIT = 50;
const RANGES: { key: Range; label: string }[] = [
  { key: "hour", label: "Last hour" },
  { key: "today", label: "Today" },
  { key: "yesterday", label: "Yesterday" },
  { key: "7d", label: "Last 7 days" },
  { key: "custom", label: "Pick dates" },
];
const STAGE: Record<string, string> = {
  review_po: "PO to check",
  po_confirmed: "PO checked",
  review_quote: "Quote to check",
  placing: "Placing order",
  placed: "Ordered, waiting for tracking",
  review_tracking: "Tracking to check",
  done: "Shipped",
  held: "On hold",
  return_needed: "eBay order cancelled: needs you",
  problem: "Needs attention",
};
const WAITING_ORDER = ["return_needed", "problem", "review_po", "po_confirmed", "review_quote", "placing", "placed", "review_tracking", "held"];
const BASIS_LABEL: Record<Basis, string> = { received: "Received", placed: "Ordered from distributor", shipped: "Shipped to buyer" };

function frameOf(range: Range, from: string, to: string): { since: Date; until: Date } | null {
  const now = new Date();
  const y = now.getFullYear();
  const m = now.getMonth();
  const d = now.getDate();
  if (range === "hour") return { since: new Date(now.getTime() - 3600_000), until: new Date(now.getTime() + 60_000) };
  if (range === "today") return { since: new Date(y, m, d), until: new Date(y, m, d + 1) };
  if (range === "yesterday") return { since: new Date(y, m, d - 1), until: new Date(y, m, d) };
  if (range === "7d") return { since: new Date(y, m, d - 6), until: new Date(y, m, d + 1) };
  const s = from ? new Date(from) : null;
  const u = to ? new Date(to) : null;
  if (!s || !u || Number.isNaN(s.getTime()) || Number.isNaN(u.getTime()) || s >= u) return null;
  return { since: s, until: u };
}

function when(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return `${d.toLocaleDateString()} ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
}

export default function DropShipActivity() {
  const [range, setRange] = useState<Range>("today");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [basis, setBasis] = useState<Basis>("received");
  const [stage, setStage] = useState("");
  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<Resp | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const seq = useRef(0);

  const load = useCallback(async () => {
    const f = frameOf(range, from, to);
    if (!f) {
      setData(null);
      setErr(range === "custom" ? "Choose a start and an end, with the start first." : null);
      return;
    }
    const mine = ++seq.current;
    setLoading(true);
    try {
      const p = new URLSearchParams({
        since: f.since.toISOString(),
        until: f.until.toISOString(),
        basis,
        offset: String(offset),
        limit: String(LIMIT),
      });
      if (stage) p.set("stage", stage);
      if (q) p.set("q", q);
      const r = await api<Resp>(`/dropship/activity?${p.toString()}`);
      if (mine === seq.current) {
        setData(r);
        setErr(null);
      }
    } catch (e) {
      if (mine === seq.current) setErr(errMsg(e));
    } finally {
      if (mine === seq.current) setLoading(false);
    }
  }, [range, from, to, basis, stage, q, offset]);

  useEffect(() => {
    void load();
  }, [load]);
  // a frame that includes "now" keeps itself fresh
  useEffect(() => {
    if (range !== "hour" && range !== "today") return;
    const t = setInterval(() => void load(), 60_000);
    return () => clearInterval(t);
  }, [range, load]);

  const reset = () => setOffset(0);
  const tile = (key: Basis, n: number | undefined, sub: string) => (
    <button
      type="button"
      onClick={() => {
        setBasis(key);
        reset();
      }}
      className={`rounded border p-3 text-left ${basis === key ? "border-black bg-white ring-1 ring-black" : "border-gray-200 bg-white hover:bg-gray-50"}`}
    >
      <div className="text-2xl font-semibold">{n ?? "…"}</div>
      <div className="text-sm font-medium">{BASIS_LABEL[key]}</div>
      <div className="text-xs text-gray-500">{sub}</div>
    </button>
  );
  const s = data?.summary;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 rounded border border-gray-200 bg-white p-3 text-sm">
        {RANGES.map((r) => (
          <button
            key={r.key}
            type="button"
            onClick={() => {
              setRange(r.key);
              reset();
            }}
            className={`rounded px-3 py-1.5 text-sm font-medium ${range === r.key ? "bg-black text-white" : "border border-gray-300 bg-white text-gray-800 hover:bg-gray-50"}`}
          >
            {r.label}
          </button>
        ))}
        {range === "custom" && (
          <>
            <label className="flex items-center gap-1 text-xs text-gray-600">
              From
              <input type="datetime-local" className={`${inputCls} w-auto`} value={from} onChange={(e) => { setFrom(e.target.value); reset(); }} />
            </label>
            <label className="flex items-center gap-1 text-xs text-gray-600">
              To
              <input type="datetime-local" className={`${inputCls} w-auto`} value={to} onChange={(e) => { setTo(e.target.value); reset(); }} />
            </label>
          </>
        )}
        <button type="button" className={`${btn2Cls} ml-auto`} onClick={() => void load()} disabled={loading}>
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>

      {err && <Notice kind="error">{err}</Notice>}

      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        {tile("received", s?.received, "eBay orders that needed a drop ship")}
        {tile("placed", s?.placed, "orders placed with the distributor")}
        {tile("shipped", s?.shipped, "eBay told it shipped, buyer notified")}
        <div className={`rounded border p-3 ${s && s.problems > 0 ? "border-red-300 bg-red-50" : "border-gray-200 bg-white"}`}>
          <div className="text-2xl font-semibold">{s?.problems ?? "…"}</div>
          <div className="text-sm font-medium">Needed attention</div>
          <div className="text-xs text-gray-500">stopped with a problem in this frame</div>
        </div>
      </div>

      {s && (
        <div className="flex flex-wrap items-center gap-2 rounded border border-gray-200 bg-white p-3 text-sm">
          <span className="font-medium">Waiting right now:</span>
          {WAITING_ORDER.filter((k) => (s.waiting[k] ?? 0) > 0).map((k) => (
            <button key={k} type="button" onClick={() => { setStage(stage === k ? "" : k); reset(); }} className={`rounded px-2 py-0.5 text-xs ${k === "problem" || k === "return_needed" ? "bg-red-50 text-red-700" : "bg-gray-100 text-gray-700"} ${stage === k ? "ring-1 ring-black" : ""}`}>
              {STAGE[k]} · {s.waiting[k]}
            </button>
          ))}
          {WAITING_ORDER.every((k) => (s.waiting[k] ?? 0) === 0) && <span className="text-gray-500">nothing</span>}
        </div>
      )}

      <form
        className="flex flex-wrap items-center gap-2 text-sm"
        onSubmit={(e) => {
          e.preventDefault();
          setQ(qInput.trim());
          reset();
        }}
      >
        <span className="text-gray-600">Showing orders by when they were</span>
        <select className={`${inputCls} w-auto`} value={basis} onChange={(e) => { setBasis(e.target.value as Basis); reset(); }}>
          <option value="received">received</option>
          <option value="placed">ordered from the distributor</option>
          <option value="shipped">shipped to the buyer</option>
        </select>
        <select className={`${inputCls} w-auto`} value={stage} onChange={(e) => { setStage(e.target.value); reset(); }}>
          <option value="">Any status</option>
          {Object.entries(STAGE).map(([k, v]) => (
            <option key={k} value={k}>{v}</option>
          ))}
        </select>
        <input className={`${inputCls} max-w-xs`} placeholder="Order, PO, buyer or tracking number" value={qInput} onChange={(e) => setQInput(e.target.value)} />
        <button type="submit" className={btn2Cls}>Search</button>
        {q && (
          <button type="button" className="text-xs underline" onClick={() => { setQ(""); setQInput(""); reset(); }}>
            Clear search
          </button>
        )}
      </form>

      <div className="overflow-x-auto rounded border border-gray-200 bg-white">
        <table className="w-full text-left text-sm">
          <thead className="bg-gray-50 text-xs uppercase text-gray-500">
            <tr>
              <th className="whitespace-nowrap px-3 py-2">{BASIS_LABEL[basis]}</th>
              <th className="px-3 py-2">eBay order</th>
              <th className="px-3 py-2">PO</th>
              <th className="px-3 py-2">Item</th>
              <th className="px-3 py-2">Buyer</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2">Distributor order · tracking</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {(data?.rows ?? []).map((r) => {
              const at = basis === "received" ? r.created_at : basis === "placed" ? r.placed_at : r.shipped_at;
              return (
                <tr key={r.id} className={r.stage === "problem" || r.stage === "return_needed" ? "bg-red-50" : ""}>
                  <td className="whitespace-nowrap px-3 py-2 text-gray-600">{when(at)}</td>
                  <td className="whitespace-nowrap px-3 py-2 font-mono text-xs">
                    {r.ebay_order_id}
                    {r.store && <div className="font-sans text-gray-500">{r.store}</div>}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2">{r.po_number ?? "—"}</td>
                  <td className="px-3 py-2">
                    <div className="max-w-xs truncate" title={r.title ?? ""}>{r.title || "—"}</div>
                    <div className="text-xs text-gray-500">{r.units} unit{r.units === 1 ? "" : "s"}</div>
                  </td>
                  <td className="px-3 py-2">
                    {r.buyer_name ?? "—"}
                    {r.ship_service && <div className="text-xs text-gray-500">{r.ship_service}</div>}
                  </td>
                  <td className="px-3 py-2">
                    <Badge tone={r.stage === "done" ? "green" : r.stage === "problem" || r.stage === "return_needed" ? "amber" : "blue"}>{STAGE[r.stage] ?? r.stage}</Badge>
                    {r.problem && <div className="max-w-xs text-xs text-red-700">{r.problem}</div>}
                  </td>
                  <td className="px-3 py-2 text-xs">
                    <div>{r.supplier_order ? `${r.supplier_order}${r.supplier_mode === "test" ? " (test)" : ""}` : "—"}</div>
                    <div className="text-gray-500">{r.tracking_number ? `${r.tracking_carrier ?? ""} ${r.tracking_number}`.trim() : "no tracking yet"}</div>
                  </td>
                </tr>
              );
            })}
            {data && data.rows.length === 0 && (
              <tr>
                <td colSpan={7} className="px-3 py-6 text-center text-gray-500">
                  No drop ship orders in this time frame.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {data && <Pager total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />}
    </div>
  );
}
