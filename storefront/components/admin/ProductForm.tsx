"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import FitmentPanel from "./FitmentPanel";
import { Field, Notice, btnCls, inputCls } from "./ui";

type Cat = { id: number; parent_id: number | null; name: string; sort_order: number };
type Photo = { id: number; source_url: string | null; storage_path: string | null; is_primary: boolean };

type FieldDef = {
  key: string;
  label: string;
  kind: "text" | "number" | "int";
  step?: string;
  notNull?: boolean; // blank means "leave to the database default", never null
  wide?: boolean;
};

const BASIC: FieldDef[] = [
  { key: "title", label: "Title", kind: "text", wide: true },
  { key: "sku", label: "SKU", kind: "text" },
  { key: "manufacturer_part_number", label: "Manufacturer part #", kind: "text" },
  { key: "internal_part_number", label: "Internal part #", kind: "text" },
  { key: "part_type", label: "Part type", kind: "text" },
  { key: "condition", label: "Condition", kind: "text" },
];
const PRICING: FieldDef[] = [
  { key: "list_price", label: "List price", kind: "number", step: "0.01" },
  { key: "cost", label: "Cost", kind: "number", step: "0.01" },
  { key: "quantity_on_hand", label: "Quantity on hand", kind: "int", notNull: true },
  { key: "reorder_point", label: "Reorder point", kind: "int" },
];
const SHIPPING: FieldDef[] = [
  { key: "weight_oz", label: "Weight (oz)", kind: "number", step: "0.01" },
  { key: "length_in", label: "Length (in)", kind: "number", step: "0.01" },
  { key: "width_in", label: "Width (in)", kind: "number", step: "0.01" },
  { key: "height_in", label: "Height (in)", kind: "number", step: "0.01" },
];
const TECH: FieldDef[] = [
  { key: "bore_size_mm", label: "Bore size (mm)", kind: "number", step: "0.01" },
  { key: "shim_count", label: "Shim count", kind: "int" },
  { key: "shim_od_mm", label: "Shim OD (mm)", kind: "number", step: "0.01" },
  { key: "pressure_rating_bar", label: "Pressure rating (bar)", kind: "number", step: "0.01" },
  { key: "units_per_package", label: "Units per package", kind: "int", notNull: true },
  { key: "barcode", label: "Barcode", kind: "text" },
  { key: "barcode_type", label: "Barcode type", kind: "text" },
];
const ALL = [...BASIC, ...PRICING, ...SHIPPING, ...TECH];

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

