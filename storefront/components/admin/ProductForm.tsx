"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import DescriptionEditor from "./DescriptionEditor";
import FitmentPanel from "./FitmentPanel";
import PhotosPanel, { type Photo } from "./PhotosPanel";
import { Field, Notice, btnCls, inputCls } from "./ui";
import { useAccess } from "./AdminAccess";
import ArrowBar from "./ArrowBar";
import { loadNav, saveNav, type NavState } from "../../lib/listNav";

type Cat = { id: number; parent_id: number | null; name: string; sort_order: number };
type SimilarMatch = {
  id: number;
  ebay_item_id: string | null;
  title: string;
  quantity_on_hand: number;
  listing_count: number;
  prior_merges: number | string;
  reason: string;
};

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
  { key: "ebay_item_id", label: "eBay item ID (optional)", kind: "text" },
  { key: "manufacturer_part_number", label: "Manufacturer part #", kind: "text" },
  { key: "custom_label_sku", label: "eBay custom label (optional)", kind: "text" },
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

type Flags = { is_published: boolean; is_active: boolean; is_universal: boolean; show_part_number: boolean };

// Turns what is on screen into what the API expects. Used for saving, and once
// at load time so that saving can send only what the person actually changed.
function buildPayload(vals: Record<string, string>, flags: Flags): { payload: Record<string, unknown> } | { error: string } {
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
      if (Number.isNaN(n)) return { error: `${f.label} must be a number` };
      payload[f.key] = n;
    }
  }
  payload.description = (vals.description ?? "").trim() ? vals.description : null;
  payload.category_id = vals.category_id ? Number(vals.category_id) : null;
  if ((vals.slug ?? "").trim()) payload.slug = vals.slug.trim();
  Object.assign(payload, flags);
  return { payload };
}

