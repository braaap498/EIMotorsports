"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, apiDownload, errMsg } from "../../lib/adminApi";
import ClearableInput from "./ClearableInput";
import ClickRow from "./ClickRow";
import { SortTh, useSort } from "./SortHeader";
import { Badge, Modal, Notice, Pager, btnCls, btn2Cls, inputCls } from "./ui";

/* The "Invoices" tab: every invoice Automatic Distributors has billed us, kept here so it can be searched.
   The server fetches them on its own (same schedule as the order tracking, set on the Data feed tab); "Fetch invoices"
   pulls older days. Click an invoice to see its parts. "Shipping and handling" is the distributor's "special charges"
   line. */

type Row = {
  id: number;
  order_number: string;
  po_number: string | null;
  invoice_number: string | null;
  invoice_date: string | null; // 2026-11-10
  invoice_amount: number | null;
  order_value: number | null;
  item_amount: number | null;
  special_charges: number | null;
  carrier_code: string | null;
  tracking_number: string | null;
  order_status: string | null;
  ship_to_name: string | null;
  ship_to_city: string | null;
  ship_to_state: string | null;
  line_count: number;
  po_id: number | null;
  ebay_order_id: string | null;
};
type Line = { number: string; description: string; ordered: string; shipped: string; backordered: string; price: string; warehouse: string | null };
type Detail = Row & { parent_order_number: string | null; ship_to_zip: string | null; lines: Line[] };
type List = { total: number; sum_invoice: number; sum_shipping: number; last_synced_at: string | null; items: Row[] };
type SortKey = "date" | "invoice" | "order" | "po" | "customer" | "amount" | "shipping";

const LIMIT = 50;
const money = (n: number | null | undefined) => (n == null ? "" : n.toLocaleString("en-US", { style: "currency", currency: "USD" }));
const dateText = (d: string | null) => {
  if (!d) return "";
  const [y, m, day] = d.split("-");
  return `${m}/${day}/${y}`;
};
const hasInvoice = (n: string | null) => !!n && n !== "0";
const qty = (v: string) => String(Number(v));