export default function ProductForm({ productId }: { productId?: number }) {
  const router = useRouter();
  const [vals, setVals] = useState<Record<string, string>>({});
  const [flags, setFlags] = useState({ is_published: false, is_active: true, is_universal: false });
  const [cats, setCats] = useState<Cat[]>([]);
  const [photos, setPhotos] = useState<Photo[]>([]);
  const [loading, setLoading] = useState(!!productId);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);

  const orderedCats = useMemo(() => orderCategories(cats), [cats]);
  const set = (key: string, value: string) => setVals((v) => ({ ...v, [key]: value }));

  useEffect(() => {
    api<Cat[]>("/categories").then(setCats).catch(() => {});
  }, []);

  useEffect(() => {
    if (!productId) return;
    api<Record<string, unknown> & { photos: Photo[] }>(`/products/${productId}`)
      .then((p) => {
        const next: Record<string, string> = {};
        [...ALL.map((f) => f.key), "description", "slug", "category_id"].forEach((k) => {
          const v = p[k];
          next[k] = v == null ? "" : String(v);
        });
        setVals(next);
        setFlags({
          is_published: !!p.is_published,
          is_active: !!p.is_active,
          is_universal: !!p.is_universal,
        });
        setPhotos(p.photos ?? []);
      })
      .catch((e) => setMsg({ kind: "error", text: errMsg(e) }))
      .finally(() => setLoading(false));
  }, [productId]);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setMsg(null);
    if (!(vals.title ?? "").trim() || !(vals.sku ?? "").trim()) {
      return setMsg({ kind: "error", text: "Title and SKU are required" });
    }
    const payload: Record<string, unknown> = {};
    for (const f of ALL) {
      const raw = (vals[f.key] ?? "").trim();
      if (raw === "") {
        if (!f.notNull) payload[f.key] = null;
        continue;
      }
      if (f.kind === "text") payload[f.key] = raw;
      else {
        const n = Number(raw);
        if (Number.isNaN(n)) return setMsg({ kind: "error", text: `${f.label} must be a number` });
        payload[f.key] = n;
      }
    }
    payload.description = (vals.description ?? "").trim() ? vals.description : null;
    payload.category_id = vals.category_id ? Number(vals.category_id) : null;
    if ((vals.slug ?? "").trim()) payload.slug = vals.slug.trim();
    Object.assign(payload, flags);

    setBusy(true);
    try {
      if (productId) {
        await api(`/products/${productId}`, { method: "PATCH", body: payload });
        setMsg({ kind: "success", text: "Saved" });
      } else {
        const r = await api<{ id: number }>("/products", { method: "POST", body: payload });
        router.push(`/admin/products/${r.id}`);
      }
    } catch (err) {
      setMsg({ kind: "error", text: errMsg(err) });
    } finally {
      setBusy(false);
    }
  }

  if (loading) return <p className="text-sm text-gray-500">Loading…</p>;

  const renderFields = (defs: FieldDef[]) =>
    defs.map((f) => (
      <Field key={f.key} label={f.label} className={f.wide ? "sm:col-span-2 lg:col-span-3" : ""}>
        <input
          className={inputCls}
          type={f.kind === "text" ? "text" : "number"}
          step={f.kind === "number" ? f.step : f.kind === "int" ? "1" : undefined}
          value={vals[f.key] ?? ""}
          onChange={(e) => set(f.key, e.target.value)}
        />
      </Field>
    ));

  const card = "space-y-3 rounded-lg border border-gray-200 bg-white p-4";
  const grid = "grid gap-3 sm:grid-cols-2 lg:grid-cols-3";

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">{productId ? "Edit product" : "New product"}</h1>
        <Link href="/admin/products" className="text-sm underline">Back to products</Link>
      </div>

      <form onSubmit={save} className="space-y-6">
        <section className={card}>
          <h2 className="text-lg font-semibold">Basics</h2>
          <div className={grid}>
            {renderFields(BASIC)}
            <Field label="Category">
              <select className={inputCls} value={vals.category_id ?? ""} onChange={(e) => set("category_id", e.target.value)}>
                <option value="">(none)</option>
                {orderedCats.map(({ cat, depth }) => (
                  <option key={cat.id} value={cat.id}>{`${"\u00A0\u00A0".repeat(depth)}${cat.name}`}</option>
                ))}
              </select>
            </Field>
            {productId && (
              <Field label="URL slug">
                <input className={inputCls} value={vals.slug ?? ""} onChange={(e) => set("slug", e.target.value)} />
              </Field>
            )}
          </div>
          <Field label="Description">
            <textarea className={`${inputCls} min-h-40`} value={vals.description ?? ""} onChange={(e) => set("description", e.target.value)} />
          </Field>
        </section>

        <section className={card}>
          <h2 className="text-lg font-semibold">Price &amp; stock</h2>
          <div className={grid}>{renderFields(PRICING)}</div>
        </section>

        <section className={card}>
          <h2 className="text-lg font-semibold">Visibility</h2>
          <div className="flex flex-wrap gap-6 text-sm">
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={flags.is_published} onChange={(e) => setFlags((f) => ({ ...f, is_published: e.target.checked }))} />
              Published (shown in the store)
            </label>
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={flags.is_active} onChange={(e) => setFlags((f) => ({ ...f, is_active: e.target.checked }))} />
              Active
            </label>
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={flags.is_universal} onChange={(e) => setFlags((f) => ({ ...f, is_universal: e.target.checked }))} />
              Universal fit (shows for every machine)
            </label>
          </div>
        </section>

        <details className="rounded-lg border border-gray-200 bg-white p-4">
          <summary className="cursor-pointer text-lg font-semibold">Shipping size &amp; technical specs</summary>
          <div className="mt-3 space-y-3">
            <div className={grid}>{renderFields(SHIPPING)}</div>
            <div className={grid}>{renderFields(TECH)}</div>
          </div>
        </details>

        {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}
        <button className={btnCls} disabled={busy}>
          {busy ? "Saving…" : productId ? "Save changes" : "Create product"}
        </button>
        {!productId && <p className="text-xs text-gray-500">Fitment and photos can be added after the product is created.</p>}
      </form>

      {productId && <FitmentPanel productId={productId} isUniversal={flags.is_universal} />}

      {productId && (
        <section className={card}>
          <h2 className="text-lg font-semibold">Photos</h2>
          {photos.length === 0 ? (
            <p className="text-sm text-gray-500">No photos.</p>
          ) : (
            <div className="flex flex-wrap gap-3">
              {photos.map((ph) =>
                ph.source_url ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img key={ph.id} src={ph.source_url} alt="" className="h-24 w-24 rounded border border-gray-200 object-cover" />
                ) : null
              )}
            </div>
          )}
          <p className="text-xs text-gray-500">Read-only for now. Photo upload comes with the image storage step.</p>
        </section>
      )}
    </div>
  );
}
