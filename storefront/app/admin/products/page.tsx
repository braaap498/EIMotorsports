"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Badge, Field, Notice, Pager, btn2Cls, inputCls } from "../../../components/admin/ui";

type Row = {
  id: number;
  sku: string;
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

export default function ProductsPage() {
  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState("");
  const [published, setPublished] = useState("");
  const [universal, setUniversal] = useState("");
  const [noFitment, setNoFitment] = useState(false);
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<Resp>({ total: 0, items: [] });
  const [error, setError] = useState("");
  const [ready, setReady] = useState(false);

  // ?no_fitment=1 (from the dashboard) pre-ticks the filter.
  useEffect(() => {
    if (new URLSearchParams(window.location.search).get("no_fitment") === "1") setNoFitment(true);
    setReady(true);
  }, []);

  const load = useCallback(() => {
    if (!ready) return;
    const p = new URLSearchParams({ limit: String(LIMIT), offset: String(offset) });
    if (q) p.set("q", q);
    if (published) p.set("published", published);
    if (universal) p.set("universal", universal);
    if (noFitment) p.set("no_fitment", "true");
    api<Resp>(`/products?${p}`)
      .then((d) => {
        setData(d);
        setError("");
      })
      .catch((e) => setError(errMsg(e)));
  }, [ready, q, published, universal, noFitment, offset]);
  useEffect(load, [load]);

  function filterChange(fn: () => void) {
    fn();
    setOffset(0);
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Products</h1>
        <Link href="/admin/products/new" className="rounded bg-black px-4 py-2 text-sm font-semibold text-white">
          Add product
        </Link>
      </div>
      {error && <Notice kind="error">{error}</Notice>}

      <div className="flex flex-wrap items-end gap-3">
        <form className="flex items-end gap-2" onSubmit={(e) => { e.preventDefault(); filterChange(() => setQ(qInput.trim())); }}>
          <Field label="Search">
            <input className={inputCls} value={qInput} onChange={(e) => setQInput(e.target.value)} placeholder="Title, SKU or part #" />
          </Field>
          <button className={btn2Cls}>Search</button>
        </form>
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

      <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white">
        <table className="w-full text-left text-sm">
          <thead className="bg-gray-50 text-xs uppercase text-gray-500">
            <tr>
              <th className="px-3 py-2">SKU</th>
              <th className="px-3 py-2">Title</th>
              <th className="px-3 py-2">Price</th>
              <th className="px-3 py-2">Qty</th>
              <th className="px-3 py-2">Fitment</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {data.items.map((p) => (
              <tr key={p.id}>
                <td className="whitespace-nowrap px-3 py-2 text-gray-600">{p.sku}</td>
                <td className="max-w-md px-3 py-2 font-medium">
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
                <td className="px-3 py-2 text-right">
                  <Link href={`/admin/products/${p.id}`} className={btn2Cls}>Edit</Link>
                </td>
              </tr>
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
