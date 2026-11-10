"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Badge, Notice, Pager, btn2Cls, inputCls } from "../ui";
import { SortTh, useSort } from "../SortHeader";

type Row = {
  id: number;
  at: string;
  employee: string | null;
  email: string | null;
  action: string;
  action_label: string;
  channel: string;
  order_ref: string;
  eio_number: string;
  detail: string | null;
};

type SortKey = "date" | "employee" | "action" | "eio" | "channel" | "order" | "detail";
const LIMIT = 50;
const TONE: Record<string, "gray" | "green" | "amber" | "blue"> = {
  picked: "blue",
  released: "gray",
  label_opened: "amber",
  tracking_shipped: "green",
};

/** Who did what to which order, and when. Search by EIO number, eBay / Amazon / website order number or employee. */
export default function OrderActivityLog({ active }: { active: boolean }) {
  const [q, setQ] = useState("");
  const [action, setAction] = useState("");
  const [channel, setChannel] = useState("");
  const [fFrom, setFFrom] = useState("");
  const [fTo, setFTo] = useState("");
  const [offset, setOffset] = useState(0);
  const [rows, setRows] = useState<Row[]>([]);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const seq = useRef(0);
  const { sort, toggle } = useSort<SortKey>(); // nothing chosen = newest first

  const load = useCallback(() => {
    const my = ++seq.current;
    const p = new URLSearchParams({ limit: String(LIMIT), offset: String(offset) });
    if (q.trim()) p.set("q", q.trim());
    if (action) p.set("action", action);
    if (channel) p.set("channel", channel);
    if (fFrom) p.set("date_from", fFrom);
    if (fTo) p.set("date_to", fTo);
    if (sort.key) {
      p.set("sort", sort.key);
      p.set("dir", sort.dir);
    }
    setLoading(true);
    api<{ items: Row[]; total: number }>(`/inventory/activity?${p.toString()}`)
      .then((r) => {
        if (my !== seq.current) return;
        setRows(r.items);
        setTotal(r.total);
        setError("");
      })
      .catch((e) => my === seq.current && setError(errMsg(e)))
      .finally(() => my === seq.current && setLoading(false));
  }, [q, action, channel, fFrom, fTo, sort.key, sort.dir, offset]);

  const onSort = (k: SortKey) => {
    toggle(k);
    setOffset(0);
  };

  // Typing in the search box waits a moment so it does not ask the server on every key.
  useEffect(() => {
    if (!active) return;
    const t = setTimeout(load, q ? 300 : 0);
    return () => clearTimeout(t);
  }, [active, load, q]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <input
          className={`${inputCls} min-w-[20rem] flex-1`}
          placeholder="Search EIO, eBay, Amazon or website order number"
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setOffset(0);
          }}
        />
        <select
          className={`${inputCls} w-52`}
          value={action}
          onChange={(e) => {
            setAction(e.target.value);
            setOffset(0);
          }}
        >
          <option value="">All actions</option>
          <option value="picked">Picked</option>
          <option value="label_opened">Label button pressed</option>
          <option value="released">Released</option>
          <option value="tracking_shipped">Tracking entered / shipped</option>
        </select>
        <select
          className={`${inputCls} w-36`}
          value={channel}
          onChange={(e) => {
            setChannel(e.target.value);
            setOffset(0);
          }}
        >
          <option value="">All channels</option>
          <option value="Website">Website</option>
          <option value="eBay">eBay</option>
          <option value="Amazon">Amazon</option>
        </select>
        <label className="text-xs text-gray-500">
          <span className="mb-1 block">From</span>
          <input type="date" className={`${inputCls} !w-auto`} value={fFrom} max={fTo || undefined} onChange={(e) => { setFFrom(e.target.value); setOffset(0); }} />
        </label>
        <label className="text-xs text-gray-500">
          <span className="mb-1 block">To</span>
          <input type="date" className={`${inputCls} !w-auto`} value={fTo} min={fFrom || undefined} onChange={(e) => { setFTo(e.target.value); setOffset(0); }} />
        </label>
        {(fFrom || fTo) && (
          <button type="button" className={btn2Cls} onClick={() => { setFFrom(""); setFTo(""); setOffset(0); }}>
            Clear dates
          </button>
        )}
        <button className={btn2Cls} onClick={load} disabled={loading}>
          Refresh
        </button>
      </div>
      {error && <Notice kind="error">{error}</Notice>}
      {!error && !loading && rows.length === 0 && <p className="text-sm text-gray-500">Nothing recorded yet.</p>}
      {rows.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white">
          <table className="w-full text-left text-sm">
            <thead className="bg-gray-50 text-xs uppercase text-gray-500">
              <tr>
                <SortTh label="Date and time" k="date" sort={sort} onSort={onSort} />
                <SortTh label="Employee" k="employee" sort={sort} onSort={onSort} />
                <SortTh label="Action" k="action" sort={sort} onSort={onSort} />
                <SortTh label="EIO" k="eio" sort={sort} onSort={onSort} />
                <SortTh label="Channel" k="channel" sort={sort} onSort={onSort} />
                <SortTh label="Order number" k="order" sort={sort} onSort={onSort} />
                <SortTh label="Details" k="detail" sort={sort} onSort={onSort} />
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {rows.map((r) => (
                <tr key={r.id} className="align-top">
                  <td className="whitespace-nowrap px-3 py-2">{new Date(r.at).toLocaleString()}</td>
                  <td className="px-3 py-2" title={r.email ?? undefined}>
                    <div className="font-medium">{r.employee ?? "—"}</div>
                    <div className="text-xs text-gray-500">{r.email}</div>
                  </td>
                  <td className="whitespace-nowrap px-3 py-2">
                    <Badge tone={TONE[r.action] ?? "gray"}>{r.action_label}</Badge>
                  </td>
                  <td className="whitespace-nowrap px-3 py-2 font-mono text-xs">{r.eio_number}</td>
                  <td className="px-3 py-2">{r.channel}</td>
                  <td className="whitespace-nowrap px-3 py-2 font-mono text-xs">{r.order_ref}</td>
                  <td className="px-3 py-2 text-gray-700">{r.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Pager total={total} limit={LIMIT} offset={offset} onChange={setOffset} />
    </div>
  );
}
