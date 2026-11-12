"use client";

import { useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Notice, btnCls, inputCls } from "./ui";

/* The "Website products" tab: makes a published website product for every Automatic Distributors part that is in stock at one of
   their warehouses (Indiana or Nevada), unless we already have it as a single item. Start with a small batch and look at the
   results before making the rest. Requires sql/autodist_products.sql. */

type State = { running: boolean; message: string; done: number; total: number; created: number; skipped: number; failed: number; errors: string[] };
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
  state: State;
};
const n = (x: number | null | undefined) => (x ?? 0).toLocaleString("en-US");

export default function AutodistProducts() {
  const [pv, setPv] = useState<Preview | null>(null);
  const [st, setSt] = useState<State | null>(null);
  const [limit, setLimit] = useState("25");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  async function loadPreview() {
    setBusy(true);
    setErr(null);
    try {
      const p = await api<Preview>("/autodist/api/website-products/preview");
      setPv(p);
      setSt(p.state);
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
        const s = await api<State>("/autodist/api/website-products/status");
        setSt(s);
        if (!s.running) {
          if (timer.current) clearInterval(timer.current);
          void loadPreview();
        }
      } catch {
        /* keep watching */
      }
    }, 2000);
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

  const running = !!st?.running;
  return (
    <div className="space-y-4 text-sm">
      <Notice kind="info">
        Makes one published website product for each Automatic Distributors part that is in stock at Indiana or Nevada and that we do not
        already have as a single item. A part that is only inside one of our kits still gets its own product. The shelf count stays at 0
        (distributor stock is never added to it). Each product gets the best-fitting category from your category tree (picked from the part name); each gets a short starting description (brand, condition, package size, weight) to build on; products have no vehicle fit yet.
      </Notice>
      {err && <Notice kind="error">{err}</Notice>}

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
      {note && <Notice kind="info">{note}</Notice>}

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
      <p className="text-xs text-gray-500">Start with 25, open a few in Products to check them, then raise the number.</p>
    </div>
  );
}
