"use client";

import ClearableInput from "../ClearableInput";
import ClickRow from "../ClickRow";
import Link from "next/link";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Badge, Notice, Pager, btn2Cls, inputCls } from "../ui";
import { SortTh, useSort } from "../SortHeader";
import ProductPicker, { type PickRow } from "./ProductPicker";

type Row = {
  id: number;
  sku: string;
  title: string;
  quantity: number; // on hand in this warehouse
  bin_location: string | null;
  reorder_point: number | null; // restock level for this warehouse
  is_active: boolean;
  reserved: number; // this warehouse's share of what is promised to paid orders and open checkouts
  available: number; // on hand here minus reserved here
  product_available: number; // what customers can buy across every warehouse
  on_order: boolean;
  has_open_po: boolean; // only a part on an unreceived purchase order can be Backordered
  expected_date: string | null; // "YYYY-MM-DD", typed by hand, shown to customers
  oversold: boolean;
};
type Resp = { total: number; low_count: number; out_count: number; items: Row[] };
type View = "all" | "low" | "out";
type Status = "all" | "active" | "inactive";
type Note = "" | "saving" | "saved";
type SortKey = "sku" | "part" | "bin" | "onhand" | "reserved" | "available" | "level" | "short" | "status";

const LIMIT = 50;
// Low = something left to sell but at or under the restock level. Nothing left is "Out", never both.
const isLow = (r: Row) => (r.reorder_point ?? 0) > 0 && r.available > 0 && r.available <= (r.reorder_point ?? 0);

