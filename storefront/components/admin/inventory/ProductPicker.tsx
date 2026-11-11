"use client";

import ClearableInput from "../ClearableInput";
import { useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Notice, Pager, btn2Cls, inputCls } from "../ui";

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
 * With `paged` as well, the list is split into pages with a pager above and below, instead of a "Show more" button.
 */
export default function ProductPicker({ onPick, browse = false, paged = false }: { onPick: (p: PickRow) => void; browse?: boolean; paged?: boolean }) {
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<PickRow[] | null>(null);
  const [error, setError] = useState("");
  const MAX_RESULTS = 50; // the most the server will return for one request
  const [busy, setBusy] = useState(false);
  const [more, setMore] = useState(false);
  const seq = useRef(0); // so a slow, older answer can't replace a newer one
  const [offset, setOffset] = useState(0); // paged mode: where the current page starts
  const [total, setTotal] = useState(0);
  const topRef = useRef<HTMLDivElement>(null);
  const firstOffset = useRef(true);

  async function fetchRows(term: string, offset: number) {
    const id = ++seq.current;
    setBusy(true);
    setError("");
    try {
      const r = await api<{ items: PickRow[]; total?: number }>(
        `/inventory/products/search?q=${encodeURIComponent(term)}&limit=${MAX_RESULTS}&offset=${offset}`,
      );
      if (id !== seq.current) return;
      setTotal(r.total ?? r.items.length);
      setRows((prev) => (!paged && offset > 0 && prev ? [...prev, ...r.items] : r.items));
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
    const start = paged ? offset : 0;
    const h = setTimeout(() => fetchRows(t, start), t && start === 0 ? 300 : 0); // wait while typing, but not when turning a page
    return () => clearTimeout(h);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, browse, paged, offset]);

  // after turning a page, bring the top of the list back into view
  useEffect(() => {
    if (firstOffset.current) {
      firstOffset.current = false;
      return;
    }
    topRef.current?.scrollIntoView({ block: "nearest" });
  }, [offset]);

  function search(e: React.FormEvent) {
    e.preventDefault();
    const t = q.trim();
    if (browse ? t.length === 1 : t.length < 2) return;
    setOffset(0);
    fetchRows(t, 0);
  }

  const typed = q.trim().length > 0;

  return (
    <div ref={topRef} className="space-y-2">
      <form onSubmit={search} className="flex gap-2">
        <ClearableInput
          className={inputCls}
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setOffset(0);
          }}
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
      {paged && rows && rows.length > 0 && <Pager total={total} limit={MAX_RESULTS} offset={offset} onChange={setOffset} />}
      {rows && rows.length > 0 && (
        <ul
          className={`divide-y divide-gray-100 rounded border border-gray-200 bg-white ${paged ? "" : "overflow-y-auto"}`}
          style={paged ? undefined : { maxHeight: "60vh" }}
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
      {paged && rows && rows.length > 0 && <Pager total={total} limit={MAX_RESULTS} offset={offset} onChange={setOffset} />}
      {browse && !paged && rows && more && (
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
