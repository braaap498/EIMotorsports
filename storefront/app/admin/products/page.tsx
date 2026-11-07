"use client";

import ClearableInput from "../../../components/admin/ClearableInput";
import ClickRow from "../../../components/admin/ClickRow";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Badge, Field, Notice, Pager, inputCls } from "../../../components/admin/ui";
import { useAccess } from "../../../components/admin/AdminAccess";
import { saveNav } from "../../../lib/listNav";
import { loadSaved, saveSaved } from "../../../lib/savedState";

type Row = {
  id: number;
  sku: string;
  custom_label_sku: string | null;
  title: string;
  slug: string;
  list_price: number | string | null;
  quantity_on_hand: number;
  is_published: boolean;
  is_active: boolean;
  is_universal: boolean;
  fitment_count: number;
};
type Resp = { total: number; items: Row[] };

const LIMIT = 50;

// Column heading -> the field the server sorts by.
type SortKey = "sku" | "custom_label_sku" | "title" | "list_price" | "quantity_on_hand" | "fitment_count" | "is_published";
const COLUMNS: { key: SortKey; label: string; width: string }[] = [
  { key: "sku", label: "SKU", width: "w-40" },
  { key: "custom_label_sku", label: "Custom label", width: "w-[14rem]" },
  { key: "title", label: "Title", width: "" }, // takes the rest of the width
  { key: "list_price", label: "Price", width: "w-24" },
  { key: "quantity_on_hand", label: "Qty", width: "w-20" },
  { key: "fitment_count", label: "Fitment", width: "w-28" },
  { key: "is_published", label: "Status", width: "w-44" },
];

