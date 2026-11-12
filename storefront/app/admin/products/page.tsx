"use client";

import ClearableInput from "../../../components/admin/ClearableInput";
import ClickRow from "../../../components/admin/ClickRow";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Badge, Field, Notice, Pager, btnCls, inputCls } from "../../../components/admin/ui";
import { useAccess } from "../../../components/admin/AdminAccess";
import { MODE_OPTIONS, STATUS_LABEL, type FitmentMode, type FitmentStatus } from "../../../lib/fitmentMode";
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
  fitment_status: FitmentStatus;
};
type Resp = { total: number; items: Row[] };
type Summary = { total: number } & Record<FitmentStatus, number>;
type Cat = { id: number; parent_id: number | null; name: string; sort_order: number };
type Saved = {
  q: string;
  published: string;
  fitment?: string;
  category?: string;
  source?: string;
  noFitment?: boolean; // older saved screens
  offset: number;
  sort?: SortKey | "";
  dir?: "asc" | "desc";
};

const LIMIT = 50;

// Column heading -> the field the server sorts by.
type SortKey = "sku" | "custom_label_sku" | "title" | "list_price" | "quantity_on_hand" | "fitment_count" | "is_published";
const COLUMNS: { key: SortKey; label: string; width: string }[] = [
  { key: "sku", label: "SKU", width: "w-40" },
  { key: "custom_label_sku", label: "Custom label", width: "w-[14rem]" },
  { key: "title", label: "Title", width: "" }, // takes the rest of the width
  { key: "list_price", label: "Price", width: "w-24" },
  { key: "quantity_on_hand", label: "Qty", width: "w-20" },
  { key: "fitment_count", label: "Fitment", width: "w-56" },
  { key: "is_published", label: "Status", width: "w-44" },
];

// The status buttons above the list, in the order they are worked through.
const STATUS_CHIPS: { key: FitmentStatus; tone: string }[] = [
  { key: "needs_fitment", tone: "border-amber-400 bg-amber-50 text-amber-900" },
  { key: "has_fitment", tone: "border-green-400 bg-green-50 text-green-900" },
  { key: "universal_search", tone: "border-blue-400 bg-blue-50 text-blue-900" },
  { key: "universal_category", tone: "border-blue-300 bg-blue-50 text-blue-900" },
  { key: "not_vehicle", tone: "border-gray-400 bg-gray-100 text-gray-800" },
];

// Categories in tree order, with a depth for indentation.
function orderCategories(cats: Cat[]): { cat: Cat; depth: number }[] {
  const byParent = new Map<number | null, Cat[]>();
  cats.forEach((c) => {
    const list = byParent.get(c.parent_id) ?? [];
    list.push(c);
    byParent.set(c.parent_id, list);
  });
  const out: { cat: Cat; depth: number }[] = [];
  const walk = (parent: number | null, depth: number) => {
    (byParent.get(parent) ?? []).forEach((c) => {
      out.push({ cat: c, depth });
      walk(c.id, depth + 1);
    });
  };
  walk(null, 0);
  return out;
}

function FitmentCell({ p }: { p: Row }) {
  switch (p.fitment_status) {
    case "universal_search":
      return <Badge tone="blue">Universal · vehicle search</Badge>;
    case "universal_category":
      return (
        <span className="flex flex-wrap items-center gap-1">
          <Badge tone="blue">Universal · category only</Badge>
          {p.fitment_count > 0 && <span className="text-xs text-gray-500">+{p.fitment_count} machine{p.fitment_count === 1 ? "" : "s"}</span>}
        </span>
      );
    case "not_vehicle":
      return <Badge tone="gray">Not vehicle-based</Badge>;
    case "has_fitment":
      return <Badge tone="green">{p.fitment_count} machine{p.fitment_count === 1 ? "" : "s"}</Badge>;
    default:
      return <Badge tone="amber">Needs fitment</Badge>;
  }
}

