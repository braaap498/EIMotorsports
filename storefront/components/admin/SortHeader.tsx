"use client";

import { useState } from "react";

/* Click-to-sort helpers for the admin lists.
   Same look as the Customers and Products lists: the column being sorted shows a dark ▲ or ▼,
   every other column shows grayed-out ▲▼. Nothing is sorted until a heading is clicked. */

export type SortState<K extends string> = { key: K | null; dir: "asc" | "desc" };

export function useSort<K extends string>() {
  const [sort, setSort] = useState<SortState<K>>({ key: null, dir: "asc" });
  const toggle = (k: K) =>
    setSort((s) => (s.key === k ? { key: k, dir: s.dir === "asc" ? "desc" : "asc" } : { key: k, dir: "asc" }));
  return { sort, toggle };
}

type Cell = string | number | null | undefined;

/** Returns a sorted copy (or the same order when nothing is chosen). Blanks always sort last; numbers sort as numbers. */
export function sortRows<T, K extends string>(rows: T[], sort: SortState<K>, getters: Record<K, (r: T) => Cell>): T[] {
  if (!sort.key) return rows;
  const get = getters[sort.key];
  const sign = sort.dir === "asc" ? 1 : -1;
  const idx = new Map(rows.map((r, i) => [r, i] as const));
  return [...rows].sort((a, b) => {
    const x = get(a);
    const y = get(b);
    const xe = x == null || x === "";
    const ye = y == null || y === "";
    if (xe || ye) return xe && ye ? (idx.get(a) ?? 0) - (idx.get(b) ?? 0) : xe ? 1 : -1;
    const c =
      typeof x === "number" && typeof y === "number"
        ? x - y
        : String(x).localeCompare(String(y), undefined, { numeric: true, sensitivity: "base" });
    return c !== 0 ? c * sign : (idx.get(a) ?? 0) - (idx.get(b) ?? 0);
  });
}

function Arrows({ active, dir }: { active: boolean; dir: "asc" | "desc" }) {
  return <span className={`text-[10px] ${active ? "" : "text-gray-300"}`}>{active ? (dir === "asc" ? "▲" : "▼") : "▲▼"}</span>;
}

/** A sortable <th>. */
export function SortTh<K extends string>({
  label,
  k,
  sort,
  onSort,
  className = "px-3 py-2",
}: {
  label: string;
  k: K;
  sort: SortState<K>;
  onSort: (k: K) => void;
  className?: string;
}) {
  const active = sort.key === k;
  return (
    <th className={`whitespace-nowrap ${className}`} aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}>
      <button
        type="button"
        className={`inline-flex items-center gap-1 uppercase hover:text-gray-900 ${active ? "text-gray-900" : ""}`}
        onClick={() => onSort(k)}
        title={`Sort by ${label.toLowerCase()}`}
      >
        <span className="text-left">{label}</span>
        <Arrows active={active} dir={sort.dir} />
      </button>
    </th>
  );
}

/** The same thing as a row of small buttons, for lists that are not tables. */
export function SortBar<K extends string>({
  options,
  sort,
  onSort,
}: {
  options: { k: K; label: string }[];
  sort: SortState<K>;
  onSort: (k: K) => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-gray-500">
      <span className="font-medium uppercase">Sort by</span>
      {options.map((o) => {
        const active = sort.key === o.k;
        return (
          <button
            key={o.k}
            type="button"
            onClick={() => onSort(o.k)}
            className={`inline-flex items-center gap-1 uppercase hover:text-gray-900 ${active ? "text-gray-900" : ""}`}
            title={`Sort by ${o.label.toLowerCase()}`}
          >
            <span>{o.label}</span>
            <Arrows active={active} dir={sort.dir} />
          </button>
        );
      })}
    </div>
  );
}
