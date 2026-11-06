"use client";

import { useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { loadSaved, saveSaved } from "../../../lib/savedState";
import { Notice, btnCls, btn2Cls } from "../../../components/admin/ui";
import StoreInput from "../../../components/admin/StoreInput";

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
  ready_ended: number;
  by_store: Record<string, number>;
  sample: Sample[];
  skipped_sample: { item: string; store: string | null; title: string | null }[];
  reports: { store: string; n: number }[];
};
type ReportResult = {
  store: string;
  listing_status?: string | null;
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

type EbayState = { reports: { store: string; n: number }[]; api_todo: number; api_ready: number; api_skipped: number };
type ApiRunResult = RunResult & {
  asked_ebay: number;
  skipped: number;
  errors: number;
  error_sample: string[];
  rate_limited: string | null;
  remaining_to_ask: number;
  remaining_to_create: number;
};

const n = (v: number) => v.toLocaleString();

/* ------------------------------------------------------------------- page */

export default function EbayListingImportPage() {
  const [store, setStore] = useState("");

  // Remember the store name when you leave this page and come back (this browser tab only).
  const [storeRestored, setStoreRestored] = useState(false);
  useEffect(() => {
    const saved = loadSaved<{ store: string }>("ebay-import");
    if (saved) setStore(saved.store ?? "");
    setStoreRestored(true);
  }, []);
  useEffect(() => {
    if (storeRestored) saveSaved("ebay-import", { store });
  }, [storeRestored, store]);
  const [fileName, setFileName] = useState("");
  const [uploading, setUploading] = useState(false);
  const [reports, setReports] = useState<(ReportResult & { file: string })[]>([]);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [loadingPreview, setLoadingPreview] = useState(false);
  const [running, setRunning] = useState(false);
  const [totals, setTotals] = useState({ created: 0, photos: 0, compat: 0, labels: 0, problems: [] as string[], compatError: "", asked: 0, skipped: 0, notes: [] as string[] });
  const [ebayState, setEbayState] = useState<EbayState | null>(null);
  const [error, setError] = useState("");
  const stopRef = useRef(false);
  const [fillLeft, setFillLeft] = useState<number | null>(null);
  const [filling, setFilling] = useState(false);
  const [fillTotals, setFillTotals] = useState({ filled: 0, skipped: 0, notes: [] as string[] });
  const fileRef = useRef<HTMLInputElement>(null);

  async function loadState() {
    try {
      setEbayState(await api<EbayState>("/ebay-import/state"));
    } catch (e) {
      setError(errMsg(e));
    }
  }

  useEffect(() => {
    void loadState();
  }, []);

  // The report buttons (and Check now) belong to a loaded report; the straight-from-eBay buttons to no report.
  const hasReport = (ebayState?.reports.length ?? 0) > 0;

  // With a report on file, show what would be imported right away instead of waiting for "Check now".
  const autoChecked = useRef(false);
  useEffect(() => {
    if (hasReport && !autoChecked.current) {
      autoChecked.current = true;
      void loadPreview();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasReport]);

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
    const files = Array.from(fileRef.current?.files ?? []);
    if (files.length === 0) {
      setError("Choose the Seller Hub report file(s) first.");
      return;
    }
    setUploading(true);
    setError("");
    setReports([]);
    let failed = false;
    try {
      // One file at a time, in the order chosen. A listing in more than one file keeps the data from the last file.
      for (const file of files) {
        const csv = await file.text();
        // Which kind of report this is comes from the file name Seller Hub gave it ("inactive" must be tested before "active").
        const lower = file.name.toLowerCase();
        const status = lower.includes("inactive") ? "ended" : lower.includes("scheduled") ? "scheduled" : lower.includes("active") ? "active" : "";
        try {
          const r = await api<ReportResult>("/ebay-import/report", { method: "POST", body: { store: store.trim(), csv, status } });
          setReports((list) => [...list, { ...r, file: file.name }]);
        } catch (e) {
          setError(`${file.name}: ${errMsg(e)}${files.length > 1 ? " The files before it were saved; the ones after it were not uploaded." : ""}`);
          failed = true;
          break;
        }
      }
      if (!failed) {
        setFileName("");
        if (fileRef.current) fileRef.current.value = "";
      }
      await loadState();
      await loadPreview();
    } finally {
      setUploading(false);
    }
  }

  async function runImport(limit: number, all: boolean) {
    setRunning(true);
    stopRef.current = false;
    setError("");
    setTotals({ created: 0, photos: 0, compat: 0, labels: 0, problems: [], compatError: "", asked: 0, skipped: 0, notes: [] });
    try {
      for (;;) {
        const r = await api<RunResult>("/ebay-import/run", { method: "POST", body: { limit } });
        setTotals((t) => ({
          ...t,
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

  async function loadFillLeft() {
    try {
      const r = await api<{ to_ask: number }>("/ebay-import/blank-details");
      setFillLeft(r.to_ask);
    } catch {
      /* a convenience: the rest of the page works without it */
    }
  }

  useEffect(() => {
    void loadFillLeft();
  }, []);

  async function fillBlank(limit: number, all: boolean) {
    setFilling(true);
    stopRef.current = false;
    setError("");
    setFillTotals({ filled: 0, skipped: 0, notes: [] });
    try {
      for (;;) {
        const r = await api<{ filled: number; skipped: number; errors: number; error_sample: string[]; rate_limited: string | null; remaining: number }>(
          "/ebay-import/fill-blank",
          { method: "POST", body: { limit } },
        );
        setFillTotals((t) => ({
          filled: t.filled + r.filled,
          skipped: t.skipped + r.skipped,
          notes: [...t.notes, ...(r.rate_limited ? [r.rate_limited + " Start it again later; what was done is saved."] : []), ...r.error_sample].slice(0, 20),
        }));
        setFillLeft(r.remaining);
        if (!all || stopRef.current || r.rate_limited || r.remaining === 0 || (r.filled === 0 && r.skipped === 0)) break;
      }
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setFilling(false);
      await loadFillLeft();
    }
  }

  async function runFromEbay(limit: number, all: boolean) {
    setRunning(true);
    stopRef.current = false;
    setError("");
    setTotals({ created: 0, photos: 0, compat: 0, labels: 0, problems: [], compatError: "", asked: 0, skipped: 0, notes: [] });
    try {
      for (;;) {
        const r = await api<ApiRunResult>("/ebay-import/run-api", { method: "POST", body: { limit } });
        setTotals((t) => ({
          created: t.created + r.created,
          photos: t.photos + r.photos_attached,
          compat: t.compat + r.compat_copied,
          labels: t.labels + r.label_dropped,
          problems: [...t.problems, ...r.problems].slice(0, 60),
          compatError: t.compatError || r.compat_copy_error || "",
          asked: t.asked + r.asked_ebay,
          skipped: t.skipped + r.skipped,
          notes: [...t.notes, ...(r.rate_limited ? [r.rate_limited + " Start it again later; what was done is saved."] : []), ...r.error_sample].slice(0, 20),
        }));
        const done = r.remaining_to_ask === 0 && r.remaining_to_create === 0;
        // Stop when asked, at eBay's limit, when nothing moved (everything left is failing), or when it is all done.
        if (!all || stopRef.current || r.rate_limited || done || (r.asked_ebay === 0 && r.created === 0)) break;
      }
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setRunning(false);
      await loadState();
    }
  }

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-xl font-semibold">eBay listing import</h2>
        <p className="mt-1 max-w-3xl text-sm text-gray-600">
          Imports every eBay listing that is not on the website yet, the same way the Link tab does when you create one, and also attaches the photos the photo import saved and copies the compatibility data from the compatibility import.
          Title, price and store come from a Seller Hub &ldquo;all active listings&rdquo; report (upload one per store), or, if you
          have no report, straight from eBay. New listings are unpublished, with stock 0.
        </p>
      </div>

      {error && <Notice kind="error">{error}</Notice>}

      <div className="space-y-3 rounded-lg border border-gray-200 bg-white p-4">
        <h3 className="text-base font-semibold">1. Upload a Seller Hub report</h3>
        <div className="flex flex-wrap items-end gap-3">
          <label className="block text-sm">
            <span className="mb-1 block font-medium">eBay store name</span>
            <StoreInput value={store} onChange={setStore} placeholder="eandimotorsports" />
          </label>
          <input
            ref={fileRef}
            type="file"
            accept=".csv,text/csv"
            multiple
            className="hidden"
            onChange={(e) => {
              setFileName(Array.from(e.target.files ?? []).map((f) => f.name).join(", "));
              setReports([]);
              setError("");
            }}
          />
          <button type="button" className={btn2Cls} disabled={uploading || running} onClick={() => fileRef.current?.click()}>
            Choose report(s) (CSV)
          </button>
          <button type="button" className={btnCls} disabled={!fileName || uploading || running} onClick={() => void upload()}>
            {uploading ? "Reading…" : "Upload report(s)"}
          </button>
        </div>
        <p className="text-xs text-gray-500">
          {uploading ? "Reading the report(s)…" : fileName ? `Selected: ${fileName}` : "No file chosen yet."}
        </p>
        <p className="text-xs text-gray-500">Leave the store blank for eandimotorsports; type genuine-oem-parts for the other store. Choose several files at once if they are all for the same store; do the other store in a second upload. Uploading again replaces the earlier rows for the same listings. Every listing in a file that has no product yet will be imported, including ended ones, so only upload ended-listing reports if you want those imported too.</p>
        {reports.map((report) => (
          <div key={report.file} className="text-sm text-gray-700">
            <span className="font-medium">{report.file}:</span> {n(report.listings_in_report)} {report.listing_status === "ended" ? "ended " : report.listing_status === "scheduled" ? "scheduled " : ""}listings read for {report.store}{report.listing_status ? "" : " (could not tell if active or ended from the file name, so they will be imported as drafts)"}; {n(report.with_label)} have a custom label, {n(report.without_price)} have no price
            {report.unreadable_rows > 0 && `, ${n(report.unreadable_rows)} unreadable rows`}.
            {report.store_mismatch > 0 && (
              <Notice kind="error">
                {n(report.store_mismatch)} of the listings in {report.file} were saved by the compatibility import under a different store. Check you chose the right store for this file.
              </Notice>
            )}
          </div>
        ))}
      </div>

      {hasReport && (
        <>
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
              <div><dt className="text-xs text-gray-500">Not imported yet</dt><dd>{n(preview.total)}</dd></div>
              <div><dt className="text-xs text-gray-500">Ready to import</dt><dd className="font-medium">{n(preview.ready)}</dd></div>
              <div><dt className="text-xs text-gray-500">Missing store / missing title</dt><dd>{n(preview.no_store)} / {n(preview.no_title)}</dd></div>
              <div><dt className="text-xs text-gray-500">Ready, ended on eBay</dt><dd>{n(preview.ready_ended)}</dd></div>
              <div><dt className="text-xs text-gray-500">With a custom label</dt><dd>{n(preview.with_label)}</dd></div>
              <div><dt className="text-xs text-gray-500">No photos saved</dt><dd>{n(preview.no_photos)}</dd></div>
              <div><dt className="text-xs text-gray-500">No compatibility list</dt><dd>{n(preview.no_compat)}</dd></div>
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
                <p className="mt-2 text-xs text-gray-500">Upload that store&rsquo;s report, or finish its compatibility import, and check again.</p>
              </details>
            )}
            {preview.sample.length > 0 && (
              <details className="text-sm">
                <summary className="cursor-pointer text-gray-700">Sample of what will be created</summary>
                <div className="mt-2 overflow-x-auto">
                  <table className="w-full text-left text-xs">
                    <thead className="text-gray-500">
                      <tr><th className="pr-3">Item</th><th className="pr-3">Store</th><th className="pr-3">Title</th><th className="pr-3">Label</th><th className="pr-3">Price</th><th className="pr-3">Photos</th><th>Compatibility</th></tr>
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
          <button type="button" className={btn2Cls} disabled={running} onClick={() => void runImport(5, false)}>
            Import 5 as a test
          </button>
          <button type="button" className={btnCls} disabled={running} onClick={() => void runImport(200, true)}>
            {running ? "Importing…" : preview ? `Import all ${n(preview.ready)} ready listings` : "Import all ready listings"}
          </button>
          {running && (
            <button type="button" className={btn2Cls} onClick={() => { stopRef.current = true; }}>
              Stop after this batch
            </button>
          )}
        </div>
        <p className="text-xs text-gray-500">
          Safe to repeat: listings that are already on the website are skipped. Run the 5-listing test first and open one of the new listings to check it.
        </p>
      </div>
        </>
      )}

      {ebayState && !hasReport && (
        <div className="space-y-3 rounded-lg border border-gray-200 bg-white p-4">
          <h3 className="text-base font-semibold">2. No report loaded: import straight from eBay</h3>
          <p className="max-w-3xl text-sm text-gray-600">
            Asks eBay about each listing that has no website listing yet (title, price, custom label and store), then imports it. That is
            one eBay lookup per listing, so it uses the same daily allowance as the photo and compatibility imports. If eBay&rsquo;s daily limit
            is reached it stops and keeps what it has; start it again later. Ended or deleted listings are skipped.
          </p>
          <dl className="grid max-w-3xl grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-3">
            <div><dt className="text-xs text-gray-500">Still to ask eBay about</dt><dd>{n(ebayState.api_todo)}</dd></div>
            <div><dt className="text-xs text-gray-500">Answered, ready to import</dt><dd>{n(ebayState.api_ready)}</dd></div>
            <div><dt className="text-xs text-gray-500">Cannot be imported</dt><dd>{n(ebayState.api_skipped)}</dd></div>
          </dl>
          <div className="flex flex-wrap gap-3">
            <button type="button" className={btn2Cls} disabled={running || (ebayState.api_todo === 0 && ebayState.api_ready === 0)} onClick={() => void runFromEbay(5, false)}>
              Import 5 from eBay as a test
            </button>
            <button type="button" className={btnCls} disabled={running || (ebayState.api_todo === 0 && ebayState.api_ready === 0)} onClick={() => void runFromEbay(40, true)}>
              {running ? "Importing…" : `Import all ${n(ebayState.api_todo + ebayState.api_ready)} from eBay`}
            </button>
            {running && (
              <button type="button" className={btn2Cls} onClick={() => { stopRef.current = true; }}>
                Stop after this batch
              </button>
            )}
          </div>
          <p className="text-xs text-gray-500">Safe to repeat. Run the 5-listing test first and open one of the new listings to check it. Avoid running the compatibility or photo import at the same time.</p>
        </div>
      )}

      {fillLeft !== null && fillLeft > 0 && (
        <div className="space-y-3 rounded-lg border border-gray-200 bg-white p-4">
          <h3 className="text-base font-semibold">Fill missing prices, labels and quantities</h3>
          <p className="max-w-3xl text-sm text-gray-600">
            {n(fillLeft)} active listings are linked to a product that has no price or has stock 0, and no report or eBay answer is saved for them. This asks eBay about
            each one (one lookup per listing, the same daily allowance as the other imports) and saves the price, custom label and available quantity it returns. The Listings tab then shows
            them; a quantity from eBay is marked (eBay). Nothing is changed on eBay, and no product or stock count is edited. If eBay&rsquo;s daily limit is reached it stops and keeps what it has; start it again later.
          </p>
          <div className="flex flex-wrap gap-3">
            <button type="button" className={btn2Cls} disabled={filling || running} onClick={() => void fillBlank(5, false)}>
              Try 5 as a test
            </button>
            <button type="button" className={btnCls} disabled={filling || running} onClick={() => void fillBlank(40, true)}>
              {filling ? "Asking eBay…" : `Fill all ${n(fillLeft)}`}
            </button>
            {filling && (
              <button type="button" className={btn2Cls} onClick={() => { stopRef.current = true; }}>
                Stop after this batch
              </button>
            )}
          </div>
          {(fillTotals.filled > 0 || fillTotals.skipped > 0) && (
            <p className="text-sm text-gray-700">
              Saved {n(fillTotals.filled)} answers from eBay{fillTotals.skipped > 0 && `; ${n(fillTotals.skipped)} could not be read (ended, not found or not on a connected store)`}.
            </p>
          )}
          {fillTotals.notes.length > 0 && (
            <Notice kind="error">
              <ul className="list-disc pl-5 text-xs">{fillTotals.notes.map((x, i) => <li key={i}>{x}</li>)}</ul>
            </Notice>
          )}
        </div>
      )}

      <div className="space-y-3">
        {(totals.created > 0 || totals.asked > 0 || totals.problems.length > 0) && (
          <div className="space-y-1 text-sm text-gray-700">
            {totals.asked > 0 && (
              <div>
                Asked eBay about {n(totals.asked)} listings{totals.skipped > 0 && `; ${n(totals.skipped)} could not be imported (ended, not found or not on a connected store)`}.
              </div>
            )}
            {totals.notes.length > 0 && (
              <Notice kind="error">
                <ul className="list-disc pl-5 text-xs">{totals.notes.map((x, i) => <li key={i}>{x}</li>)}</ul>
              </Notice>
            )}
            <div>
              Created {n(totals.created)} listings; attached {n(totals.photos)} photos; copied compatibility data for {n(totals.compat)} listings
              {totals.labels > 0 && `; ${n(totals.labels)} labels left empty because they matched the item number`}.
            </div>
            {totals.compatError && <Notice kind="error">Compatibility copy problem (the listings were still created): {totals.compatError}</Notice>}
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
