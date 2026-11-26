"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Notice, btnCls, btn2Cls } from "./ui";

// Import tab helpers for the catalog that replaced the hand-typed Rocky Mountain list:
//  - Product links: carry each product's distributor-part pointer (the "Ours" link) to the catalog part that is the same part.
//  - Photos: download the photo links of products made from catalog parts into our own storage.
type Plan = {
  total: number; from_saved_parts: number; from_dropped_parts: number; ready: number; brand_differs: number; left_alone: number;
  by: Record<string, number>; number_changes: number; limited: boolean;
  sample: { product_id: number; product_sku: string | null; product_title: string | null; kind: string; old_number: string | null; new_sku: string | null; new_brand: string | null; matched_by: string | null; candidates: number }[];
};
type PhotoStatus = { running: boolean; message: string; pending: number; done: number; total: number };
type Msg = { kind: "error" | "success" | "info"; text: string } | null;
const n = (x: number) => x.toLocaleString("en-US");

export default function RmatvLinks({ onChanged }: { onChanged: () => void }) {
  const [plan, setPlan] = useState<Plan | null>(null);
  const [photos, setPhotos] = useState<PhotoStatus | null>(null);
  const [withBrand, setWithBrand] = useState(false);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<Msg>(null);

  const loadPhotos = useCallback(() => {
    api<PhotoStatus>("/rmatv/photos/status").then(setPhotos).catch(() => setPhotos(null));
  }, []);
  useEffect(loadPhotos, [loadPhotos]);
  useEffect(() => {
    if (!photos?.running) return;
    const t = setInterval(loadPhotos, 3000);
    return () => clearInterval(t);
  }, [photos?.running, loadPhotos]);

  async function preview() {
    setBusy("preview");
    setMsg(null);
    try {
      setPlan(await api<Plan>("/rmatv/relink"));
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }
  async function apply() {
    const count = plan ? plan.ready + (withBrand ? plan.brand_differs : 0) : 0;
    if (!window.confirm(`Move ${n(count)} product link${count === 1 ? "" : "s"} to the catalog parts?\n\nOnly which part a product points at changes. Part numbers, custom labels and prices on the products are not touched, and nothing is deleted.`)) return;
    setBusy("apply");
    setMsg(null);
    try {
      const r = await api<{ moved: number; products: number; left_alone: number }>("/rmatv/relink", { method: "POST", body: { include_brand_differs: withBrand } });
      setMsg({ kind: "success", text: `Moved ${n(r.moved)} link${r.moved === 1 ? "" : "s"} (${n(r.products)} products). ${n(r.left_alone)} left as they were.` });
      setPlan(null);
      onChanged();
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }
  async function savePhotos() {
    setBusy("photos");
    setMsg(null);
    try {
      await api("/rmatv/photos/store", { method: "POST", body: { limit: 500 } });
      setMsg({ kind: "info", text: "Saving photos in the background (500 at a time; run it again for more)." });
      loadPhotos();
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="space-y-3 rounded border border-gray-200 bg-white p-4 text-sm">
      <h2 className="font-semibold">Product links and photos</h2>
      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}

      <div className="space-y-2">
        <p className="text-xs text-gray-500">
          After the catalog is imported: point each product&apos;s distributor part (its &quot;Ours&quot; link) at the catalog part that is the same part, matched by part number, then manufacturer number, then barcode. A part with several or no matches is left alone.
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" className={btn2Cls} disabled={busy !== ""} onClick={() => void preview()}>{busy === "preview" ? "Checking…" : "Preview the links"}</button>
          {plan && plan.ready + (withBrand ? plan.brand_differs : 0) > 0 && (
            <button type="button" className={btnCls} disabled={busy !== ""} onClick={() => void apply()}>{busy === "apply" ? "Moving…" : `Move ${n(plan.ready + (withBrand ? plan.brand_differs : 0))} links`}</button>
          )}
        </div>
        {plan && (
          <div className="space-y-2 text-xs">
            <p>
              {n(plan.total)} product links to check ({n(plan.from_saved_parts)} from the hand-typed list, {n(plan.from_dropped_parts)} from dropped catalog parts): <b>{n(plan.ready)}</b> ready to move
              {plan.number_changes ? ` (${n(plan.number_changes)} to a different part number)` : ""}, <b>{n(plan.brand_differs)}</b> match by manufacturer number but the brand differs, <b>{n(plan.left_alone)}</b> left alone.
            </p>
            {plan.brand_differs > 0 && (
              <label className="flex items-center gap-2"><input type="checkbox" checked={withBrand} onChange={(e) => setWithBrand(e.target.checked)} />Also move the ones where the brand differs (check the list first)</label>
            )}
            <div className="max-h-72 overflow-auto rounded border border-gray-100">
              <table className="w-full text-left">
                <thead className="sticky top-0 bg-gray-50 text-gray-500"><tr><th className="px-2 py-1">Product</th><th className="px-2 py-1">Old part</th><th className="px-2 py-1">Catalog part</th><th className="px-2 py-1">Matched by</th></tr></thead>
                <tbody className="divide-y divide-gray-100">
                  {plan.sample.map((r, i) => (
                    <tr key={i}>
                      <td className="px-2 py-1">{r.product_sku} {r.product_title}</td>
                      <td className="px-2 py-1">{r.old_number}</td>
                      <td className="px-2 py-1">{r.new_sku ? `${r.new_brand ?? ""} ${r.new_sku}` : "—"}</td>
                      <td className="px-2 py-1">{r.matched_by ?? (r.candidates > 1 ? `${r.candidates} parts match` : "no match")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {plan.limited && <p className="text-gray-500">Only the first 300 are listed; the counts cover all of them.</p>}
          </div>
        )}
      </div>

      <div className="space-y-2 border-t border-gray-100 pt-3">
        <p className="text-xs text-gray-500">
          A product made from a catalog part starts with Rocky Mountain&apos;s photo links and saves them to our storage right away. Use this to finish any that are still waiting.
        </p>
        <div className="flex flex-wrap items-center gap-3">
          <button type="button" className={btn2Cls} disabled={busy !== "" || !!photos?.running || !photos?.pending} onClick={() => void savePhotos()}>
            {photos?.running ? "Saving photos…" : `Save ${photos ? n(photos.pending) : "pending"} waiting photos`}
          </button>
          {photos?.message && <span className="text-xs text-gray-600">{photos.message}</span>}
        </div>
      </div>
    </div>
  );
}
