"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
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
type Note = "" | "saving" | "saved";

const LIMIT = 50;
const SAVE_DELAY_MS = 700;
const isLow = (r: Row) => (r.reorder_point ?? 0) > 0 && r.quantity_on_hand <= (r.reorder_point ?? 0);

/** Every product with its stock, manual count changes, restock levels, and the list of what needs reordering. */
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

  // Merge only the field that changed, so a slow save can never overwrite a newer edit to another field.
  function onUpdate(id: number, patch: Partial<Row>) {
    setData((prev) => ({ ...prev, items: prev.items.map((r) => (r.id === id ? { ...r, ...patch } : r)) }));
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
        Every part and how many you have. Use the arrows or type the real number; changes save by themselves a moment
        after you stop. Set a restock level on a part and it shows up under Low stock when you reach or drop below it.
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
  onUpdate: (id: number, patch: Partial<Row>) => void;
  onError: (msg: string) => void;
}) {
  const [count, setCount] = useState(String(row.quantity_on_hand));
  const [level, setLevel] = useState(row.reorder_point ? String(row.reorder_point) : "");
  const [countNote, setCountNote] = useState<Note>("");
  const [levelNote, setLevelNote] = useState<Note>("");

  // Always the latest row, so async handlers never act on a stale copy.
  const rowRef = useRef(row);
  rowRef.current = row;

  // Per-field save bookkeeping.
  //  timer: pending debounced save; seq: newest request (older replies are ignored);
  //  dirty: the box holds something the server hasn't confirmed, so don't overwrite it;
  //  sent: the last value we asked the server to store.
  const countTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const levelTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const countSeq = useRef(0);
  const levelSeq = useRef(0);
  const countDirty = useRef(false);
  const levelDirty = useRef(false);
  const sentCount = useRef(row.quantity_on_hand);
  const sentLevel = useRef(row.reorder_point ?? 0);

  // Follow the server's value, but never while you're mid-edit.
  useEffect(() => {
    sentCount.current = row.quantity_on_hand;
    if (!countDirty.current) setCount(String(row.quantity_on_hand));
  }, [row.quantity_on_hand]);
  useEffect(() => {
    sentLevel.current = row.reorder_point ?? 0;
    if (!levelDirty.current) setLevel(row.reorder_point ? String(row.reorder_point) : "");
  }, [row.reorder_point]);

  function flash(set: (fn: (n: Note) => Note) => void) {
    set(() => "saved");
    setTimeout(() => set((n) => (n === "saved" ? "" : n)), 1500);
  }

  // ----- on hand -----
  async function saveCount(text: string, final: boolean) {
    const n = Number(text);
    if (text.trim() === "" || !Number.isInteger(n) || n < 0) {
      if (final) {
        countDirty.current = false;
        setCount(String(sentCount.current));
      }
      return; // while typing, wait for a real number
    }
    if (n === sentCount.current) {
      if (final) countDirty.current = false;
      return;
    }
    const seq = ++countSeq.current;
    sentCount.current = n;
    setCountNote("saving");
    onError("");
    try {
      const r = await api<{ quantity_on_hand: number }>(`/inventory/products/${row.id}/adjust`, {
        method: "POST",
        body: { set_to: n },
      });
      if (seq !== countSeq.current) return;
      if (!countTimer.current) countDirty.current = false;
      sentCount.current = r.quantity_on_hand;
      onUpdate(row.id, { quantity_on_hand: r.quantity_on_hand });
      flash(setCountNote);
    } catch (e) {
      if (seq !== countSeq.current) return;
      onError(errMsg(e));
      countDirty.current = false;
      sentCount.current = rowRef.current.quantity_on_hand;
      setCount(String(rowRef.current.quantity_on_hand));
      setCountNote("");
    }
  }

  function editCount(v: string) {
    setCount(v);
    countDirty.current = true;
    if (countTimer.current) clearTimeout(countTimer.current);
    countTimer.current = setTimeout(() => {
      countTimer.current = null;
      saveCount(v, false);
    }, SAVE_DELAY_MS);
  }

  function finishCount() {
    if (countTimer.current) {
      clearTimeout(countTimer.current);
      countTimer.current = null;
    }
    saveCount(count, true);
  }

  // ----- restock level -----
  async function saveLevel(text: string, final: boolean) {
    const t = text.trim();
    const n = t === "" ? 0 : Number(t); // empty = no restock level
    if (!Number.isInteger(n) || n < 0) {
      if (final) {
        levelDirty.current = false;
        setLevel(sentLevel.current ? String(sentLevel.current) : "");
      }
      return;
    }
    if (n === sentLevel.current) {
      if (final) levelDirty.current = false;
      return;
    }
    const seq = ++levelSeq.current;
    sentLevel.current = n;
    setLevelNote("saving");
    onError("");
    try {
      const r = await api<{ reorder_point: number | null }>(`/inventory/products/${row.id}/reorder-point`, {
        method: "PUT",
        body: { reorder_point: n === 0 ? null : n },
      });
      if (seq !== levelSeq.current) return;
      if (!levelTimer.current) levelDirty.current = false;
      sentLevel.current = r.reorder_point ?? 0;
      onUpdate(row.id, { reorder_point: r.reorder_point });
      flash(setLevelNote);
    } catch (e) {
      if (seq !== levelSeq.current) return;
      onError(errMsg(e));
      levelDirty.current = false;
      sentLevel.current = rowRef.current.reorder_point ?? 0;
      setLevel(rowRef.current.reorder_point ? String(rowRef.current.reorder_point) : "");
      setLevelNote("");
    }
  }

  function editLevel(v: string) {
    setLevel(v);
    levelDirty.current = true;
    if (levelTimer.current) clearTimeout(levelTimer.current);
    levelTimer.current = setTimeout(() => {
      levelTimer.current = null;
      saveLevel(v, false);
    }, SAVE_DELAY_MS);
  }

  function finishLevel() {
    if (levelTimer.current) {
      clearTimeout(levelTimer.current);
      levelTimer.current = null;
    }
    saveLevel(level, true);
  }

  const enterBlurs = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") (e.target as HTMLInputElement).blur();
  };
  // Scrolling the page over a focused number box would otherwise change the number.
  const noWheel = (e: React.WheelEvent<HTMLInputElement>) => (e.target as HTMLInputElement).blur();

  const noteText = (n: Note) => (n === "saving" ? "Saving…" : n === "saved" ? "Saved" : "");
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
      <td className="whitespace-nowrap px-3 py-2">
        {canEdit ? (
          <div className="flex items-center gap-2">
            <input
              type="number"
              min={0}
              className={`${inputCls} w-24 text-center`}
              value={count}
              onChange={(e) => editCount(e.target.value)}
              onBlur={finishCount}
              onKeyDown={enterBlurs}
              onWheel={noWheel}
            />
            <span className="w-14 text-xs text-gray-500">{noteText(countNote)}</span>
          </div>
        ) : (
          row.quantity_on_hand
        )}
      </td>
      <td className="whitespace-nowrap px-3 py-2">
        {canEdit ? (
          <div className="flex items-center gap-2">
            <input
              type="number"
              min={0}
              className={`${inputCls} w-24`}
              value={level}
              placeholder="none"
              onChange={(e) => editLevel(e.target.value)}
              onBlur={finishLevel}
              onKeyDown={enterBlurs}
              onWheel={noWheel}
            />
            <span className="w-14 text-xs text-gray-500">{noteText(levelNote)}</span>
          </div>
        ) : (
          row.reorder_point ?? "—"
        )}
      </td>
      {showShort && <td className="px-3 py-2 font-semibold">{Math.max(0, (row.reorder_point ?? 0) - row.quantity_on_hand)}</td>}
      <td className="whitespace-nowrap px-3 py-2">
        <span className="whitespace-nowrap">
          {out ? <Badge tone="amber">Out of stock</Badge> : low ? <Badge tone="amber">Low</Badge> : <Badge tone="green">Ok</Badge>}
        </span>
      </td>
    </tr>
  );
}
