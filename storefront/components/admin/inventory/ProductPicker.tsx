"use client";

import { useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Notice, btn2Cls, inputCls } from "../ui";

export type PickRow = {
  id: number;
  sku: string;
  title: string;
  quantity_on_hand: number;
  bin_location: string | null;
  barcode_count: number;
};

/** Search by title, SKU, part number, bin or any linked barcode, then click the product. */
export default function ProductPicker({ onPick }: { onPick: (p: PickRow) => void }) {
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<PickRow[] | null>(null);
  const [error, setError] = useState("");
  const MAX_RESULTS = 50; // the most the server will return for one search
  const [busy, setBusy] = useState(false);

  async function search(e: React.FormEvent) {
    e.preventDefault();
    if (q.trim().length < 2) return;
    setBusy(true);
    setError("");
    try {
      const r = await api<{ items: PickRow[] }>(`/inventory/products/search?q=${encodeURIComponent(q.trim())}&limit=${MAX_RESULTS}`);
      setRows(r.items);
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-2">
      <form onSubmit={search} className="flex gap-2">
        <input
          className={inputCls}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search by title, SKU, part number, bin or barcode (e.g. Tusk 1.8 bar)"
        />
        <button className={btn2Cls} disabled={busy}>
          {busy ? "…" : "Search"}
        </button>
      </form>
      {error && <Notice kind="error">{error}</Notice>}
      {rows && rows.length === 0 && <p className="text-sm text-gray-500">Nothing found. Try fewer words.</p>}
      {rows && rows.length > 0 && (
        <ul
          className="divide-y divide-gray-100 overflow-y-auto rounded border border-gray-200 bg-white"
          style={{ maxHeight: "60vh" }}
        >
          {rows.map((r) => (
            <li key={r.id}>
              <button type="button" onClick={() => onPick(r)} className="block w-full px-3 py-2 text-left text-sm hover:bg-gray-50">
                <span className="font-medium">{r.title}</span>
                <span className="block text-xs text-gray-500">
                  {r.sku} · on hand {r.quantity_on_hand ?? 0}
                  {r.bin_location ? ` · bin ${r.bin_location}` : ""} · {r.barcode_count} barcode
                  {r.barcode_count === 1 ? "" : "s"}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {rows && rows.length >= MAX_RESULTS && (
        <p className="text-xs text-gray-500">Showing the first {MAX_RESULTS} matches. Add more words to narrow it down.</p>
      )}
    </div>
  );
}