export default function ProductsPage() {
  const { can } = useAccess();
  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState("");
  const [published, setPublished] = useState("");
  const [universal, setUniversal] = useState("");
  const [noFitment, setNoFitment] = useState(false);
  const [sort, setSort] = useState<SortKey | "">("");
  const [dir, setDir] = useState<"asc" | "desc">("asc");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<Resp>({ total: 0, items: [] });
  const [error, setError] = useState("");
  const [ready, setReady] = useState(false);

  // Come back to the same search, filters and page after visiting a product.
  // ?no_fitment=1 (from the dashboard) pre-ticks the filter instead.
  useEffect(() => {
    const saved = loadSaved<{ q: string; published: string; universal: string; noFitment: boolean; offset: number; sort?: SortKey | ""; dir?: "asc" | "desc" }>("products");
    if (saved) {
      setQInput(saved.q);
      setQ(saved.q);
      setPublished(saved.published);
      setUniversal(saved.universal);
      setNoFitment(saved.noFitment);
      setOffset(saved.offset);
      setSort(saved.sort ?? "");
      setDir(saved.dir ?? "asc");
    }
    if (new URLSearchParams(window.location.search).get("no_fitment") === "1") {
      setNoFitment(true);
      setOffset(0);
    }
    setReady(true);
  }, []);

  useEffect(() => {
    if (ready) saveSaved("products", { q, published, universal, noFitment, offset, sort, dir });
  }, [ready, q, published, universal, noFitment, offset, sort, dir]);

  const load = useCallback(() => {
    if (!ready) return;
    const p = new URLSearchParams({ limit: String(LIMIT), offset: String(offset) });
    if (q) p.set("q", q);
    if (published) p.set("published", published);
    if (universal) p.set("universal", universal);
    if (noFitment) p.set("no_fitment", "true");
    if (sort) {
      p.set("sort", sort);
      p.set("dir", dir);
    }
    api<Resp>(`/products?${p}`)
      .then((d) => {
        setData(d);
        setError("");
        // Remember this page so the edit screen can step to the product above/below.
        const filters = new URLSearchParams();
        if (q) filters.set("q", q);
        if (published) filters.set("published", published);
        if (universal) filters.set("universal", universal);
        if (noFitment) filters.set("no_fitment", "true");
        if (sort) {
          filters.set("sort", sort);
          filters.set("dir", dir);
        }
        saveNav({ kind: "products", query: filters.toString(), limit: LIMIT, offset, total: d.total, ids: d.items.map((i) => i.id) });
      })
      .catch((e) => setError(errMsg(e)));
  }, [ready, q, published, universal, noFitment, offset, sort, dir]);
  useEffect(load, [load]);

  // Click a heading: ascending, click again: descending, a third click goes back to the default order.
  function clickSort(key: SortKey) {
    if (sort !== key) {
      setSort(key);
      setDir("asc");
    } else if (dir === "asc") {
      setDir("desc");
    } else {
      setSort("");
      setDir("asc");
    }
    setOffset(0);
  }

  // The list follows the search box: a short pause after the last keystroke reads it again from the first page.
  useEffect(() => {
    if (!ready || qInput.trim() === q) return;
    const t = setTimeout(() => {
      setQ(qInput.trim());
      setOffset(0);
    }, 350);
    return () => clearTimeout(t);
  }, [ready, qInput, q]);

  function filterChange(fn: () => void) {
    fn();
    setOffset(0);
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Products</h1>
        {can("products.create") && (
          <Link href="/admin/products/new" className="rounded bg-black px-4 py-2 text-sm font-semibold text-white">
            Add product
          </Link>
        )}
      </div>
      {error && <Notice kind="error">{error}</Notice>}

      <div className="flex flex-wrap items-end gap-3">
        <Field label="Search">
          <ClearableInput className={inputCls} value={qInput} onChange={(e) => setQInput(e.target.value)} placeholder="Title, SKU or part #" onClear={() => { setQInput(""); filterChange(() => setQ("")); }} />
        </Field>
        <Field label="Status">
          <select className={inputCls} value={published} onChange={(e) => filterChange(() => setPublished(e.target.value))}>
            <option value="">All</option>
            <option value="true">Published</option>
            <option value="false">Not published</option>
          </select>
        </Field>
        <Field label="Universal">
          <select className={inputCls} value={universal} onChange={(e) => filterChange(() => setUniversal(e.target.value))}>
            <option value="">All</option>
            <option value="true">Universal only</option>
            <option value="false">Not universal</option>
          </select>
        </Field>
        <label className="flex items-center gap-2 pb-2 text-sm">
          <input type="checkbox" checked={noFitment} onChange={(e) => filterChange(() => setNoFitment(e.target.checked))} />
          No fitment yet
        </label>
      </div>

      <Pager total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />

      <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white">
        <table className="w-full min-w-[72rem] table-fixed text-left text-sm">
          <colgroup>
            {COLUMNS.map((c) => (
              <col key={c.key} className={c.width} />
            ))}
          </colgroup>
          <thead className="bg-gray-50 text-xs uppercase text-gray-500">
            <tr>
              {COLUMNS.map((c) => (
                <th key={c.key} className="px-3 py-2" aria-sort={sort === c.key ? (dir === "asc" ? "ascending" : "descending") : "none"}>
                  <button
                    type="button"
                    className={`inline-flex max-w-full items-center gap-1 text-left uppercase hover:text-gray-900 ${sort === c.key ? "text-gray-900" : ""}`}
                    onClick={() => clickSort(c.key)}
                    title={`Sort by ${c.label.toLowerCase()}`}
                  >
                    <span className="truncate">{c.label}</span>
                    <span className={`shrink-0 text-[10px] ${sort === c.key ? "" : "text-gray-300"}`}>{sort === c.key ? (dir === "asc" ? "▲" : "▼") : "▲▼"}</span>
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {data.items.map((p) => (
              <ClickRow key={p.id} href={`/admin/products/${p.id}`}>
                <td className="truncate px-3 py-2 text-gray-600" title={p.sku}>{p.sku}</td>
                <td className="truncate px-3 py-2 text-gray-600" title={p.custom_label_sku ?? ""}>{p.custom_label_sku ?? "—"}</td>
                <td className="px-3 py-2 font-medium">
                  <Link href={`/admin/products/${p.id}`} className="hover:underline">{p.title}</Link>
                </td>
                <td className="px-3 py-2">{p.list_price != null ? Number(p.list_price).toFixed(2) : "—"}</td>
                <td className="px-3 py-2">{p.quantity_on_hand}</td>
                <td className="px-3 py-2">
                  {p.is_universal ? <Badge tone="blue">Universal</Badge> : p.fitment_count > 0 ? p.fitment_count : <Badge tone="amber">none</Badge>}
                </td>
                <td className="px-3 py-2">
                  <span className="flex gap-1">
                    <Badge tone={p.is_published ? "green" : "gray"}>{p.is_published ? "Published" : "Draft"}</Badge>
                    {!p.is_active && <Badge tone="amber">Inactive</Badge>}
                  </span>
                </td>
              </ClickRow>
            ))}
            {data.items.length === 0 && (
              <tr><td colSpan={7} className="px-3 py-6 text-center text-gray-500">No products match.</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <Pager total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
    </div>
  );
}
