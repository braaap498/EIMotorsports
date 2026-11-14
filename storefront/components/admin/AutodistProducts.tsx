"use client";

import { useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Notice, btnCls, inputCls } from "./ui";

/* The "Website products" tab: makes a published website product for every Automatic Distributors part that is in stock at one of
   their warehouses (Indiana or Nevada), unless we already have it as a single item. Start with a small batch and look at the
   results before making the rest. Requires sql/autodist_products.sql. */

type State = { running: boolean; message: string; done: number; total: number; created: number; skipped: number; failed: number; errors: string[] };
type PhotoState = { running: boolean; message: string; done: number; total: number; stored: number; failed: number; errors: string[] };
type Preview = {
  in_stock: number;
  already_made: number;
  have_single: number;
  no_price: number;
  to_create: number;
  to_create_no_photo: number;
  missing_parts: number;
  no_category: number;
  no_description: number;
  old_description_lines: number;
  wrong_labels: number;
  photos_pending: number | null;
  state: State;
  photos: PhotoState;
};
type OnePart = {
  sku: string; manufacturer: string | null; mfr_number: string | null; name: string | null; title: string;
  price: number | null; cost: number | null; qty: number; active: boolean; photos: number;
  product_id: number | null; product_title: string | null; have_single: boolean;
};
const money = (x: number | null) => (x == null ? "no price" : `$${x.toFixed(2)}`);
const n = (x: number | null | undefined) => (x ?? 0).toLocaleString("en-US");

