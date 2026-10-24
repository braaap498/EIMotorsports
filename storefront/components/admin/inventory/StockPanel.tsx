"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Badge, Notice, Pager, btn2Cls, inputCls } from "../ui";
import { useAccess } from "../AdminAccess";

type Row = {
  id: number;
  sku: string;
  title: string;
  quantity_on_hand: number;
  reorder_point: number | null;
  bin_location: string | null;
  is_active: boolean;
};
type Resp = { total: number; low_count: number; out_count: number; items: Row[] };
type View = "all" | "low" | "out";

const LIMIT = 50;
const isLow = (r: Row) => (r.reorder_point ?? 0) > 0 && r.quantity_on_hand <= (r.reorder_point ?? 0);

/** Every product with its stock, manual +/-, restock levels, and the list of what needs reordering. */
export default function StockPanel({ active }: { active: boolean }) {
  const { can } = useAccess();
  const canEdit = can("inventory.adjust");
  const [view, setView] = useState<View>("all");
  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState("");
  const [inactive, setInactive] = useState(false);
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<Resp>({ total: 0, low_count: 0, out_count: 0, items: [] });
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);

  // Wait for a pause in typing before searching.
  useEffect(() => {
    const t = setTimeout(() => {
      setQ(qInput.trim());
      setOffset(0);
    }, 300);
    return () => clearTimeout(t);
  }, [qInput]);

  const load = useCallback(() => {
    const p = new URLSearchParams({ limit: String(LIMIT), offset: String(offset), view });
    if (q) p.set("q", q);
    if (inactive) p.set("include_inactive", "true");
    api<Resp>(`/inventory/stock?${p}`)
      .then((d) => {
        setData(d);
        setError("");
        setLoaded(true);
      })
      .catch((e) => setError(errMsg(e)));
  }, [offset, view, q, inactive]);

  useEffect(() => {
    if (active) load();
  }, [active, load]);

  // After a change, update just the badge counts. The rows stay put so you can keep clicking.
  const refreshCounts = useCallback(() => {
    const p = new URLSearchParams({ limit: "1", offset: "0", view: "all" });
    if (inactive) p.set("include_inactive", "true");
    api<Resp>(`/inventory/stock?${p}`)
      .then((d) => setData((prev) => ({ ...prev, low_count: d.low_count, out_count: d.out_count })))
      .catch(() => {});
  }, [inactive]);

  function onUpdate(row: Row) {
    setData((prev) => ({ ...prev, items: prev.items.map((r) => (r.id === row.id ? row : r)) }));
    refreshCounts();
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

  return (
    <div className="space-y-4">
      <p className="text-sm text-gray-600">
        Every part and how many you have. Use − and + to change a count by one, or type the real number and press Enter.
        Set a restock level on a part and it shows up under Low stock when you reach or drop below it.
      </p>

      <div className="flex flex-wrap items-center gap-3">
        <div className="inline-flex overflow-hidden rounded border border-gray-300 text-sm">
          {tabBtn("all", "All")}
          {tabBtn("low", `Low stock (${data.low_count})`)}
          {tabBtn("out", `Out of stock (${data.out_count})`)}
        </div>
        <input
          className={`${inputCls} max-w-xs`}
          value={qInput}
          onChange={(e) => setQInput(e.target.value)}
          placeholder="Search title, SKU, part number, bin or barcode"
        />
        <label className="flex items-center gap-2 text-sm text-gray-600">
          <input
            type="checkbox"
            checked={inactive}
            onChange={(e) => {
              setInactive(e.target.checked);
              setOffset(0);
            }}
          />
          Include inactive
        </label>
        <button type="button" className={btn2Cls} onClick={load}>
          Refresh
        </button>
      </div>

      {error && <Notice kind="error">{error}</Notice>}

      <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white">
        <table className="w-full text-left text-sm">
          <thead className="bg-gray-50 text-xs uppercase text-gray-500">
            <tr>
              <th className="px-3 py-2">Part</th>
              <th className="px-3 py-2">Bin</th>
              <th className="px-3 py-2">On hand</th>
              <th className="px-3 py-2">Restock level</th>
              {view === "low" && <th className="px-3 py-2">Short by</th>}
              <th className="px-3 py-2">Status</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {data.items.map((r) => (
              <StockRow key={r.id} row={r} canEdit={canEdit} showShort={view === "low"} onUpdate={onUpdate} onError={setError} />
            ))}
            {loaded && data.items.length === 0 && (
              <tr>
                <td colSpan={view === "low" ? 6 : 5} className="px-3 py-6 text-center text-gray-500">
                  {view === "low"
                    ? "Nothing is at or below its restock level."
                    : view === "out"
                    ? "Nothing is out of stock."
                    : "No parts match."}
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

function StockRow({
  row,
  canEdit,
  showShort,
  onUpdate,
  onError,
}: {
  row: Row;
  canEdit: boolean;
  showShort: boolean;
  onUpdate: (r: Row) => void;
  onError: (msg: string) => void;
}) {
  const [count, setCount] = useState(String(row.quantity_on_hand));
  const [level, setLevel] = useState(row.reorder_point ? String(row.reorder_point) : "");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setCount(String(row.quantity_on_hand));
  }, [row.quantity_on_hand]);
  useEffect(() => {
    setLevel(row.reorder_point ? String(row.reorder_point) : "");
  }, [row.reorder_point]);

  async function adjust(body: { delta?: number; set_to?: number }) {
    setBusy(true);
    onError("");
    try {
      const r = await api<{ quantity_on_hand: number }>(`/inventory/products/${row.id}/adjust`, {
        method: "POST",
        body,
      });
      onUpdate({ ...row, quantity_on_hand: r.quantity_on_hand });
    } catch (e) {
      onError(errMsg(e));
      setCount(String(row.quantity_on_hand));
    } finally {
      setBusy(false);
    }
  }

  function commitCount() {
    const n = Number(count);
    if (count.trim() === "" || !Number.isInteger(n) || n < 0) {
      setCount(String(row.quantity_on_hand));
      return;
    }
    if (n !== row.quantity_on_hand) adjust({ set_to: n });
  }

  async function commitLevel() {
    const t = level.trim();
    const n = t === "" ? null : Number(t);
    if (n !== null && (!Number.isInteger(n) || n < 0)) {
      setLevel(row.reorder_point ? String(row.reorder_point) : "");
      return;
    }
    if ((n ?? 0) === (row.reorder_point ?? 0)) return;
    setBusy(true);
    onError("");
    try {
      const r = await api<{ reorder_point: number | null }>(`/inventory/products/${row.id}/reorder-point`, {
        method: "PUT",
        body: { reorder_point: n },
      });
      onUpdate({ ...row, reorder_point: r.reorder_point });
    } catch (e) {
      onError(errMsg(e));
      setLevel(row.reorder_point ? String(row.reorder_point) : "");
    } finally {
      setBusy(false);
    }
  }

  const enterBlurs = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") (e.target as HTMLInputElement).blur();
  };
  const low = isLow(row);
  const out = row.quantity_on_hand <= 0;

  return (
    <tr>
      <td className="px-3 py-2">
        <Link href={`/admin/products/${row.id}`} className="font-medium hover:underline">
          {row.title}
        </Link>
        <div className="text-xs text-gray-500">
          {row.sku}
          {!row.is_active && " · inactive"}
        </div>
      </td>
      <td className="px-3 py-2 font-semibold">{row.bin_location ?? "—"}</td>
      <td className="px-3 py-2">
        {canEdit ? (
          <div className="flex items-center gap-1">
            <button
              type="button"
              className={btn2Cls}
              disabled={busy || row.quantity_on_hand <= 0}
              onClick={() => adjust({ delta: -1 })}
              aria-label="Take one off"
            >
              −
            </button>
            <input
              type="number"
              min={0}
              className={`${inputCls} w-20 text-center`}
              value={count}
              disabled={busy}
              onChange={(e) => setCount(e.target.value)}
              onBlur={commitCount}
              onKeyDown={enterBlurs}
            />
            <button type="button" className={btn2Cls} disabled={busy} onClick={() => adjust({ delta: 1 })} aria-label="Add one">
              +
            </button>
          </div>
        ) : (
          row.quantity_on_hand
        )}
      </td>
      <td className="px-3 py-2">
        {canEdit ? (
          <input
            type="number"
            min={0}
            className={`${inputCls} w-24`}
            value={level}
            placeholder="none"
            disabled={busy}
            onChange={(e) => setLevel(e.target.value)}
            onBlur={commitLevel}
            onKeyDown={enterBlurs}
          />
        ) : (
          row.reorder_point ?? "—"
        )}
      </td>
      {showShort && <td className="px-3 py-2 font-semibold">{Math.max(0, (row.reorder_point ?? 0) - row.quantity_on_hand)}</td>}
      <td className="px-3 py-2">
        {out ? <Badge tone="amber">Out of stock</Badge> : low ? <Badge tone="amber">Low</Badge> : <Badge tone="green">OK</Badge>}
      </td>
    </tr>
  );
}