/** One warehouse's parts: counts, bin/shelf, restock levels, and its own low and out of stock lists. */
export default function WarehouseStock({ warehouseId, name, isMain, canEdit, active }: { warehouseId: number | "all"; name: string; isMain: boolean; canEdit: boolean; active: boolean }) {
  const isAll = warehouseId === "all";
  const [view, setView] = useState<View>("all");
  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState("");
  const [status, setStatus] = useState<Status>("active");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<Resp>({ total: 0, low_count: 0, out_count: 0, items: [] });
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [adding, setAdding] = useState(false);
  const [picked, setPicked] = useState<PickRow | null>(null);
  const [qty, setQty] = useState("");
  const [busy, setBusy] = useState(false);
  const seq = useRef(0);
  const { sort, toggle } = useSort<SortKey>(); // nothing chosen = by part name (Low stock: furthest below first)
  const onSort = (k: SortKey) => {
    toggle(k);
    setOffset(0);
  };

  const hasAction = !isMain && !isAll;
  const colKeys = ["sku", "part", "bin", "onhand", "reserved", "available", "level", ...(view === "low" ? ["short"] : []), "status", ...(hasAction ? ["action"] : [])];

  // Every column is as wide as its widest content seen so far and never shrinks, so nothing shifts between pages or tabs.
  const tableRef = useRef<HTMLTableElement>(null);
  const [colW, setColW] = useState<Record<string, number>>({});
  useLayoutEffect(() => {
    const ths = tableRef.current?.querySelectorAll("thead th");
    if (!ths) return;
    const next = { ...colW };
    let changed = false;
    ths.forEach((th, i) => {
      const k = colKeys[i];
      const w = Math.ceil(th.getBoundingClientRect().width);
      // The Part column takes whatever room is left over, so only the others are held at their widest.
      if (k && k !== "part" && w > (next[k] ?? 0)) {
        next[k] = w;
        changed = true;
      }
    });
    if (changed) setColW(next);
  });

  // Wait for a pause in typing before searching.
  useEffect(() => {
    const t = setTimeout(() => {
      setQ(qInput.trim());
      setOffset(0);
    }, 300);
    return () => clearTimeout(t);
  }, [qInput]);

  const load = useCallback(() => {
    const my = ++seq.current;
    const p = new URLSearchParams({ limit: String(LIMIT), offset: String(offset), view, status });
    if (sort.key && !(sort.key === "short" && view !== "low")) {
      p.set("sort", sort.key);
      p.set("dir", sort.dir);
    }
    if (q) p.set("q", q);
    api<Resp>(`/inventory/warehouses/${warehouseId}/stock?${p}`)
      .then((d) => {
        if (my !== seq.current) return;
        setData(d);
        setError("");
        setLoaded(true);
      })
      .catch((e) => my === seq.current && setError(errMsg(e)));
  }, [warehouseId, offset, view, q, status, sort.key, sort.dir]);

  useEffect(() => {
    if (active) load();
  }, [active, load]);

  async function addPart() {
    const n = parseInt(qty, 10);
    if (isAll || !picked || !Number.isFinite(n) || n < 0) return setError("Pick a part and enter how many are in this warehouse (0 is fine).");
    setBusy(true);
    setError("");
    try {
      await api(`/inventory/warehouses/${warehouseId}/stock/${picked.id}`, { method: "PUT", body: { quantity: n } });
      setPicked(null);
      setQty("");
      setAdding(false);
      load();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  async function removePart(r: Row) {
    if (!window.confirm(`Remove ${r.sku} from ${name}?${r.quantity > 0 ? ` The ${r.quantity} counted here will also come off the total on hand.` : ""}`)) return;
    setBusy(true);
    setError("");
    try {
      await api(`/inventory/warehouses/${warehouseId}/stock/${r.id}`, { method: "DELETE" });
      load();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  const tabBtn = (v: View, label: string) => (
    <button
      type="button"
      className={`px-4 py-2 ${view === v ? "bg-black text-white" : "bg-white text-gray-700"}`}
      onClick={() => {
        setView(v);
        setOffset(0);
      }}
    >
      {label}
    </button>
  );
  const cols = (view === "low" ? 9 : 8) + (hasAction ? 1 : 0);

  return (
    <div className="space-y-4">
      <p className="text-sm text-gray-600">
        {isAll && "Totals across every warehouse, read-only: pick a warehouse to change a count, bin or restock level. "}“On hand” is what is physically on the shelf: type the real number and press Enter (or click away) to save it. Every
        change is recorded in History with who made it. “Reserved” is promised to paid orders that haven’t been picked yet,
        taken from the main warehouse first, and “Available” is what is left to sell. Set a restock level on a part and it
        shows up under Low stock when available reaches or drops below it (and under Out of stock once none are left).
        “Backordered” can be ticked once the part is on a purchase order.
      </p>

      <div className="flex flex-wrap items-center gap-3">
        <div className="inline-flex overflow-hidden rounded border border-gray-300 text-sm">
          {tabBtn("all", "All")}
          {tabBtn("low", `Low stock (${data.low_count})`)}
          {tabBtn("out", `Out of stock (${data.out_count})`)}
        </div>
        <ClearableInput
          className={`${inputCls} w-[24rem] max-w-full`}
          value={qInput}
          onChange={(e) => setQInput(e.target.value)}
          placeholder="Search title, SKU, part number, bin or barcode"
        />
        <label className="flex items-center gap-2 text-sm text-gray-600">
          Status
          <select
            className={`${inputCls} !w-auto`}
            value={status}
            onChange={(e) => {
              setStatus(e.target.value as Status);
              setOffset(0);
            }}
          >
            <option value="all">All</option>
            <option value="active">Active</option>
            <option value="inactive">Inactive</option>
          </select>
        </label>
        <button type="button" className={btn2Cls} onClick={load}>
          Refresh
        </button>
        {canEdit && !isMain && !isAll && (
          <button type="button" className={btn2Cls} onClick={() => setAdding((a) => !a)}>
            {adding ? "Close" : "Add part"}
          </button>
        )}
      </div>

      {error && <Notice kind="error">{error}</Notice>}

      {adding && (
        <div className="space-y-3 rounded border border-gray-200 bg-white p-3">
          {picked ? (
            <div className="flex flex-wrap items-end gap-3">
              <div className="text-sm">
                <div className="font-medium">{picked.sku}</div>
                <div className="text-gray-600">{picked.title}</div>
              </div>
              <label className="text-sm font-medium text-gray-700">
                <span className="mb-1 block">How many here</span>
                <input className={`${inputCls} w-28`} type="number" min={0} value={qty} onChange={(e) => setQty(e.target.value)} />
              </label>
              <button type="button" className="rounded bg-black px-4 py-2 text-sm font-semibold text-white disabled:opacity-40" disabled={busy} onClick={() => void addPart()}>
                Add to {name}
              </button>
              <button type="button" className={btn2Cls} onClick={() => setPicked(null)}>
                Pick a different part
              </button>
            </div>
          ) : (
            <ProductPicker browse onPick={setPicked} />
          )}
        </div>
      )}

      <Pager total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />

      <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white">
        <table ref={tableRef} className="w-full text-left text-sm">
          <colgroup>
            {colKeys.map((k) => (
              <col key={k} style={k === "part" ? { width: "100%" } : colW[k] ? { width: colW[k] } : undefined} />
            ))}
          </colgroup>
          <thead className="bg-gray-50 text-xs uppercase text-gray-500">
            <tr>
              <SortTh label="SKU" k="sku" sort={sort} onSort={onSort} />
              <SortTh label="Part" k="part" sort={sort} onSort={onSort} />
              <SortTh label="Bin/Shelf" k="bin" sort={sort} onSort={onSort} />
              <SortTh label="On hand" k="onhand" sort={sort} onSort={onSort} />
              <SortTh label="Reserved" k="reserved" sort={sort} onSort={onSort} />
              <SortTh label="Available" k="available" sort={sort} onSort={onSort} />
              <SortTh label="Restock level" k="level" sort={sort} onSort={onSort} />
              {view === "low" && <SortTh label="Short by" k="short" sort={sort} onSort={onSort} />}
              <SortTh label="Status" k="status" sort={sort} onSort={onSort} />
              {hasAction && <th className="px-3 py-2"></th>}
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {data.items.map((r) => (
              <StockRow key={r.id} row={r} warehouseId={warehouseId} hasAction={hasAction} canEdit={canEdit} busy={busy} showShort={view === "low"} onChanged={load} onError={setError} onRemove={() => void removePart(r)} />
            ))}
            {loaded && data.items.length === 0 && (
              <tr>
                <td colSpan={cols} className="px-3 py-6 text-center text-gray-500">
                  {view === "low"
                    ? "Nothing is at or below its restock level."
                    : view === "out"
                    ? "Nothing is out of stock."
                    : isMain || isAll
                    ? "No parts match."
                    : "No parts in this warehouse yet."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <Pager total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
    </div>
  );
}

/** A box that saves when you press Enter or click away (never while you are still typing), so History gets one line per change. */
function EditCell({
  value,
  canEdit,
  onSave,
  type,
  placeholder,
  widthCls,
  center,
}: {
  value: string;
  canEdit: boolean;
  onSave: (v: string) => Promise<boolean>;
  type: "text" | "number";
  placeholder?: string;
  widthCls: string;
  center?: boolean;
}) {
  const [text, setText] = useState(value);
  const [note, setNote] = useState<Note>("");
  const dirty = useRef(false);

  // Follow the server's value, but never while you're mid-edit.
  useEffect(() => {
    if (!dirty.current) setText(value);
  }, [value]);

  async function commit() {
    if (!dirty.current) return;
    dirty.current = false;
    const t = text.trim();
    if (t === value.trim()) {
      setText(value);
      return;
    }
    setNote("saving");
    const ok = await onSave(t);
    if (ok) {
      setNote("saved");
      setTimeout(() => setNote((n) => (n === "saved" ? "" : n)), 1500);
    } else {
      setText(value);
      setNote("");
    }
  }

  if (!canEdit) return <>{value || "—"}</>;
  return (
    <div className="flex items-center gap-2">
      <input
        type={type}
        min={type === "number" ? 0 : undefined}
        className={`${inputCls} ${widthCls} ${center ? "text-center" : ""}`}
        value={text}
        placeholder={placeholder}
        onChange={(e) => {
          dirty.current = true;
          setText(e.target.value);
        }}
        onBlur={() => void commit()}
        onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
        // Scrolling the page over a focused number box would otherwise change the number.
        onWheel={(e) => (e.target as HTMLInputElement).blur()}
      />
      <span className="w-14 shrink-0 text-xs text-gray-500">{note === "saving" ? "Saving…" : note === "saved" ? "Saved" : ""}</span>
    </div>
  );
}

function StockRow({
  row,
  warehouseId,
  hasAction,
  canEdit,
  busy,
  showShort,
  onChanged,
  onError,
  onRemove,
}: {
  row: Row;
  warehouseId: number | "all";
  hasAction: boolean;
  canEdit: boolean;
  busy: boolean;
  showShort: boolean;
  onChanged: () => void;
  onError: (msg: string) => void;
  onRemove: () => void;
}) {
  const base = `/inventory/warehouses/${warehouseId}/stock/${row.id}`;

  async function put(path: string, body: unknown): Promise<boolean> {
    onError("");
    try {
      await api(`${base}${path}`, { method: "PUT", body });
      onChanged();
      return true;
    } catch (e) {
      onError(errMsg(e));
      return false;
    }
  }
  const saveCount = (t: string) => {
    const n = Number(t);
    if (t === "" || !Number.isInteger(n) || n < 0) {
      onError("Type a whole number, 0 or more.");
      return Promise.resolve(false);
    }
    return put("", { quantity: n });
  };
  const saveLevel = (t: string) => {
    const n = t === "" ? 0 : Number(t); // empty = no restock level
    if (!Number.isInteger(n) || n < 0) {
      onError("Type a whole number, 0 or more (or leave it empty for none).");
      return Promise.resolve(false);
    }
    return put("/restock-level", { reorder_point: n === 0 ? null : n });
  };

  const low = isLow(row);
  const out = row.available <= 0;
  // Backordered is about the part as a whole: only say it when no warehouse has any left to sell.
  const backordered = out && row.on_order && row.product_available <= 0;

  const [onOrder, setOnOrder] = useState(row.on_order);
  useEffect(() => setOnOrder(row.on_order), [row.on_order]);
  const [date, setDate] = useState(row.expected_date ?? "");
  useEffect(() => setDate(row.expected_date ?? ""), [row.expected_date]);
  const [dateNote, setDateNote] = useState<Note>("");

  async function toggleOnOrder(next: boolean) {
    setOnOrder(next);
    onError("");
    try {
      const r = await api<{ on_order: boolean; expected_date: string | null }>(`/inventory/products/${row.id}/on-order`, {
        method: "PUT",
        body: { on_order: next },
      });
      setOnOrder(r.on_order);
      setDate(r.expected_date ?? "");
      onChanged();
    } catch (e) {
      setOnOrder(!next);
      onError(errMsg(e));
    }
  }

  async function saveDate(next: string) {
    setDate(next);
    setDateNote("saving");
    onError("");
    try {
      const r = await api<{ expected_date: string | null }>(`/inventory/products/${row.id}/expected-date`, {
        method: "PUT",
        body: { expected_date: next || null },
      });
      setDate(r.expected_date ?? "");
      setDateNote("saved");
      setTimeout(() => setDateNote((n) => (n === "saved" ? "" : n)), 1500);
      onChanged();
    } catch (e) {
      setDate(row.expected_date ?? "");
      setDateNote("");
      onError(errMsg(e));
    }
  }

  return (
    <ClickRow href={`/admin/products/${row.id}`}>
      <td className="whitespace-nowrap px-3 py-2">{row.sku}</td>
      <td className="min-w-[14rem] px-3 py-2">
        <Link href={`/admin/products/${row.id}`} className="font-medium hover:underline">
          {row.title}
        </Link>
        {!row.is_active && <div className="text-gray-500">inactive</div>}
      </td>
      <td className="whitespace-nowrap px-3 py-2 font-semibold">{row.bin_location || "—"}</td>
      <td className="whitespace-nowrap px-3 py-2">
        <EditCell value={String(row.quantity)} canEdit={canEdit} onSave={saveCount} type="number" widthCls="w-[5.5rem] min-w-[5.5rem] shrink-0" />
      </td>
      <td className="whitespace-nowrap px-3 py-2 text-gray-700">{row.reserved}</td>
      <td className="whitespace-nowrap px-3 py-2 font-semibold">{row.available}</td>
      <td className="whitespace-nowrap px-3 py-2">
        <EditCell value={row.reorder_point ? String(row.reorder_point) : ""} canEdit={canEdit} onSave={saveLevel} type="number" placeholder="none" widthCls="w-[5.5rem] min-w-[5.5rem] shrink-0" />
      </td>
      {showShort && <td className="whitespace-nowrap px-3 py-2 font-semibold">{Math.max(0, (row.reorder_point ?? 0) - row.available)}</td>}
      <td className="whitespace-nowrap px-3 py-2">
        <span className="flex flex-wrap items-center gap-1 whitespace-nowrap">
          {row.oversold && <span className="rounded bg-red-100 px-2 py-0.5 text-xs font-semibold text-red-800">OVERSOLD</span>}
          {out ? <Badge tone="amber">{backordered ? "Backordered" : "Out of stock"}</Badge> : low ? <Badge tone="amber">Low</Badge> : <Badge tone="green">Ok</Badge>}
        </span>
        {/* Only a part that is on a purchase order can be Backordered. Creating the PO ticks it by default. */}
        {canEdit && row.has_open_po && (
          <div className="mt-1 space-y-1 text-xs text-gray-600">
            <label className="flex items-center gap-1">
              <input type="checkbox" checked={onOrder} onChange={(e) => void toggleOnOrder(e.target.checked)} />
              Backordered (on a PO)
            </label>
            {onOrder && (
              <label className="flex items-center gap-1">
                Expected
                <input type="date" className={`${inputCls} w-36 py-0.5 text-xs`} value={date} onChange={(e) => void saveDate(e.target.value)} />
                <span className="w-12 text-gray-500">{dateNote === "saving" ? "Saving…" : dateNote === "saved" ? "Saved" : ""}</span>
              </label>
            )}
          </div>
        )}
        {!canEdit && onOrder && row.expected_date && <div className="mt-1 text-xs text-gray-600">Expected {row.expected_date}</div>}
      </td>
      {hasAction && (
        <td className="whitespace-nowrap px-3 py-2">
          {canEdit && (
            <button type="button" className="text-xs text-red-700 underline disabled:opacity-40" disabled={busy} onClick={onRemove}>
              Remove
            </button>
          )}
        </td>
      )}
    </ClickRow>
  );
}
