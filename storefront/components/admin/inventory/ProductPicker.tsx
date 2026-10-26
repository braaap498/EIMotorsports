"use client";

import { useEffect, useRef, useState } from "react";
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

/**
 * Search by title, SKU, part number, bin or any linked barcode, then click the product.
 * With `browse`, the parts are listed (A to Z) before anything is typed, and typing narrows the list.
 */
export default function ProductPicker({ onPick, browse = false }: { onPick: (p: PickRow) => void; browse?: boolean }) {
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<PickRow[] | null>(null);
  const [error, setError] = useState("");
  const MAX_RESULTS = 50; // the most the server will return for one request
  const [busy, setBusy] = useState(false);
  const [more, setMore] = useState(false);
  const seq = useRef(0); // so a slow, older answer can't replace a newer one

  async function fetchRows(term: string, offset: number) {
    const id = ++seq.current;
    setBusy(true);
    setError("");
    try {
      const r = await api<{ items: PickRow[] }>(
        `/inventory/products/search?q=${encodeURIComponent(term)}&limit=${MAX_RESULTS}&offset=${offset}`,
      );
      if (id !== seq.current) return;
      setRows((prev) => (offset > 0 && prev ? [...prev, ...r.items] : r.items));
      setMore(r.items.length >= MAX_RESULTS);
    } catch (err) {
      if (id === seq.current) setError(errMsg(err));
    } finally {
      if (id === seq.current) setBusy(false);
    }
  }

  // Browse mode: list everything at first, then refine as the person types.
  useEffect(() => {
    if (!browse) return;
    const t = q.trim();
    if (t.length === 1) return; // wait for a second character
    const h = setTimeout(() => fetchRows(t, 0), t ? 300 : 0);
    return () => clearTimeout(h);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, browse]);

  function search(e: React.FormEvent) {
    e.preventDefault();
    const t = q.trim();
    if (browse ? t.length === 1 : t.length < 2) return;
    fetchRows(t, 0);
  }

  const typed = q.trim().length > 0;

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
      {browse && rows && rows.length > 0 && (
        <p className="text-xs text-gray-500">
          {typed ? "Parts matching your search." : "All parts, A to Z. Type above to narrow the list."} Click a part to work on it.
        </p>
      )}
      {browse && !rows && !error && <p className="text-sm text-gray-500">Loading parts…</p>}
      {rows && rows.length === 0 && (
        <p className="text-sm text-gray-500">{typed ? "Nothing found. Try fewer words." : "There are no parts yet."}</p>
      )}
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
      {browse && rows && more && (
        <button type="button" className={btn2Cls} disabled={busy} onClick={() => fetchRows(q.trim(), rows.length)}>
          {busy ? "Loading…" : "Show more"}
        </button>
      )}
      {!browse && rows && rows.length >= MAX_RESULTS && (
        <p className="text-xs text-gray-500">Showing the first {MAX_RESULTS} matches. Add more words to narrow it down.</p>
      )}
    </div>
  );
}