export default function AutodistProducts() {
  const [pv, setPv] = useState<Preview | null>(null);
  const [st, setSt] = useState<State | null>(null);
  const [ph, setPh] = useState<PhotoState | null>(null);
  const [limit, setLimit] = useState("25");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [oneSku, setOneSku] = useState("");
  const [oneParts, setOneParts] = useState<OnePart[] | null>(null);
  const [oneMsg, setOneMsg] = useState<{ kind: "info" | "error"; text: string } | null>(null);
  const [oneBusy, setOneBusy] = useState(false);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  async function loadPreview() {
    setBusy(true);
    setErr(null);
    try {
      const p = await api<Preview>("/autodist/api/website-products/preview");
      setPv(p);
      setSt(p.state);
      setPh(p.photos);
      if (p.photos?.running) watch();
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  }
  useEffect(() => {
    void loadPreview();
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, []);

  function watch() {
    if (timer.current) clearInterval(timer.current);
    timer.current = setInterval(async () => {
      try {
        const s = await api<State & { photos: PhotoState }>("/autodist/api/website-products/status");
        setSt(s);
        setPh(s.photos);
        if (!s.running && !s.photos?.running) {
          if (timer.current) clearInterval(timer.current);
          void loadPreview();
        }
      } catch {
        /* keep watching */
      }
    }, 2000);
  }

  async function findOne() {
    setOneBusy(true);
    setOneMsg(null);
    setOneParts(null);
    try {
      const r = await api<{ items: OnePart[] }>(`/autodist/api/website-products/lookup?sku=${encodeURIComponent(oneSku.trim())}`);
      setOneParts(r.items);
      if (!r.items.length) setOneMsg({ kind: "error", text: "That SKU is not in the Automatic Distributors catalog. Check the feed has been imported." });
    } catch (e) {
      setOneMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setOneBusy(false);
    }
  }

  async function makeOne(part: OnePart, force: boolean) {
    setOneBusy(true);
    setOneMsg(null);
    try {
      const r = await api<{ product_id: number; title: string; custom_label: string | null; published: boolean; photos_saving: boolean }>(
        "/autodist/api/website-products/create-one", { method: "POST", body: { sku: part.sku, force } });
      setOneMsg({
        kind: "info",
        text: `Made product #${r.product_id}: ${r.title}${r.custom_label ? ` (custom label ${r.custom_label})` : ""}. ${
          r.published ? "It is published." : "It has no price in the feed, so it is NOT published; set a price first."
        }${r.photos_saving ? " Its photos are being saved to storage." : ""}`,
      });
      setOneParts(null);
      setOneSku("");
      void loadPreview();
    } catch (e) {
      setOneMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setOneBusy(false);
    }
  }

  async function create() {
    const count = Math.max(1, Math.min(100000, Math.floor(Number(limit) || 0)));
    if (!window.confirm(`Create up to ${n(count)} published website products from in-stock Automatic Distributors parts?`)) return;
    setErr(null);
    try {
      await api("/autodist/api/website-products/create", { method: "POST", body: { limit: count } });
      setSt({ running: true, message: "Starting…", done: 0, total: 0, created: 0, skipped: 0, failed: 0, errors: [] });
      watch();
    } catch (e) {
      setErr(errMsg(e));
    }
  }

  async function addParts() {
    setErr(null);
    setNote(null);
    setBusy(true);
    try {
      const r = await api<{ parts_added: number; labels_set: number }>("/autodist/api/website-products/add-parts", { method: "POST" });
      setNote(`Added ${n(r.parts_added)} distributor part entr${r.parts_added === 1 ? "y" : "ies"}; set ${n(r.labels_set)} custom label${r.labels_set === 1 ? "" : "s"}.`);
      await loadPreview();
    } catch (e) {
      setErr(errMsg(e));
      setBusy(false);
    }
  }

  async function assignCategories() {
    setErr(null);
    setNote(null);
    setBusy(true);
    try {
      const r = await api<{ checked: number; assigned: number; top: { category: string; products: number }[] }>(
        "/autodist/api/website-products/assign-categories",
        { method: "POST", body: { redo: false } },
      );
      const top = r.top.slice(0, 5).map((c) => `${c.category} (${n(c.products)})`).join(", ");
      setNote(`Set a category on ${n(r.assigned)} of ${n(r.checked)} product${r.checked === 1 ? "" : "s"}.${top ? ` Most: ${top}.` : ""}`);
      await loadPreview();
    } catch (e) {
      setErr(errMsg(e));
      setBusy(false);
    }
  }

  async function fillDescriptions() {
    setErr(null);
    setNote(null);
    setBusy(true);
    try {
      const r = await api<{ filled: number }>("/autodist/api/website-products/fill-descriptions", { method: "POST" });
      setNote(`Added a starting description to ${n(r.filled)} product${r.filled === 1 ? "" : "s"}.`);
      await loadPreview();
    } catch (e) {
      setErr(errMsg(e));
      setBusy(false);
    }
  }

  async function fixLabels() {
    setErr(null);
    setNote(null);
    setBusy(true);
    try {
      const r = await api<{ fixed: number; left: number; left_with_listings: number }>("/autodist/api/website-products/fix-labels", { method: "POST" });
      setNote(
        `Custom label changed to the Automatic SKU on ${n(r.fixed)} product${r.fixed === 1 ? "" : "s"}.` +
          (r.left ? ` ${n(r.left)} left unchanged (${n(r.left_with_listings)} have eBay listings, the rest would clash with another label).` : "")
      );
      await loadPreview();
    } catch (e) {
      setErr(errMsg(e));
      setBusy(false);
    }
  }

  async function cleanDescriptions() {
    setErr(null);
    setNote(null);
    setBusy(true);
    try {
      const r = await api<{ cleaned: number }>("/autodist/api/website-products/clean-descriptions", { method: "POST" });
      setNote(`Removed the condition, size and weight lines from ${n(r.cleaned)} description${r.cleaned === 1 ? "" : "s"}.`);
      await loadPreview();
    } catch (e) {
      setErr(errMsg(e));
      setBusy(false);
    }
  }

  async function storePhotos() {
    setErr(null);
    try {
      await api("/autodist/api/website-products/store-photos", { method: "POST", body: { limit: 500 } });
      setPh({ running: true, message: "Starting…", done: 0, total: 0, stored: 0, failed: 0, errors: [] });
      watch();
    } catch (e) {
      setErr(errMsg(e));
    }
  }

  const photosRunning = !!ph?.running;
  const running = !!st?.running || photosRunning;
  return (
    <div className="space-y-4 text-sm">
      <Notice kind="info">
        Makes one published website product for each Automatic Distributors part that is in stock at Indiana or Nevada and that we do not
        already have as a single item. A part that is only inside one of our kits still gets its own product. The shelf count stays at 0
        (distributor stock is never added to it). Each product gets the best-fitting category from your category tree (picked from the part name); each gets a short starting description (brand, condition, package size, weight) to build on; products have no vehicle fit yet.
      </Notice>
      {err && <Notice kind="error">{err}</Notice>}

      <section className="max-w-3xl space-y-2 rounded border border-gray-200 p-3">
        <h3 className="font-semibold">Make one product from an Automatic SKU</h3>
        <p className="text-xs text-gray-500">
          Works for any part in the catalog, in stock or not. It is made the same way as the batch below (title, category, photos, distributor part and custom label).
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <input className={inputCls + " w-64"} placeholder="Automatic SKU (e.g. 56-132)" value={oneSku}
            onChange={(e) => setOneSku(e.target.value)} onKeyDown={(e) => e.key === "Enter" && oneSku.trim() && void findOne()} />
          <button type="button" className={btnCls} disabled={oneBusy || !oneSku.trim()} onClick={() => void findOne()}>
            {oneBusy ? "Working…" : "Find part"}
          </button>
        </div>
        {oneMsg && <Notice kind={oneMsg.kind}>{oneMsg.text}</Notice>}
        {oneParts?.map((p) => (
          <div key={p.sku} className="space-y-1 rounded bg-gray-50 p-3">
            <div className="font-medium">{p.title || p.name}</div>
            <div className="text-xs text-gray-600">
              SKU {p.sku}{p.mfr_number ? ` · Mfr # ${p.mfr_number}` : ""} · {money(p.price)} (cost {money(p.cost)}) · {n(p.qty)} in stock · {p.photos} photo link{p.photos === 1 ? "" : "s"}
              {!p.active && " · not active in the feed"}
            </div>
            {p.product_id ? (
              <div className="text-xs text-amber-700">Already a website product: #{p.product_id} {p.product_title}</div>
            ) : (
              <div className="flex flex-wrap items-center gap-2">
                {p.have_single && <span className="text-xs text-amber-700">A single-item product with this UPC or part number already exists.</span>}
                <button type="button" className={btnCls} disabled={oneBusy || running} onClick={() => void makeOne(p, p.have_single)}>
                  {p.have_single ? "Make it anyway" : "Make website product"}
                </button>
                {p.price == null && <span className="text-xs text-gray-500">No price: it will be made unpublished.</span>}
              </div>
            )}
          </div>
        ))}
      </section>

      {pv && (
        <div className="grid max-w-xl grid-cols-[1fr_auto] gap-x-6 gap-y-1 rounded border border-gray-200 bg-white p-4">
          <span>Parts in stock at a distributor warehouse</span>
          <span className="text-right font-medium">{n(pv.in_stock)}</span>
          <span>Already made into a website product here</span>
          <span className="text-right">{n(pv.already_made)}</span>
          <span>We already have it as a single item (skipped)</span>
          <span className="text-right">{n(pv.have_single)}</span>
          <span>No price (skipped)</span>
          <span className="text-right">{n(pv.no_price)}</span>
          <span className="font-medium">Ready to make</span>
          <span className="text-right font-medium">{n(pv.to_create)}</span>
          <span className="text-gray-500">...of those with no photo link</span>
          <span className="text-right text-gray-500">{n(pv.to_create_no_photo)}</span>
        </div>
      )}

      {pv && pv.missing_parts > 0 && (
        <div className="flex max-w-xl items-center justify-between gap-3 rounded border border-amber-300 bg-amber-50 p-3">
          <span>
            {n(pv.missing_parts)} product{pv.missing_parts === 1 ? "" : "s"} made here {pv.missing_parts === 1 ? "has" : "have"} no Distributor parts entry yet.
          </span>
          <button type="button" className={btnCls} disabled={running || busy} onClick={() => void addParts()}>
            Add distributor parts
          </button>
        </div>
      )}
      {pv && pv.no_category > 0 && (
        <div className="flex max-w-xl items-center justify-between gap-3 rounded border border-amber-300 bg-amber-50 p-3">
          <span>
            {n(pv.no_category)} product{pv.no_category === 1 ? "" : "s"} made here {pv.no_category === 1 ? "has" : "have"} no category yet.
          </span>
          <button type="button" className={btnCls} disabled={running || busy} onClick={() => void assignCategories()}>
            Assign categories
          </button>
        </div>
      )}
      {pv && pv.no_description > 0 && (
        <div className="flex max-w-xl items-center justify-between gap-3 rounded border border-amber-300 bg-amber-50 p-3">
          <span>
            {n(pv.no_description)} product{pv.no_description === 1 ? "" : "s"} made here {pv.no_description === 1 ? "has" : "have"} no description yet.
          </span>
          <button type="button" className={btnCls} disabled={running || busy} onClick={() => void fillDescriptions()}>
            Add starting descriptions
          </button>
        </div>
      )}
      {pv && pv.wrong_labels > 0 && (
        <div className="flex max-w-xl items-center justify-between gap-3 rounded border border-amber-300 bg-amber-50 p-3">
          <span>
            {n(pv.wrong_labels)} product{pv.wrong_labels === 1 ? "" : "s"} made here {pv.wrong_labels === 1 ? "has" : "have"} the manufacturer number as the
            custom label. It should be the Automatic SKU. Products with eBay listings are left as they are.
          </span>
          <button type="button" className={btnCls} disabled={running || busy} onClick={() => void fixLabels()}>
            Change to Automatic SKU
          </button>
        </div>
      )}
      {pv && pv.old_description_lines > 0 && (
        <div className="flex max-w-xl items-center justify-between gap-3 rounded border border-amber-300 bg-amber-50 p-3">
          <span>
            {n(pv.old_description_lines)} product{pv.old_description_lines === 1 ? "" : "s"} made here still {pv.old_description_lines === 1 ? "has" : "have"} Condition, Package size or
            Shipping weight lines in the description. The website now shows Condition from the product, and weight and size are kept for shipping.
          </span>
          <button type="button" className={btnCls} disabled={running || busy} onClick={() => void cleanDescriptions()}>
            Remove those lines
          </button>
        </div>
      )}
      {note && <Notice kind="info">{note}</Notice>}

      {pv && ((pv.photos_pending ?? 0) > 0 || photosRunning) && (
        <div className="flex max-w-xl items-center justify-between gap-3 rounded border border-amber-300 bg-amber-50 p-3">
          <span>
            {photosRunning
              ? `Saving photos to storage: ${n(ph?.done)} of ${n(ph?.total)}`
              : `${n(pv.photos_pending)} distributor photo${pv.photos_pending === 1 ? " is" : "s are"} still only a link to their server, not saved in our storage.`}
          </span>
          <button type="button" className={btnCls} disabled={running || busy} onClick={() => void storePhotos()}>
            Save photos to storage
          </button>
        </div>
      )}
      {ph && !ph.running && ph.message && (
        <div className="max-w-xl rounded border border-gray-200 bg-white p-3">
          <div>{ph.message}</div>
          {ph.errors?.length > 0 && (
            <ul className="mt-1 list-disc pl-5 text-xs text-red-700">
              {ph.errors.map((e, i) => (
                <li key={i}>{e}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      <div className="flex items-end gap-3">
        <label className="flex flex-col gap-0.5 text-xs text-gray-600">
          How many to make now
          <input className={`${inputCls} !w-28`} value={limit} onChange={(e) => setLimit(e.target.value.replace(/\D/g, ""))} disabled={running} />
        </label>
        <button type="button" className={btnCls} disabled={running || busy || !pv || pv.to_create === 0} onClick={() => void create()}>
          {running ? "Working…" : "Create products"}
        </button>
        <button type="button" className="text-xs text-blue-700 underline" disabled={running || busy} onClick={() => void loadPreview()}>
          Recount
        </button>
      </div>

      {st && (st.running || st.message) && (
        <div className="rounded border border-gray-200 bg-white p-3">
          <div>
            {st.running ? `Working: ${n(st.done)} of ${n(st.total)}` : st.message}
            {st.running && ` · ${n(st.created)} made, ${n(st.failed)} failed`}
          </div>
          {st.errors?.length > 0 && (
            <ul className="mt-1 list-disc pl-5 text-xs text-red-700">
              {st.errors.map((e, i) => (
                <li key={i}>{e}</li>
              ))}
            </ul>
          )}
        </div>
      )}
      <p className="text-xs text-gray-500">New products have their distributor photos saved to storage at the end of each batch. In Products, “Don&apos;t use” on a photo keeps it but leaves it off the website and eBay.</p>
      <p className="text-xs text-gray-500">Start with 25, open a few in Products to check them, then raise the number.</p>
    </div>
  );
}