export default function ProductForm({ productId }: { productId?: number }) {
  const { can } = useAccess();
  // What this person may change. New products: "create" covers the basic details.
  const mayDetails = productId ? can("products.edit_details") : can("products.create");
  const mayPrice = can("products.edit_price");
  const mayStock = can("inventory.adjust");
  const mayPublish = can("products.publish");
  const mayUniversal = can("fitment.add");
  const maySave = productId
    ? can("products.edit_details", "products.edit_price", "inventory.adjust", "products.publish", "fitment.add")
    : can("products.create");
  const fieldAllowed = (key: string) =>
    key === "list_price" || key === "cost" ? mayPrice : key === "quantity_on_hand" || key === "reorder_point" ? mayStock : mayDetails;
  const [baseline, setBaseline] = useState<Record<string, unknown> | null>(null);
  const router = useRouter();
  const [vals, setVals] = useState<Record<string, string>>({});
  const [flags, setFlags] = useState<Flags>({ is_published: false, is_active: true, is_universal: false, show_part_number: false });
  const [cats, setCats] = useState<Cat[]>([]);
  const [photos, setPhotos] = useState<Photo[]>([]);
  const [photosDirty, setPhotosDirty] = useState(false); // unsaved main-photo / order changes in the photos panel
  const [similar, setSimilar] = useState<SimilarMatch[]>([]);
  const [inHouseSku, setInHouseSku] = useState(""); // our EI- number; set by the database, never typed in
  const [loading, setLoading] = useState(!!productId);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);

  const orderedCats = useMemo(() => orderCategories(cats), [cats]);
  const set = (key: string, value: string) => setVals((v) => ({ ...v, [key]: value }));

  useEffect(() => {
    api<Cat[]>("/categories").then(setCats).catch(() => {});
  }, []);

  // New products only: warn (never block) if a product like this already exists.
  useEffect(() => {
    if (productId) return;
    const title = (vals.title ?? "").trim();
    const ebayId = (vals.ebay_item_id ?? "").trim();
    const pn = (vals.manufacturer_part_number ?? "").trim();
    if (title.length < 8 && !ebayId && !pn) {
      setSimilar([]);
      return;
    }
    let cancelled = false;
    const timer = setTimeout(() => {
      const p = new URLSearchParams();
      if (title) p.set("title", title);
      if (ebayId) p.set("ebay_item_id", ebayId);
      if (pn) p.set("part_number", pn);
      api<{ matches: SimilarMatch[] }>(`/cleanup/similar?${p}`)
        .then((r) => {
          if (!cancelled) setSimilar(r.matches);
        })
        .catch(() => {
          if (!cancelled) setSimilar([]);
        });
    }, 700);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [productId, vals.title, vals.ebay_item_id, vals.manufacturer_part_number]);

  useEffect(() => {
    if (!productId) return;
    api<Record<string, unknown> & { photos: Photo[] }>(`/products/${productId}`)
      .then((p) => {
        const next: Record<string, string> = {};
        [...ALL.map((f) => f.key), "description", "slug", "category_id"].forEach((k) => {
          const v = p[k];
          next[k] = v == null ? "" : String(v);
        });
        const loadedFlags = {
          is_published: !!p.is_published,
          is_active: !!p.is_active,
          is_universal: !!p.is_universal,
          show_part_number: !!p.show_part_number,
        };
        setVals(next);
        setInHouseSku(p.sku == null ? "" : String(p.sku));
        setFlags(loadedFlags);
        const base = buildPayload(next, loadedFlags);
        setBaseline("payload" in base ? base.payload : null);
        setPhotos(p.photos ?? []);
      })
      .catch((e) => setMsg({ kind: "error", text: errMsg(e) }))
      .finally(() => setLoading(false));
  }, [productId]);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setMsg(null);
    if (!(vals.title ?? "").trim()) {
      return setMsg({ kind: "error", text: "A title is required" });
    }
    const built = buildPayload(vals, flags);
    if ("error" in built) return setMsg({ kind: "error", text: built.error });
    let payload = built.payload;
    if (productId && baseline) {
      // Send only what changed, so a field the person can't see or edit (like cost) is never sent back.
      payload = Object.fromEntries(
        Object.entries(payload).filter(([k, v]) => JSON.stringify(v) !== JSON.stringify(baseline[k]))
      );
      if (Object.keys(payload).length === 0) return setMsg({ kind: "success", text: "Nothing has changed." });
    }

    setBusy(true);
    try {
      if (productId) {
        await api(`/products/${productId}`, { method: "PATCH", body: payload });
        if (baseline) setBaseline({ ...baseline, ...payload });
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

  // ---- up / down arrows: step to the product above or below in the list you came from
  const [nav, setNav] = useState<NavState | null>(null);
  const [navBusy, setNavBusy] = useState(false);
  useEffect(() => {
    if (productId) setNav(loadNav("products"));
  }, [productId]);
  const navIdx = nav && productId ? nav.ids.indexOf(productId) : -1;
  const navPos = nav && navIdx >= 0 ? nav.offset + navIdx : -1; // 0-based place in the whole list
  const canUp = navPos > 0;
  const canDown = !!nav && navPos >= 0 && navPos < nav.total - 1;

  function hasUnsavedChanges(): boolean {
    if (photosDirty) return true;
    if (!baseline) return false;
    const built = buildPayload(vals, flags);
    if (!("payload" in built)) return true;
    return Object.entries(built.payload).some(([k, v]) => JSON.stringify(v) !== JSON.stringify(baseline[k]));
  }

  async function step(dir: -1 | 1) {
    if (!nav || navIdx < 0) return;
    if (hasUnsavedChanges() && !window.confirm("You have unsaved changes on this product. Leave without saving?")) return;
    const j = navIdx + dir;
    if (j >= 0 && j < nav.ids.length) {
      router.push(`/admin/products/${nav.ids[j]}`);
      return;
    }
    // At the edge of this page of the list: load the neighbouring page.
    const offset = dir > 0 ? nav.offset + nav.limit : Math.max(0, nav.offset - nav.limit);
    setNavBusy(true);
    try {
      const r = await api<{ total: number; items: { id: number }[] }>(
        `/products?${nav.query ? `${nav.query}&` : ""}limit=${nav.limit}&offset=${offset}`
      );
      const ids = r.items.map((i) => i.id);
      if (ids.length === 0) return;
      saveNav({ ...nav, offset, total: r.total, ids });
      router.push(`/admin/products/${dir > 0 ? ids[0] : ids[ids.length - 1]}`);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setNavBusy(false);
    }
  }

  if (loading) return <p className="text-sm text-gray-500">Loading…</p>;

  const renderFields = (defs: FieldDef[]) =>
    defs
      .filter((f) => f.key !== "cost" || mayPrice) // what the shop paid is only shown to people who can change prices
      .map((f) => (
      <Field key={f.key} label={f.label} className={f.wide ? "sm:col-span-2 lg:col-span-3" : ""}>
        <input
          className={inputCls}
          disabled={!fieldAllowed(f.key)}
          type={f.kind === "text" ? "text" : "number"}
          step={f.kind === "number" ? f.step : f.kind === "int" ? "1" : undefined}
          value={vals[f.key] ?? ""}
          onChange={(e) => set(f.key, e.target.value)}
        />
        {f.key === "cost" && (
          <span className="mt-1 block text-xs font-normal text-gray-500">
            What you pay for this part. Admin only: customers never see it anywhere on the website. It is the starting cost on purchase orders.
          </span>
        )}
        {f.key === "manufacturer_part_number" && (
          <label className="mt-1 flex items-center gap-2 text-xs font-normal text-gray-600">
            <input
              type="checkbox"
              disabled={!mayDetails}
              checked={flags.show_part_number}
              onChange={(e) => setFlags((fl) => ({ ...fl, show_part_number: e.target.checked }))}
            />
            Show this part number to customers
          </label>
        )}
      </Field>
    ));

  const card = "space-y-3 rounded-lg border border-gray-200 bg-white p-4";
  const grid = "grid gap-3 sm:grid-cols-2 lg:grid-cols-3";

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">{productId ? "Edit product" : "New product"}</h1>
        <div className="flex items-center gap-4">
          {productId && navIdx >= 0 && (
            <ArrowBar
              upLabel="Previous product in the list"
              downLabel="Next product in the list"
              position={`${navPos + 1} of ${nav!.total}`}
              canUp={canUp}
              canDown={canDown}
              busy={navBusy}
              onUp={() => void step(-1)}
              onDown={() => void step(1)}
            />
          )}
          <Link href="/admin/products" className="text-sm underline">Back to products</Link>
        </div>
      </div>

      <form onSubmit={save} className="space-y-6">
        <section className={card}>
          <h2 className="text-lg font-semibold">Basics</h2>
          <div className={grid}>
            {renderFields(BASIC)}
            <Field label="SKU (assigned automatically)">
              <input
                className={`${inputCls} cursor-not-allowed bg-gray-100 text-gray-500`}
                readOnly
                value={productId ? inHouseSku : "Assigned when you save"}
              />
              <span className="mt-1 block text-xs font-normal text-gray-500">
                Our own number (EI-0000001…). EI-0000000 means no number has been given yet.
              </span>
            </Field>
            <Field label="Category">
              <select className={inputCls} disabled={!mayDetails} value={vals.category_id ?? ""} onChange={(e) => set("category_id", e.target.value)}>
                <option value="">(none)</option>
                {orderedCats.map(({ cat, depth }) => (
                  <option key={cat.id} value={cat.id}>{`${"\u00A0\u00A0".repeat(depth)}${cat.name}`}</option>
                ))}
              </select>
            </Field>
            {productId && (
              <Field label="URL slug (set automatically)">
                <input className={`${inputCls} cursor-not-allowed bg-gray-100 text-gray-500`} readOnly value={vals.slug ?? ""} />
                <span className="mt-1 block text-xs font-normal text-gray-500">
                  This is the product&apos;s web address. It is locked because changing it would break existing links.
                </span>
              </Field>
            )}
          </div>
          {!productId && similar.length > 0 && (
            <div className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
              <p className="font-semibold">A product like this may already exist</p>
              <p className="mb-2 text-xs">
                If it is the same part, open the existing one instead of creating a new one. You can also create this one
                and merge them later in Parts clean-up.
              </p>
              <ul className="space-y-1">
                {similar.map((m) => (
                  <li key={m.id} className="flex flex-wrap items-center gap-x-2">
                    <Link href={`/admin/products/${m.id}`} target="_blank" className="font-medium underline">
                      {m.title}
                    </Link>
                    <span className="text-xs text-amber-800">
                      {m.ebay_item_id ?? "no eBay item ID"} · {m.listing_count} eBay · qty {m.quantity_on_hand}
                      {Number(m.prior_merges) > 0 ? " · MASTER" : ""}
                    </span>
                    <span className="text-xs italic">({m.reason})</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <DescriptionEditor
            label="Description"
            disabled={!mayDetails}
            value={vals.description ?? ""}
            onChange={(html) => set("description", html)}
          />
        </section>

        <section className={card}>
          <h2 className="text-lg font-semibold">Price &amp; stock</h2>
          <div className={grid}>{renderFields(PRICING)}</div>
        </section>

        <section className={card}>
          <h2 className="text-lg font-semibold">Visibility</h2>
          <div className="flex flex-wrap gap-6 text-sm">
            <label className="flex items-center gap-2">
              <input type="checkbox" disabled={!mayPublish} checked={flags.is_published} onChange={(e) => setFlags((f) => ({ ...f, is_published: e.target.checked }))} />
              Published (shown in the store)
            </label>
            <label className="flex items-center gap-2">
              <input type="checkbox" disabled={!mayPublish} checked={flags.is_active} onChange={(e) => setFlags((f) => ({ ...f, is_active: e.target.checked }))} />
              Active
            </label>
            <label className="flex items-center gap-2">
              <input type="checkbox" disabled={!mayUniversal} checked={flags.is_universal} onChange={(e) => setFlags((f) => ({ ...f, is_universal: e.target.checked }))} />
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
        {maySave ? (
          <button className={btnCls} disabled={busy}>
            {busy ? "Saving…" : productId ? "Save changes" : "Create product"}
          </button>
        ) : (
          <p className="text-sm text-gray-500">You can look at this product but not change it.</p>
        )}
        {!productId && <p className="text-xs text-gray-500">Fitment and photos can be added after the product is created.</p>}
      </form>

      {productId && <FitmentPanel productId={productId} isUniversal={flags.is_universal} />}

      {productId && <PhotosPanel productId={productId} photos={photos} onChange={setPhotos} cardCls={card} onDirtyChange={setPhotosDirty} />}
    </div>
  );
}
