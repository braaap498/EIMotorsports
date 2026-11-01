"use client";

import { useRef, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Notice, btnCls, btn2Cls, inputCls } from "../../../components/admin/ui";

/* ------------------------------------------------------------------ types */

type Sample = { item: string; store: string | null; title: string | null; label: string | null; price: number | null; photos: boolean; compat: boolean };
type Preview = {
  total: number;
  ready: number;
  no_store: number;
  no_title: number;
  no_photos: number;
  no_compat: number;
  no_price: number;
  with_label: number;
  by_store: Record<string, number>;
  sample: Sample[];
  skipped_sample: { item: string; store: string | null; title: string | null }[];
  reports: { store: string; n: number }[];
};
type ReportResult = {
  store: string;
  listings_in_report: number;
  unreadable_rows: number;
  with_label: number;
  without_price: number;
  store_mismatch: number;
};
type RunResult = {
  created: number;
  photos_attached: number;
  compat_copied: number;
  label_dropped: number;
  problems: string[];
  problem_count: number;
  compat_copy_error: string | null;
  remaining: number;
  not_ready: number;
};

const n = (v: number) => v.toLocaleString();

/* ------------------------------------------------------------------- page */

export default function EbayProductImportPage() {
  const [store, setStore] = useState("");
  const [fileName, setFileName] = useState("");
  const [uploading, setUploading] = useState(false);
  const [report, setReport] = useState<ReportResult | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [loadingPreview, setLoadingPreview] = useState(false);
  const [running, setRunning] = useState(false);
  const [totals, setTotals] = useState({ created: 0, photos: 0, compat: 0, labels: 0, problems: [] as string[], compatError: "" });
  const [error, setError] = useState("");
  const stopRef = useRef(false);
  const fileRef = useRef<HTMLInputElement>(null);

  async function loadPreview() {
    setLoadingPreview(true);
    setError("");
    try {
      setPreview(await api<Preview>("/ebay-import/preview"));
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setLoadingPreview(false);
    }
  }

  async function upload() {
    const file = fileRef.current?.files?.[0];
    if (!file) {
      setError("Choose the Seller Hub report file first.");
      return;
    }
    setUploading(true);
    setError("");
    setReport(null);
    try {
      const csv = await file.text();
      const r = await api<ReportResult>("/ebay-import/report", { method: "POST", body: { store: store.trim(), csv } });
      setReport(r);
      await loadPreview();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setUploading(false);
    }
  }

  async function runImport(limit: number, all: boolean) {
    setRunning(true);
    stopRef.current = false;
    setError("");
    setTotals({ created: 0, photos: 0, compat: 0, labels: 0, problems: [], compatError: "" });
    try {
      for (;;) {
        const r = await api<RunResult>("/ebay-import/run", { method: "POST", body: { limit } });
        setTotals((t) => ({
          created: t.created + r.created,
          photos: t.photos + r.photos_attached,
          compat: t.compat + r.compat_copied,
          labels: t.labels + r.label_dropped,
          problems: [...t.problems, ...r.problems].slice(0, 60),
          compatError: t.compatError || r.compat_copy_error || "",
        }));
        // Stop when asked, when nothing was created (everything left is failing), or when it is all done.
        if (!all || stopRef.current || r.created === 0 || r.remaining === 0) break;
      }
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setRunning(false);
      await loadPreview();
    }
  }

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-xl font-semibold">eBay product import</h2>
        <p className="mt-1 max-w-3xl text-sm text-gray-600">
          Makes a product for every eBay listing that is not linked to one yet, the same way &ldquo;Create product&rdquo; does on
          the Link tab, and also attaches the photos the photo import saved and copies the compat data from the compat import.
          Title, price and store come from a Seller Hub &ldquo;all active listings&rdquo; report (upload one per store) or, when a
          listing is not in a report, from the compat import. New products are unpublished, with stock 0.
        </p>
      </div>

      {error && <Notice kind="error">{error}</Notice>}

      <div className="space-y-3 rounded-lg border border-gray-200 bg-white p-4">
        <h3 className="text-base font-semibold">1. Upload a Seller Hub report</h3>
        <div className="flex flex-wrap items-end gap-3">
          <label className="block text-sm">
            <span className="mb-1 block font-medium">eBay store name</span>
            <input className={inputCls} value={store} onChange={(e) => setStore(e.target.value)} placeholder="eandimotorsports" />
          </label>
          <label className="block text-sm">
            <span className="mb-1 block font-medium">Report (.csv)</span>
            <input
              ref={fileRef}
              type="file"
              accept=".csv,text/csv"
              onChange={(e) => setFileName(e.target.files?.[0]?.name ?? "")}
              className="block text-sm"
            />
          </label>
          <button type="button" className={btnCls} disabled={uploading || running || !fileName} onClick={() => void upload()}>
            {uploading ? "Uploading…" : "Upload report"}
          </button>
        </div>
        <p className="text-xs text-gray-500">Leave the store blank for eandimotorsports; type genuine-oem-parts for the other store. Uploading again replaces the earlier rows for the same listings.</p>
        {report && (
          <div className="text-sm text-gray-700">
            {n(report.listings_in_report)} listings read for {report.store}; {n(report.with_label)} have a custom label, {n(report.without_price)} have no price
            {report.unreadable_rows > 0 && `, ${n(report.unreadable_rows)} unreadable rows`}.
            {report.store_mismatch > 0 && (
              <Notice kind="error">
                {n(report.store_mismatch)} of these listings were saved by the compat import under a different store. Check you chose the right store for this file.
              </Notice>
            )}
          </div>
        )}
      </div>

      <div className="space-y-3 rounded-lg border border-gray-200 bg-white p-4">
        <div className="flex flex-wrap items-center gap-3">
          <h3 className="text-base font-semibold">2. Check what will be imported</h3>
          <button type="button" className={btn2Cls} disabled={loadingPreview || running} onClick={() => void loadPreview()}>
            {loadingPreview ? "Checking…" : "Check now"}
          </button>
        </div>
        {preview && (
          <div className="space-y-3">
            <dl className="grid max-w-3xl grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-3">
              <div><dt className="text-xs text-gray-500">Listings with no product</dt><dd>{n(preview.total)}</dd></div>
              <div><dt className="text-xs text-gray-500">Ready to import</dt><dd className="font-medium">{n(preview.ready)}</dd></div>
              <div><dt className="text-xs text-gray-500">Missing store / missing title</dt><dd>{n(preview.no_store)} / {n(preview.no_title)}</dd></div>
              <div><dt className="text-xs text-gray-500">With a custom label</dt><dd>{n(preview.with_label)}</dd></div>
              <div><dt className="text-xs text-gray-500">No photos saved</dt><dd>{n(preview.no_photos)}</dd></div>
              <div><dt className="text-xs text-gray-500">No compat list</dt><dd>{n(preview.no_compat)}</dd></div>
              <div><dt className="text-xs text-gray-500">No price</dt><dd>{n(preview.no_price)}</dd></div>
              {Object.entries(preview.by_store).map(([s, c]) => (
                <div key={s}><dt className="text-xs text-gray-500">Ready in {s}</dt><dd>{n(c)}</dd></div>
              ))}
            </dl>
            {preview.reports.length > 0 && (
              <p className="text-xs text-gray-500">Reports on file: {preview.reports.map((r) => `${r.store} (${n(r.n)})`).join(", ")}</p>
            )}
            {preview.skipped_sample.length > 0 && (
              <details className="text-sm">
                <summary className="cursor-pointer text-gray-700">Listings that cannot be imported yet (first {preview.skipped_sample.length})</summary>
                <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-gray-600">
                  {preview.skipped_sample.map((s) => (
                    <li key={s.item}>{s.item}: {s.store ? "" : "no store"}{!s.store && !s.title ? ", " : ""}{s.title ? "" : "no title"}</li>
                  ))}
                </ul>
                <p className="mt-2 text-xs text-gray-500">Upload that store&rsquo;s report, or finish its compat import, and check again.</p>
              </details>
            )}
            {preview.sample.length > 0 && (
              <details className="text-sm">
                <summary className="cursor-pointer text-gray-700">Sample of what will be created</summary>
                <div className="mt-2 overflow-x-auto">
                  <table className="w-full text-left text-xs">
                    <thead className="text-gray-500">
                      <tr><th className="pr-3">Item</th><th className="pr-3">Store</th><th className="pr-3">Title</th><th className="pr-3">Label</th><th className="pr-3">Price</th><th className="pr-3">Photos</th><th>Compat</th></tr>
                    </thead>
                    <tbody className="divide-y divide-gray-100">
                      {preview.sample.map((s) => (
                        <tr key={s.item}>
                          <td className="py-1 pr-3">{s.item}</td><td className="pr-3">{s.store}</td><td className="pr-3">{s.title}</td>
                          <td className="pr-3">{s.label ?? ""}</td><td className="pr-3">{s.price ?? ""}</td>
                          <td className="pr-3">{s.photos ? "yes" : "no"}</td><td>{s.compat ? "yes" : "no"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>
            )}
          </div>
        )}
      </div>

      <div className="space-y-3 rounded-lg border border-gray-200 bg-white p-4">
        <h3 className="text-base font-semibold">3. Import</h3>
        <div className="flex flex-wrap gap-3">
          <button type="button" className={btn2Cls} disabled={running || !preview || preview.ready === 0} onClick={() => void runImport(5, false)}>
            Import 5 as a test
          </button>
          <button type="button" className={btnCls} disabled={running || !preview || preview.ready === 0} onClick={() => void runImport(200, true)}>
            {running ? "Importing…" : `Import all ${preview ? n(preview.ready) : ""} ready listings`}
          </button>
          {running && (
            <button type="button" className={btn2Cls} onClick={() => { stopRef.current = true; }}>
              Stop after this batch
            </button>
          )}
        </div>
        <p className="text-xs text-gray-500">
          Safe to repeat: listings that already have a product are skipped. Run the 5-listing test first and open one of the new products to check it.
        </p>
        {(totals.created > 0 || totals.problems.length > 0) && (
          <div className="space-y-1 text-sm text-gray-700">
            <div>
              Created {n(totals.created)} products; attached {n(totals.photos)} photos; copied compat for {n(totals.compat)} listings
              {totals.labels > 0 && `; ${n(totals.labels)} labels left empty because they matched the item number`}.
            </div>
            {totals.compatError && <Notice kind="error">Compat copy problem (products were still created): {totals.compatError}</Notice>}
            {totals.problems.length > 0 && (
              <details>
                <summary className="cursor-pointer">Problems ({totals.problems.length})</summary>
                <ul className="mt-2 max-h-56 list-disc space-y-1 overflow-y-auto pl-5 text-xs text-gray-600">
                  {totals.problems.map((p, i) => <li key={i}>{p}</li>)}
                </ul>
              </details>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
