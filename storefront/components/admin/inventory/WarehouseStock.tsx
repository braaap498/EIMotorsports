"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Notice, Pager, inputCls, btn2Cls } from "../ui";
import ProductPicker, { type PickRow } from "./ProductPicker";

type Item = { id: number; sku: string; title: string; quantity: number; bin_location: string | null; updated_at: string; updated_by: string | null };
const LIMIT = 50;

/** What a warehouse other than the Main one holds. Parts are added with the picker; the count is typed and saved. */
export default function WarehouseStock({ warehouseId, name, canEdit, active }: { warehouseId: number; name: string; canEdit: boolean; active: boolean }) {
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const [items, setItems] = useState<Item[]>([]);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState("");
  const [adding, setAdding] = useState(false);
  const [picked, setPicked] = useState<PickRow | null>(null);
  const [qty, setQty] = useState("");
  const [busy, setBusy] = useState(false);
  const seq = useRef(0);

  const load = useCallback(() => {
    const my = ++seq.current;
    const p = new URLSearchParams({ limit: String(LIMIT), offset: String(offset) });
    if (q.trim()) p.set("q", q.trim());
    api<{ total: number; items: Item[] }>(`/inventory/warehouses/${warehouseId}/stock?${p.toString()}`)
      .then((r) => {
        if (my !== seq.current) return;
        setItems(r.items);
        setTotal(r.total);
        setError("");
      })
      .catch((e) => my === seq.current && setError(errMsg(e)));
  }, [warehouseId, q, offset]);

  useEffect(() => {
    setOffset(0);
    setPicked(null);
    setAdding(false);
  }, [warehouseId]);
  useEffect(() => {
    if (!active) return;
    const t = setTimeout(load, q ? 300 : 0);
    return () => clearTimeout(t);
  }, [active, load, q]);

  async function setCount(productId: number, n: number) {
    setBusy(true);
    setError("");
    try {
      await api(`/inventory/warehouses/${warehouseId}/stock/${productId}`, { method: "PUT", body: { quantity: n } });
      load();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  async function addPart() {
    const n = parseInt(qty, 10);
    if (!picked || !Number.isFinite(n) || n < 1) return setError("Pick a part and enter how many are in this warehouse.");
    await setCount(picked.id, n);
    setPicked(null);
    setQty("");
    setAdding(false);
  }

  return (
    <div className="space-y-4">
      <p className="text-sm text-gray-600">
        Parts held in <strong>{name}</strong>. Every warehouse counts toward what the website and eBay can sell. Picking takes
        from the Main warehouse first and only draws on the others when Main runs out of that part. Changing a count here
        changes the total on hand by the same amount and is written to the stock history.
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <input
          className={`${inputCls} min-w-[16rem] flex-1`}
          placeholder="Search title, SKU or bin"
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setOffset(0);
          }}
        />
        {canEdit && (
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
                <input className={`${inputCls} w-28`} type="number" min={1} value={qty} onChange={(e) => setQty(e.target.value)} />
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
      {items.length === 0 && !error && <p className="text-sm text-gray-500">No parts in this warehouse yet.</p>}
      {items.length > 0 && (
        <div className="overflow-x-auto rounded border border-gray-200 bg-white">
          <table className="w-full text-left text-sm">
            <thead className="bg-gray-50 text-xs uppercase text-gray-500">
              <tr>
                <th className="px-3 py-2">SKU</th>
                <th className="px-3 py-2">Part</th>
                <th className="px-3 py-2">Bin</th>
                <th className="px-3 py-2">Count</th>
                <th className="px-3 py-2">Last changed</th>
                <th className="px-3 py-2"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {items.map((r) => (
                <CountRow key={`${r.id}-${r.quantity}`} r={r} canEdit={canEdit} busy={busy} onSave={(n) => void setCount(r.id, n)} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Pager total={total} limit={LIMIT} offset={offset} onChange={setOffset} />
    </div>
  );
}

function CountRow({ r, canEdit, busy, onSave }: { r: Item; canEdit: boolean; busy: boolean; onSave: (n: number) => void }) {
  const [v, setV] = useState(String(r.quantity));
  const n = parseInt(v, 10);
  const dirty = Number.isFinite(n) && n >= 0 && n !== r.quantity;
  return (
    <tr>
      <td className="whitespace-nowrap px-3 py-2 font-mono text-xs">{r.sku}</td>
      <td className="px-3 py-2">{r.title}</td>
      <td className="px-3 py-2">{r.bin_location ?? "—"}</td>
      <td className="px-3 py-2">
        {canEdit ? (
          <input className={`${inputCls} w-24`} type="number" min={0} value={v} onChange={(e) => setV(e.target.value)} onKeyDown={(e) => e.key === "Enter" && dirty && onSave(n)} />
        ) : (
          r.quantity
        )}
      </td>
      <td className="px-3 py-2 text-xs text-gray-500">
        {new Date(r.updated_at).toLocaleString()}
        {r.updated_by ? ` · ${r.updated_by}` : ""}
      </td>
      <td className="whitespace-nowrap px-3 py-2">
        {canEdit && dirty && (
          <button type="button" className={btn2Cls} disabled={busy} onClick={() => onSave(n)}>
            Save
          </button>
        )}
        {canEdit && (
          <button type="button" className="ml-2 text-xs text-red-700 underline disabled:opacity-40" disabled={busy} onClick={() => onSave(0)}>
            Remove
          </button>
        )}
      </td>
    </tr>
  );
}