export default function ProductsPage() {
  const { can } = useAccess();
  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState("");
  const [published, setPublished] = useState("");
  const [fitment, setFitment] = useState("");
  const [category, setCategory] = useState("");
  const [source, setSource] = useState("");
  const [sort, setSort] = useState<SortKey | "">("");
  const [dir, setDir] = useState<"asc" | "desc">("asc");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<Resp>({ total: 0, items: [] });
  const [summary, setSummary] = useState<Summary | null>(null);
  const [cats, setCats] = useState<Cat[]>([]);
  const [error, setError] = useState("");
  const [ready, setReady] = useState(false);

  // bulk change
  const [sel, setSel] = useState<Set<number>>(new Set());
  const [bulkMode, setBulkMode] = useState<FitmentMode>("not_vehicle");
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkMsg, setBulkMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);

  const mayChangeFitment = can("fitment.add");
  const orderedCats = useMemo(() => orderCategories(cats), [cats]);

  // Come back to the same search, filters and page after visiting a product.
  // ?no_fitment=1 (from the dashboard) pre-selects the Needs fitment list.
  useEffect(() => {
    const saved = loadSaved<Saved>("products");
    if (saved) {
      setQInput(saved.q);
      setQ(saved.q);
      setPublished(saved.published);
      setFitment(saved.fitment ?? (saved.noFitment ? "needs_fitment" : ""));
      setCategory(saved.category ?? "");
      setSource(saved.source ?? "");
      setOffset(saved.offset);
      setSort(saved.sort ?? "");
      setDir(saved.dir ?? "asc");
    }
    const params = new URLSearchParams(window.location.search);
    if (params.get("no_fitment") === "1") {
      setFitment("needs_fitment");
      setOffset(0);
    }
    setReady(true);
  }, []);

  useEffect(() => {
    api<Cat[]>("/categories").then(setCats).catch(() => {});
  }, []);

  useEffect(() => {
    if (ready) saveSaved("products", { q, published, fitment, category, source, offset, sort, dir });
  }, [ready, q, published, fitment, category, source, offset, sort, dir]);

  // The search, category and other filters shared by the list, the counts and the bulk change.
  const filterParams = useCallback(
    (withStatus: boolean) => {
      const f = new URLSearchParams();
      if (q) f.set("q", q);
      if (published) f.set("published", published);
      if (category) f.set("category_id", category);
      if (source) f.set("source", source);
      if (withStatus && fitment) f.set("fitment", fitment);
      return f;
    },
    [q, published, category, source, fitment]
  );

  const load = useCallback(() => {
    if (!ready) return;
    const p = filterParams(true);
    p.set("limit", String(LIMIT));
    p.set("offset", String(offset));
    if (sort) {
      p.set("sort", sort);
      p.set("dir", dir);
    }
    api<Resp>(`/products?${p}`)
      .then((d) => {
        setData(d);
        setError("");
        // Remember this page so the edit screen can step to the product above/below.
        const filters = filterParams(true);
        if (sort) {
          filters.set("sort", sort);
          filters.set("dir", dir);
        }
        saveNav({ kind: "products", query: filters.toString(), limit: LIMIT, offset, total: d.total, ids: d.items.map((i) => i.id) });
      })
      .catch((e) => setError(errMsg(e)));
    api<Summary>(`/products/fitment-summary?${filterParams(false)}`)
      .then(setSummary)
      .catch(() => setSummary(null));
  }, [ready, filterParams, offset, sort, dir]);
  useEffect(load, [load]);

  // A different list means the ticks no longer match what is on screen.
  useEffect(() => {
    setSel(new Set());
  }, [q, published, fitment, category, source, offset, sort, dir]);

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

  const modeLabel = MODE_OPTIONS.find((o) => o.value === bulkMode)?.label ?? "";
  const anyFilter = Boolean(q || published || fitment || category || source);
  const pageIds = data.items.map((i) => i.id);
  const allOnPage = pageIds.length > 0 && pageIds.every((id) => sel.has(id));

  async function applyBulk(scope: "selected" | "list") {
    const count = scope === "selected" ? sel.size : data.total;
    if (count === 0) return;
    const what = scope === "selected" ? `the ${count} selected product${count === 1 ? "" : "s"}` : `all ${count} products in this list`;
    if (!window.confirm(`Set ${what} to "${modeLabel}"?\n\nThis changes how they match vehicle searches. You can change them back the same way.`)) return;
    setBulkBusy(true);
    setBulkMsg(null);
    try {
      const body: Record<string, unknown> = { mode: bulkMode };
      if (scope === "selected") body.ids = [...sel];
      else {
        if (q) body.q = q;
        if (published) body.published = published === "true";
        if (category) body.category_id = Number(category);
        if (fitment) body.fitment = fitment;
        if (source) body.source = source;
        body.expected_count = data.total;
      }
      const r = await api<{ updated: number }>("/products/fitment-mode", { method: "POST", body });
      setBulkMsg({ kind: "success", text: `${r.updated} product${r.updated === 1 ? "" : "s"} set to "${modeLabel}".` });
      setSel(new Set());
      load();
    } catch (e) {
      setBulkMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBulkBusy(false);
    }
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
          <ClearableInput className="w-[36rem] max-w-full rounded border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900" value={qInput} onChange={(e) => setQInput(e.target.value)} placeholder="Search title, item number, custom label or SKU" onClear={() => { setQInput(""); filterChange(() => setQ("")); }} />
        </Field>
        <Field label="Category">
          <select className={`${inputCls} max-w-[16rem]`} value={category} onChange={(e) => filterChange(() => setCategory(e.target.value))}>
            <option value="">All categories</option>
            {orderedCats.map(({ cat, depth }) => (
              <option key={cat.id} value={cat.id}>
                {"\u00A0\u00A0".repeat(depth)}
                {cat.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Status">
          <select className={inputCls} value={published} onChange={(e) => filterChange(() => setPublished(e.target.value))}>
            <option value="">All</option>
            <option value="true">Published</option>
            <option value="false">Not published</option>
          </select>
        </Field>
        <Field label="Source">
          <select className={inputCls} value={source} onChange={(e) => filterChange(() => setSource(e.target.value))}>
            <option value="">All products</option>
            <option value="autodist">From the Automatic Distributors catalog</option>
          </select>
        </Field>
      </div>

      {/* Fitment work list: click a status to show only those products; click again to clear. */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-medium uppercase text-gray-500">Fitment</span>
        <button
          type="button"
          className={`rounded-full border px-3 py-1 text-sm ${fitment === "" ? "border-black bg-black text-white" : "border-gray-300 bg-white hover:bg-gray-50"}`}
          onClick={() => filterChange(() => setFitment(""))}
        >
          All{summary ? ` (${summary.total.toLocaleString()})` : ""}
        </button>
        {STATUS_CHIPS.map((c) => (
          <button
            key={c.key}
            type="button"
            className={`rounded-full border px-3 py-1 text-sm ${c.tone} ${fitment === c.key ? "ring-2 ring-black" : "opacity-90 hover:opacity-100"}`}
            onClick={() => filterChange(() => setFitment(fitment === c.key ? "" : c.key))}
          >
            {STATUS_LABEL[c.key]}
            {summary ? ` (${summary[c.key].toLocaleString()})` : ""}
          </button>
        ))}
      </div>

      {mayChangeFitment && data.total > 0 && (
        <div className="space-y-2 rounded-lg border border-gray-200 bg-gray-50 p-3">
          <div className="flex flex-wrap items-end gap-3">
            <Field label="Set how these products match vehicles">
              <select className={`${inputCls} min-w-[18rem]`} value={bulkMode} onChange={(e) => setBulkMode(e.target.value as FitmentMode)} disabled={bulkBusy}>
                {MODE_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
            </Field>
            <button type="button" className={btnCls} disabled={bulkBusy || sel.size === 0} onClick={() => void applyBulk("selected")}>
              Apply to {sel.size} selected
            </button>
            <button type="button" className={btnCls} disabled={bulkBusy || !anyFilter} onClick={() => void applyBulk("list")} title={anyFilter ? "" : "Pick a category, search or status first"}>
              Apply to all {data.total.toLocaleString()} in this list
            </button>
            {sel.size > 0 && (
              <button type="button" className="text-xs text-blue-700 underline" onClick={() => setSel(new Set())}>
                Clear ticks
              </button>
            )}
          </div>
          <p className="text-xs text-gray-500">
            {MODE_OPTIONS.find((o) => o.value === bulkMode)?.help} To mark everything in a category at once, pick the category above, then use &ldquo;Apply to all&rdquo;. Nothing changes until you confirm.
          </p>
          {bulkMsg && <Notice kind={bulkMsg.kind}>{bulkMsg.text}</Notice>}
        </div>
      )}

      <Pager total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />

      <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white">
        <table className="w-full min-w-[76rem] table-fixed text-left text-sm">
          <colgroup>
            {mayChangeFitment && <col className="w-10" />}
            {COLUMNS.map((c) => (
              <col key={c.key} className={c.width} />
            ))}
          </colgroup>
          <thead className="bg-gray-50 text-xs uppercase text-gray-500">
            <tr>
              {mayChangeFitment && (
                <th className="px-3 py-2">
                  <input
                    type="checkbox"
                    aria-label="Tick every product on this page"
                    checked={allOnPage}
                    onChange={(e) => setSel(e.target.checked ? new Set(pageIds) : new Set())}
                  />
                </th>
              )}
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
                {mayChangeFitment && (
                  <td className="px-3 py-2">
                    <input
                      type="checkbox"
                      aria-label={`Tick ${p.title}`}
                      checked={sel.has(p.id)}
                      onChange={(e) =>
                        setSel((prev) => {
                          const next = new Set(prev);
                          if (e.target.checked) next.add(p.id);
                          else next.delete(p.id);
                          return next;
                        })
                      }
                    />
                  </td>
                )}
                <td className="truncate px-3 py-2 text-gray-600" title={p.sku}>{p.sku}</td>
                <td className="truncate px-3 py-2 text-gray-600" title={p.custom_label_sku ?? ""}>{p.custom_label_sku ?? "—"}</td>
                <td className="px-3 py-2 font-medium">
                  <Link href={`/admin/products/${p.id}`} className="hover:underline">{p.title}</Link>
                </td>
                <td className="px-3 py-2">{p.list_price != null ? Number(p.list_price).toFixed(2) : "—"}</td>
                <td className="px-3 py-2">{p.quantity_on_hand}</td>
                <td className="px-3 py-2">
                  <FitmentCell p={p} />
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
              <tr><td colSpan={mayChangeFitment ? 8 : 7} className="px-3 py-6 text-center text-gray-500">No products match.</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <Pager total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
    </div>
  );
}