export default function AutodistInvoices({ active }: { active: boolean }) {
  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [link, setLink] = useState("");
  const [invoiced, setInvoiced] = useState("");
  const [offset, setOffset] = useState(0);
  const { sort, toggle } = useSort<SortKey>();
  const [data, setData] = useState<List | null>(null);
  const [flash, setFlash] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const [open, setOpen] = useState<Detail | null>(null);
  const [days, setDays] = useState(30);
  const [syncing, setSyncing] = useState(false);
  const [bump, setBump] = useState(0);
  const poll = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    const t = setTimeout(() => {
      setQ(qInput.trim());
      setOffset(0);
    }, 300);
    return () => clearTimeout(t);
  }, [qInput]);

  const params = useCallback(() => {
    const p = new URLSearchParams();
    if (q) p.set("q", q);
    if (from) p.set("from", from);
    if (to) p.set("to", to);
    if (link) p.set("link", link);
    if (invoiced) p.set("invoiced", invoiced);
    return p;
  }, [q, from, to, link, invoiced]);

  useEffect(() => {
    if (!active) return;
    const p = params();
    p.set("limit", String(LIMIT));
    p.set("offset", String(offset));
    p.set("sort", sort.key ?? "date");
    p.set("dir", sort.key ? sort.dir : "desc");
    api<List>(`/autodist/api/invoices?${p}`)
      .then(setData)
      .catch((e) => setFlash({ kind: "error", text: errMsg(e) }));
  }, [active, params, offset, sort, bump]);

  useEffect(() => () => { if (poll.current) clearInterval(poll.current); }, []);

  async function fetchInvoices() {
    setFlash(null);
    setSyncing(true);
    try {
      await api("/autodist/api/invoices/sync", { method: "POST", body: { days } });
      poll.current = setInterval(async () => {
        try {
          const s = await api<{ running: boolean; message: string }>("/autodist/api/invoices/sync-status");
          if (!s.running) {
            if (poll.current) clearInterval(poll.current);
            setSyncing(false);
            setFlash({ kind: s.message.includes("Stopped early") || s.message.includes("failed") ? "error" : "success", text: s.message });
            setBump((b) => b + 1);
          }
        } catch (e) {
          if (poll.current) clearInterval(poll.current);
          setSyncing(false);
          setFlash({ kind: "error", text: errMsg(e) });
        }
      }, 3000);
    } catch (e) {
      setSyncing(false);
      setFlash({ kind: "error", text: errMsg(e) });
    }
  }

  async function exportCsv() {
    try {
      const blob = await apiDownload(`/autodist/api/invoices/export?${params()}`);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "autodist-invoices.csv";
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
    } catch (e) {
      setFlash({ kind: "error", text: errMsg(e) });
    }
  }

  const view = (id: number) =>
    api<Detail>(`/autodist/api/invoices/${id}`)
      .then(setOpen)
      .catch((e) => setFlash({ kind: "error", text: errMsg(e) }));

  const reset = () => {
    setQInput("");
    setFrom("");
    setTo("");
    setLink("");
    setInvoiced("");
  };
  const filtered = !!(qInput || from || to || link || invoiced);
  const onSort = (k: SortKey) => {
    toggle(k);
    setOffset(0);
  };

  return (
    <div className="space-y-4">
      {flash && <Notice kind={flash.kind}>{flash.text}</Notice>}

      <div className="flex flex-wrap items-center gap-3">
        <ClearableInput
          className={`${inputCls} w-full max-w-md`}
          value={qInput}
          onChange={(e) => setQInput(e.target.value)}
          placeholder="Search invoice #, order #, PO #, customer, tracking or part"
        />
        <label className="flex items-center gap-2 text-sm">
          From
          <input type="date" className={`${inputCls} !w-auto`} value={from} onChange={(e) => { setFrom(e.target.value); setOffset(0); }} />
        </label>
        <label className="flex items-center gap-2 text-sm">
          To
          <input type="date" className={`${inputCls} !w-auto`} value={to} onChange={(e) => { setTo(e.target.value); setOffset(0); }} />
        </label>
        <select className={`${inputCls} !w-auto`} value={link} onChange={(e) => { setLink(e.target.value); setOffset(0); }}>
          <option value="">All orders</option>
          <option value="po">From one of our POs</option>
          <option value="ebay">eBay drop ship</option>
          <option value="nopo">Not from our POs</option>
        </select>
        <select className={`${inputCls} !w-auto`} value={invoiced} onChange={(e) => { setInvoiced(e.target.value); setOffset(0); }}>
          <option value="">Invoiced or not</option>
          <option value="yes">Has an invoice number</option>
          <option value="no">No invoice number yet</option>
        </select>
        {filtered && (
          <button type="button" className={btn2Cls} onClick={reset}>
            Clear filters
          </button>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-3 text-sm">
        <button type="button" className={btn2Cls} disabled={syncing} onClick={() => void fetchInvoices()}>
          {syncing ? "Fetching…" : "Fetch invoices for the last"}
        </button>
        <select className={`${inputCls} !w-auto`} value={days} disabled={syncing} onChange={(e) => setDays(Number(e.target.value))}>
          {[7, 14, 30, 60, 90].map((d) => (
            <option key={d} value={d}>
              {d} days
            </option>
          ))}
        </select>
        <button type="button" className={btn2Cls} disabled={!data || data.total === 0} onClick={() => void exportCsv()}>
          Download CSV
        </button>
        <span className="text-xs text-gray-500">
          {data?.last_synced_at ? `Last fetched ${new Date(data.last_synced_at).toLocaleString()}. ` : "Nothing fetched yet. "}
          The server also fetches the last 3 days on its own.
        </span>
      </div>

      {data && (
        <div className="text-sm text-gray-700">
          {data.total} invoice{data.total === 1 ? "" : "s"} · invoiced {money(data.sum_invoice)} · shipping and handling {money(data.sum_shipping)}
        </div>
      )}

      <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white">
        <table className="w-full text-left text-sm">
          <thead className="bg-gray-50 text-xs uppercase text-gray-500">
            <tr>
              <SortTh label="Date" k="date" sort={sort} onSort={onSort} />
              <SortTh label="Invoice #" k="invoice" sort={sort} onSort={onSort} />
              <SortTh label="Order #" k="order" sort={sort} onSort={onSort} />
              <SortTh label="Our PO" k="po" sort={sort} onSort={onSort} />
              <SortTh label="Customer" k="customer" sort={sort} onSort={onSort} />
              <th className="px-3 py-2">Parts</th>
              <SortTh label="Ship + handling" k="shipping" sort={sort} onSort={onSort} className="px-3 py-2 text-right" />
              <SortTh label="Invoice" k="amount" sort={sort} onSort={onSort} className="px-3 py-2 text-right" />
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {data?.items.map((r) => (
              <ClickRow key={r.id} onOpen={() => void view(r.id)}>
                <td className="whitespace-nowrap px-3 py-2">{dateText(r.invoice_date)}</td>
                <td className="px-3 py-2">{hasInvoice(r.invoice_number) ? r.invoice_number : <Badge tone="amber">not yet</Badge>}</td>
                <td className="px-3 py-2 font-medium">{r.order_number}</td>
                <td className="px-3 py-2">
                  {r.po_number || ""}
                  {r.ebay_order_id && <span className="ml-2"><Badge tone="blue">eBay</Badge></span>}
                  {r.po_number && !r.po_id && <span className="ml-2"><Badge tone="gray">no PO here</Badge></span>}
                </td>
                <td className="px-3 py-2">
                  {r.ship_to_name}
                  {r.ship_to_state && <span className="text-gray-500"> · {[r.ship_to_city, r.ship_to_state].filter(Boolean).join(", ")}</span>}
                </td>
                <td className="px-3 py-2">{r.line_count}</td>
                <td className="whitespace-nowrap px-3 py-2 text-right">{money(r.special_charges)}</td>
                <td className="whitespace-nowrap px-3 py-2 text-right font-medium">{money(r.invoice_amount)}</td>
              </ClickRow>
            ))}
            {data && data.items.length === 0 && (
              <tr>
                <td colSpan={8} className="px-3 py-6 text-center text-gray-500">
                  {filtered ? "No invoices match." : "No invoices stored yet. Press “Fetch invoices” to pull the last few weeks."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {data && <Pager total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />}

      {open && (
        <Modal title={hasInvoice(open.invoice_number) ? `Invoice ${open.invoice_number}` : `Order ${open.order_number} (not invoiced yet)`} onClose={() => setOpen(null)}>
          <div className="space-y-3 text-sm">
            <div className="grid grid-cols-2 gap-x-6 gap-y-1">
              <div>Invoice date: {dateText(open.invoice_date) || "—"}</div>
              <div>Order: {open.order_number}{open.parent_order_number ? ` (parent ${open.parent_order_number})` : ""}</div>
              <div>
                Our PO:{" "}
                {open.po_id ? (
                  <a className="underline" href={`/admin/distributors/autodist?po=${open.po_id}`}>
                    {open.po_number}
                  </a>
                ) : (
                  open.po_number || "—"
                )}
              </div>
              <div>eBay order: {open.ebay_order_id || "—"}</div>
              <div>
                Ship to: {open.ship_to_name} {[open.ship_to_city, open.ship_to_state, open.ship_to_zip].filter(Boolean).join(", ")}
              </div>
              <div>
                Carrier: {open.carrier_code || "—"}
                {open.tracking_number ? ` · ${open.tracking_number}` : ""}
              </div>
              <div>Parts: {money(open.item_amount)}</div>
              <div>Shipping and handling: {money(open.special_charges)}</div>
              <div className="font-medium">Invoice total: {money(open.invoice_amount)}</div>
              <div>Order value: {money(open.order_value)}</div>
            </div>
            {open.invoice_amount != null && open.order_value != null && open.invoice_amount < open.order_value && (
              <Notice kind="success">Invoiced for less than the order value: part of the order is probably still backordered and will be invoiced later.</Notice>
            )}
            <table className="w-full text-left text-xs">
              <thead className="text-gray-500">
                <tr>
                  <th className="py-1 pr-2">Part</th>
                  <th className="py-1 pr-2">Description</th>
                  <th className="py-1 pr-2">Ordered</th>
                  <th className="py-1 pr-2">Shipped</th>
                  <th className="py-1 pr-2">Backordered</th>
                  <th className="py-1 pr-2 text-right">Price</th>
                  <th className="py-1">From</th>
                </tr>
              </thead>
              <tbody>
                {open.lines.map((l, i) => (
                  <tr key={i} className="border-t border-gray-100">
                    <td className="py-1 pr-2 font-mono">{l.number}</td>
                    <td className="py-1 pr-2">{l.description}</td>
                    <td className="py-1 pr-2">{qty(l.ordered)}</td>
                    <td className="py-1 pr-2">{qty(l.shipped)}</td>
                    <td className="py-1 pr-2">{qty(l.backordered)}</td>
                    <td className="py-1 pr-2 text-right">{money(Number(l.price))}</td>
                    <td className="py-1">{l.warehouse ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="flex justify-end">
              <button type="button" className={btnCls} onClick={() => setOpen(null)}>
                Close
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}
