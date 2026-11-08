"use client";

import { useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { loadSaved, saveSaved } from "../../../lib/savedState";
import { Badge, Notice, btnCls, btn2Cls } from "../../../components/admin/ui";
import StoreInput from "../../../components/admin/StoreInput";

/* ------------------------------------------------------------------ types */

type Job = {
  id: number;
  store: string;
  mode: "new_only" | "all_missing" | "listing_originals";
  status: "running" | "stopping" | "done" | "stopped" | "paused" | "failed";
  running: boolean;
  listings_found: number;
  listings_added: number;
  items_total: number;
  items_done: number;
  photos_added: number;
  no_photos: number;
  failed: number;
  ebay_calls: number;
  message: string | null;
  failures: string[];
  started_at: string;
  finished_at: string | null;
};
type StatusResp = { job: Job | null };
type ApiLabelsResp = {
  store: string;
  filled_from_saved: number;
  asked_ebay: number;
  labels_filled: number;
  no_label_on_ebay: number;
  not_active: number;
  other_store: number;
  errors: number;
  error_sample: string[];
  rate_limited: string | null;
  remaining: number;
  blank_without_store: number;
};
type InvItem = { item: string; label: string | null; saved: string | null; ebay: string | null; checked_on_ebay: boolean; problem: string | null; differs_from_product?: boolean };
type InvJob = {
  id: number;
  mode: "saved" | "live";
  scope: string | null;
  status: "running" | "stopping" | "done" | "stopped" | "paused" | "failed";
  running: boolean;
  total: number;
  done: number;
  counts: Record<string, number>;
  ebay_calls: number;
  message: string | null;
  error_sample: string[];
  elapsed_s: number;
  started_at: string | null;
  finished_at: string | null;
};
type InvStatus = {
  job: InvJob | null;
  totals: Record<string, number>;
  unchecked: number;
  not_checked_on_ebay: number;
  blank_products: number;
  flagged_total: number;
};
type InvFlagged = {
  total: number;
  flagged: { product_id: number; sku: string | null; title: string | null; product_label: string | null; outcome: "id_mismatch" | "conflict"; items: InvItem[] }[];
};
type LabelsResp = {
  store?: string | null;
  listings_in_report: number;
  with_label: number;
  labels_filled: number;
  listings_added: number;
  already_linked_skipped: number;
  already_had_same_label: number;
  differs_count: number;
  differs: { item: string; current: string; report: string }[];
  unreadable_rows: number;
};

const MODE_NAME: Record<Job["mode"], string> = {
  new_only: "Listings that are not products yet",
  all_missing: "Any missing photo for all listings",
  listing_originals: "Each merged listing's own photos",
};
const STATUS_TEXT: Record<Job["status"], string> = {
  running: "Running",
  stopping: "Stopping…",
  done: "Finished",
  stopped: "Stopped",
  paused: "Paused",
  failed: "Failed",
};
const STATUS_TONE: Record<Job["status"], "green" | "gray" | "amber"> = {
  running: "amber",
  stopping: "amber",
  done: "green",
  stopped: "gray",
  paused: "amber",
  failed: "amber",
};

/* ------------------------------------------------------------------- page */

export default function EbayPhotoImportPage() {
  const [store, setStore] = useState("");
  const [labelsStore, setLabelsStore] = useState("");
  const [onlyProduct, setOnlyProduct] = useState("");
  const [lookupItem, setLookupItem] = useState("");
  const [lookupMsg, setLookupMsg] = useState("");
  const [looking, setLooking] = useState(false);

  // Remember the store names when you leave this page and come back (this browser tab only).
  const [storesRestored, setStoresRestored] = useState(false);
  useEffect(() => {
    const saved = loadSaved<{ store: string; labelsStore: string }>("ebay-photos");
    if (saved) {
      setStore(saved.store ?? "");
      setLabelsStore(saved.labelsStore ?? "");
    }
    setStoresRestored(true);
  }, []);
  useEffect(() => {
    if (storesRestored) saveSaved("ebay-photos", { store, labelsStore });
  }, [storesRestored, store, labelsStore]);
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const [starting, setStarting] = useState(false);

  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [labelsErr, setLabelsErr] = useState("");
  const [labels, setLabels] = useState<LabelsResp | null>(null);
  const [apiBusy, setApiBusy] = useState(false);
  const [apiLabels, setApiLabels] = useState<ApiLabelsResp | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [labelTarget, setLabelTarget] = useState<"channel" | "inventory">("channel");
  const [invBusy, setInvBusy] = useState(false);
  const [inv, setInv] = useState<InvStatus | null>(null);
  const [invFlagged, setInvFlagged] = useState<InvFlagged | null>(null);
  const [invAll, setInvAll] = useState(false);

  async function refresh() {
    try {
      const r = await api<StatusResp>("/ebay-photos/status");
      setJob(r.job);
    } catch (e) {
      setError(errMsg(e));
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  // While a job runs, ask for its progress every few seconds.
  const running = !!job?.running;
  useEffect(() => {
    if (!running) return;
    const t = setInterval(() => void refresh(), 3000);
    return () => clearInterval(t);
  }, [running]);

  async function start(mode: Job["mode"], exact = false) {
    setStarting(true);
    setError("");
    try {
      const typed = mode === "listing_originals" ? onlyProduct.trim() : "";
      const isItem = /^\d{9,15}$/.test(typed); // an eBay item number is long; a product number is short
      const pid = typed && !isItem ? parseInt(typed, 10) : NaN;
      const j = await api<Job>("/ebay-photos/import", {
        method: "POST",
        body: { store: store.trim(), mode, ...(Number.isFinite(pid) ? { product_id: pid } : {}), ...(isItem ? { item_id: typed } : {}), ...(exact ? { exact: true } : {}) },
      });
      setJob(j);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setStarting(false);
    }
  }

  async function findProduct() {
    setLookupMsg("");
    setLooking(true);
    try {
      const r = await api<{ product_id: number; title: string | null; listings: number }>(
        `/ebay-photos/product-for-item?item_id=${encodeURIComponent(lookupItem.trim())}`,
      );
      setOnlyProduct(String(r.product_id));
      setLookupMsg(
        `Product ${r.product_id}${r.title ? `: ${r.title}` : ""} (${r.listings} listing${r.listings === 1 ? "" : "s"}). Its number is now in the box above.`,
      );
    } catch (e) {
      setLookupMsg(errMsg(e));
    } finally {
      setLooking(false);
    }
  }

  async function stop() {
    setError("");
    try {
      const r = await api<StatusResp>("/ebay-photos/stop", { method: "POST", body: {} });
      setJob(r.job);
    } catch (e) {
      setError(errMsg(e));
    }
  }

  async function uploadReport() {
    if (!file) return;
    setUploading(true);
    setLabelsErr("");
    setLabels(null);
    try {
      const csv = await file.text();
      setLabels(await api<LabelsResp>("/ebay-photos/labels", { method: "POST", body: { csv, store: labelsStore.trim() } }));
      setFile(null);
      if (fileRef.current) fileRef.current.value = "";
    } catch (e) {
      setLabelsErr(errMsg(e));
    } finally {
      setUploading(false);
    }
  }

  // Ask eBay directly for the custom labels that are still blank for the store typed in the box (no report needed).
  async function fillFromEbay() {
    if (!labelsStore.trim()) {
      setLabelsErr("Type the eBay store name above first, so the right store is asked.");
      return;
    }
    setApiBusy(true);
    setLabelsErr("");
    setApiLabels(null);
    try {
      setApiLabels(await api<ApiLabelsResp>("/ebay-photos/labels-from-ebay", { method: "POST", body: { store: labelsStore.trim(), limit: 100 } }));
    } catch (e) {
      setLabelsErr(errMsg(e));
    } finally {
      setApiBusy(false);
    }
  }

  // Inventory labels run in the background on the server; this page just shows its status.
  const invPolling = useRef(false);
  async function refreshInv() {
    if (invPolling.current) return; // never stack requests: a slow answer must not pile up more
    invPolling.current = true;
    try {
      setInv(await api<InvStatus>("/ebay-photos/labels-inventory/status"));
    } catch (e) {
      setLabelsErr(errMsg(e));
    } finally {
      invPolling.current = false;
    }
  }
  async function loadFlagged() {
    try {
      setInvFlagged(await api<InvFlagged>("/ebay-photos/labels-inventory/flagged"));
    } catch (e) {
      setLabelsErr(errMsg(e));
    }
  }
  async function startInv(mode: "saved" | "live", scope = "attention") {
    setInvBusy(true);
    setLabelsErr("");
    try {
      setInv(await api<InvStatus>("/ebay-photos/labels-inventory/start", { method: "POST", body: { mode, scope } }));
    } catch (e) {
      setLabelsErr(errMsg(e));
    } finally {
      setInvBusy(false);
    }
  }
  async function stopInv() {
    try {
      setInv(await api<InvStatus>("/ebay-photos/labels-inventory/stop", { method: "POST" }));
    } catch (e) {
      setLabelsErr(errMsg(e));
    }
  }
  async function resetInv() {
    if (!window.confirm("Start over? This forgets which products were checked (labels already filled in stay) so the next run checks every product again.")) return;
    try {
      setInv(await api<InvStatus>("/ebay-photos/labels-inventory/reset", { method: "POST" }));
      setInvFlagged(null);
    } catch (e) {
      setLabelsErr(errMsg(e));
    }
  }
  const invRunning = !!inv?.job?.running;
  useEffect(() => {
    if (labelTarget !== "inventory") return;
    void refreshInv();
  }, [labelTarget]);
  useEffect(() => {
    if (labelTarget !== "inventory" || !invRunning) return;
    const t = setInterval(() => void refreshInv(), 2000);
    return () => clearInterval(t);
  }, [labelTarget, invRunning]);
  const invJobId = inv?.job?.id;
  useEffect(() => {
    if (labelTarget === "inventory" && inv && !invRunning && inv.flagged_total > 0) void loadFlagged();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [labelTarget, invRunning, invJobId, inv?.flagged_total]);
  const fmtTime = (sec: number) => {
    const m = Math.floor(sec / 60);
    const h = Math.floor(m / 60);
    return h > 0 ? `${h}h ${m % 60}m` : m > 0 ? `${m}m ${sec % 60}s` : `${sec}s`;
  };

  const pct = job && job.items_total > 0 ? Math.min(100, Math.round((job.items_done / job.items_total) * 100)) : 0;

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-semibold">eBay photo import</h1>
        <p className="mt-1 max-w-3xl text-sm text-gray-600">
          Reads a store&rsquo;s active listings from eBay, adds any the website does not have yet, and downloads their
          photos into the website&rsquo;s photo storage. Listings are matched by eBay item number, so the store name
          only says whose listings to read. Ended listings are not visible to eBay&rsquo;s public search; use the
          report upload below for those.
        </p>
      </div>

      {error && <Notice kind="error">{error}</Notice>}

      <div className="space-y-4 rounded-lg border border-gray-200 bg-white p-4">
        <label className="block max-w-sm text-sm">
          <span className="mb-1 block font-medium">eBay store name</span>
          <StoreInput value={store} onChange={setStore} placeholder="eandimotorsports" disabled={running} />
        </label>
        <div className="flex flex-wrap gap-3">
          <button type="button" className={btnCls} disabled={running || starting} onClick={() => void start("new_only")}>
            Import photos for listings that are not products yet
          </button>
          <button type="button" className={btnCls} disabled={running || starting} onClick={() => void start("all_missing")}>
            Import any missing photo for all listings
          </button>
        </div>
        <p className="max-w-3xl text-xs text-gray-500">
          The second button does everything the first does, and also checks listings that are already linked to a
          product and adds any photo eBay has that the product does not. It takes longer: every listing needs its own
          eBay lookup. If eBay&rsquo;s daily limit is reached the import pauses; start it again later and it carries on.
        </p>
      </div>

      <div className="space-y-3 rounded-lg border border-gray-200 bg-white p-4">
        <div>
          <h2 className="text-base font-semibold">Give each merged listing its own photos</h2>
          <p className="mt-1 max-w-3xl text-sm text-gray-600">
            Listings merged into one product all show that product&rsquo;s photos. This reads each of those listings
            from eBay and saves the photos it has there on the listing itself. Photos a listing already has are
            skipped, so nothing is added twice. A saved set that is only a copy of the product&rsquo;s photos is
            replaced with what eBay shows for that item. Sets saved by a merge are only ever added to. Ended listings
            and listings with variations are not shown by eBay&rsquo;s public lookup and are left as they are.
          </p>
        </div>
        <label className="block max-w-md text-sm">
          <span className="mb-1 block font-medium">Only this product number or eBay item number (optional)</span>
          <input
            className="w-full rounded border border-gray-300 px-2 py-1.5 text-sm"
            inputMode="numeric"
            placeholder="e.g. 23 (product) or 177464505625 (eBay item)"
            value={onlyProduct}
            onChange={(e) => setOnlyProduct(e.target.value.replace(/[^0-9]/g, ""))}
            disabled={running}
          />
        </label>
        <div className="flex flex-wrap items-end gap-3">
          <label className="block max-w-xs text-sm">
            <span className="mb-1 block font-medium">Don&rsquo;t know the product number? Type the eBay item number</span>
            <input
              className="w-full rounded border border-gray-300 px-2 py-1.5 text-sm"
              inputMode="numeric"
              placeholder="e.g. 177464505625"
              value={lookupItem}
              onChange={(e) => setLookupItem(e.target.value.replace(/[^0-9]/g, ""))}
              disabled={running}
            />
          </label>
          <button
            type="button"
            className={btn2Cls}
            disabled={running || looking || !/^\d{9,15}$/.test(lookupItem)}
            onClick={() => void findProduct()}
          >
            {looking ? "Looking…" : "Find product number"}
          </button>
        </div>
        {lookupMsg && <p className="max-w-3xl text-xs text-gray-600">{lookupMsg}</p>}
        <div className="flex flex-wrap gap-3">
          <button type="button" className={btnCls} disabled={running || starting} onClick={() => void start("listing_originals")}>
            Save each merged listing&rsquo;s own photos
          </button>
          <button type="button" className={btnCls} disabled={running || starting} onClick={() => void start("listing_originals", true)}>
            Make each listing&rsquo;s photos match eBay exactly
          </button>
        </div>
        <p className="max-w-3xl text-xs text-gray-500">
          Replaces a listing&rsquo;s saved photos with exactly what eBay has, in eBay&rsquo;s order, so a photo that
          belongs to another listing is taken off and the right one becomes the main photo. Works for ended listings.
          Needs the store connected under Connect eBay. Picture files are not deleted.
        </p>
        <p className="max-w-3xl text-xs text-gray-500">
          Uses the store name above. Photos are saved to the listing only, not to the product&rsquo;s gallery. If eBay&rsquo;s
          daily limit is reached it pauses; start it again later and it carries on.
        </p>
      </div>

      {job && (
        <div className="space-y-3 rounded-lg border border-gray-200 bg-white p-4">
          <div className="flex flex-wrap items-center gap-3">
            <Badge tone={STATUS_TONE[job.status]}>{STATUS_TEXT[job.status]}</Badge>
            <span className="text-sm font-medium">{job.store}</span>
            <span className="text-sm text-gray-500">{MODE_NAME[job.mode]}</span>
            <span className="text-xs text-gray-400">started {new Date(job.started_at).toLocaleString()}</span>
            {job.running && (
              <button type="button" className={btn2Cls} onClick={() => void stop()} disabled={job.status === "stopping"}>
                {job.status === "stopping" ? "Stopping…" : "Stop"}
              </button>
            )}
          </div>

          {job.items_total > 0 && (
            <div>
              <div className="h-2 w-full overflow-hidden rounded bg-gray-100">
                <div className="h-2 bg-gray-700" style={{ width: `${pct}%` }} />
              </div>
              <div className="mt-1 text-xs text-gray-500">
                {job.items_done.toLocaleString()} of {job.items_total.toLocaleString()} done ({pct}%)
              </div>
            </div>
          )}

          {job.message && <p className="text-sm text-gray-700">{job.message}</p>}

          <dl className="grid max-w-3xl grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-3">
            <div><dt className="text-xs text-gray-500">Active listings read</dt><dd>{job.listings_found.toLocaleString()}</dd></div>
            <div><dt className="text-xs text-gray-500">Added to import table</dt><dd>{job.listings_added.toLocaleString()}</dd></div>
            <div><dt className="text-xs text-gray-500">Photo rows added</dt><dd>{job.photos_added.toLocaleString()}</dd></div>
            <div><dt className="text-xs text-gray-500">No photos from eBay</dt><dd>{job.no_photos.toLocaleString()}</dd></div>
            <div><dt className="text-xs text-gray-500">Failed</dt><dd>{job.failed.toLocaleString()}</dd></div>
            <div><dt className="text-xs text-gray-500">eBay lookups</dt><dd>{job.ebay_calls.toLocaleString()}</dd></div>
          </dl>

          {job.failures.length > 0 && (
            <details className="text-sm">
              <summary className="cursor-pointer text-gray-700">Show failures ({job.failures.length})</summary>
              <ul className="mt-2 max-h-56 list-disc space-y-1 overflow-y-auto pl-5 text-xs text-gray-600">
                {job.failures.map((f, i) => (
                  <li key={i}>{f}</li>
                ))}
              </ul>
              <p className="mt-2 text-xs text-gray-500">A listing with a failed photo gets no photo rows. Start the import again to retry it.</p>
            </details>
          )}
        </div>
      )}

      <div className="space-y-3 rounded-lg border border-gray-200 bg-white p-4">
        <div>
          <h2 className="text-base font-semibold">Fill in custom labels</h2>
          {labelTarget === "channel" && (
            <>
          <p className="mt-1 max-w-3xl text-sm text-gray-600">
            Either ask eBay directly (no report needed), or upload a Seller Hub report. Asking eBay only works for the store
            typed below, and only for active listings; use a report for ended ones.
          </p>
          <p className="mt-1 max-w-3xl text-sm text-gray-600">
            Report upload: an active or inactive listings report (CSV). Empty custom labels are filled in by item number, and
            listings in the report that are not in the import table are added. A label that is already set is never
            changed; if the report disagrees, it is listed below for you to look at.
          </p>
            </>
          )}
        </div>
        <div className="flex flex-wrap gap-4 text-sm">
          <label className="flex items-center gap-2">
            <input type="radio" name="labelTarget" checked={labelTarget === "channel"} onChange={() => setLabelTarget("channel")} disabled={uploading || apiBusy || invBusy} />
            <span><span className="font-medium">eBay channel</span> custom labels (the listings in the sales channel)</span>
          </label>
          <label className="flex items-center gap-2">
            <input type="radio" name="labelTarget" checked={labelTarget === "inventory"} onChange={() => setLabelTarget("inventory")} disabled={uploading || apiBusy || invBusy} />
            <span><span className="font-medium">Inventory</span> custom labels (the products)</span>
          </label>
        </div>
        {labelTarget === "inventory" && (
          <div className="space-y-3">
            <p className="max-w-3xl text-sm text-gray-600">
              Fills in the blank custom labels on the products. Each product&rsquo;s eBay item numbers (all the listings merged
              into it, or its own source listing if it was never merged) must agree on one label before it is applied. A product
              that already has a label is never changed; if one of its item numbers has a different label, it is listed below.
              Runs on the server, so you can leave this page.
            </p>
            <div className="max-w-3xl rounded border border-gray-200 p-3 text-sm">
              <div className="font-medium">Step 1: check against the labels already saved (no eBay calls, a minute or two)</div>
              <p className="mt-1 text-xs text-gray-500">
                Uses the Seller Hub reports and earlier eBay answers. Fills blank products whose item numbers agree, and lists the ones that do not.
              </p>
              <button type="button" className={btnCls + " mt-2"} disabled={invBusy || invRunning} onClick={() => void startInv("saved")}>
                Step 1: fill from saved labels
              </button>
              {inv && inv.unchecked > 0 && <span className="ml-3 text-xs text-gray-500">{inv.unchecked.toLocaleString()} products not checked yet</span>}
            </div>
            <div className="max-w-3xl rounded border border-gray-200 p-3 text-sm">
              <div className="font-medium">Step 2: check against eBay</div>
              <p className="mt-1 text-xs text-gray-500">
                Asks eBay about the active listings of the products that need it: blanks, the listed ones, and labels step 1 filled
                in. Ended listings cannot be asked and keep their saved label. If eBay&rsquo;s daily limit is reached it pauses;
                start it again later and it carries on.
              </p>
              <label className="mt-2 flex items-center gap-2 text-xs">
                <input type="checkbox" checked={invAll} onChange={(e) => setInvAll(e.target.checked)} disabled={invRunning} />
                Check every product on eBay, not only the ones above (takes much longer)
              </label>
              <div className="mt-2 flex flex-wrap items-center gap-3">
                <button type="button" className={btnCls} disabled={invBusy || invRunning || !inv || (inv.unchecked > 0 && Object.keys(inv.totals).length === 0)} onClick={() => void startInv("live", invAll ? "all" : "attention")}>
                  Step 2: check against eBay
                </button>
                <button type="button" className={btn2Cls} disabled={invBusy || invRunning || !inv || inv.flagged_total === 0} onClick={() => void startInv("live", "flagged")} title="Asks eBay again about the listed products, for example after you fixed a merge.">
                  Re-check the listed ones
                </button>
                {inv && <span className="text-xs text-gray-500">{inv.not_checked_on_ebay.toLocaleString()} products not checked on eBay yet</span>}
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-3">
              {invRunning && (
                <button type="button" className={btn2Cls} onClick={() => void stopInv()} disabled={inv?.job?.status === "stopping"}>
                  {inv?.job?.status === "stopping" ? "Stopping…" : "Stop"}
                </button>
              )}
              <button type="button" className={btn2Cls} disabled={invRunning} onClick={() => void resetInv()} title="Forgets which products were checked. Labels already filled in stay.">
                Start over
              </button>
              {inv && (
                <span className="text-xs text-gray-500">
                  {inv.blank_products.toLocaleString()} products have a blank label right now
                </span>
              )}
            </div>
          </div>
        )}
        {labelTarget === "inventory" && inv?.job && (() => {
          const j = inv.job;
          const pctInv = j.total > 0 ? Math.min(100, Math.round((j.done / j.total) * 100)) : j.running ? 0 : 100;
          const rate = j.elapsed_s > 5 && j.done > 0 ? j.done / j.elapsed_s : 0;
          const eta = j.running && rate > 0 && j.total > j.done ? Math.round((j.total - j.done) / rate) : 0;
          const c = j.counts;
          return (
            <div className="max-w-3xl space-y-2 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone={j.status === "done" ? "green" : j.status === "failed" || j.status === "paused" || j.running ? "amber" : "gray"}>
                  {{ running: "Running", stopping: "Stopping…", done: "Finished", stopped: "Stopped", paused: "Paused", failed: "Failed" }[j.status]}
                </Badge>
                <span className="font-medium">{j.mode === "saved" ? "Step 1: saved labels" : "Step 2: eBay" + (j.scope === "all" ? " (every product)" : j.scope === "flagged" ? " (listed ones)" : "")}</span>
                <span className="text-xs text-gray-500">
                  {j.done.toLocaleString()} of {j.total.toLocaleString()} products · {fmtTime(j.elapsed_s)} elapsed
                  {eta > 0 && <> · about {fmtTime(eta)} left</>}
                  {j.mode === "live" && <> · {j.ebay_calls.toLocaleString()} eBay lookups</>}
                </span>
              </div>
              <div className="h-2 w-full overflow-hidden rounded bg-gray-200">
                <div className="h-2 bg-blue-600 transition-all" style={{ width: `${pctInv}%` }} />
              </div>
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-700">
                <span>Filled in: <b>{(c.filled ?? 0).toLocaleString()}</b></span>
                <span>Already correct: <b>{(c.already_ok ?? 0).toLocaleString()}</b></span>
                <span>No label anywhere: <b>{(c.no_label ?? 0).toLocaleString()}</b></span>
                <span>Different label on an item number: <b>{(c.id_mismatch ?? 0).toLocaleString()}</b></span>
                <span>Labels disagree: <b>{(c.conflict ?? 0).toLocaleString()}</b></span>
                {(c.no_items ?? 0) > 0 && <span>No eBay item number: <b>{c.no_items.toLocaleString()}</b></span>}
                {j.mode === "live" && <span>Item numbers not asked (ended / other store): <b>{(c.unverified_ids ?? 0).toLocaleString()}</b></span>}
                {(c.errors ?? 0) > 0 && <span className="text-amber-700">Could not be read, retried next run: <b>{c.errors.toLocaleString()}</b></span>}
              </div>
              {j.message && <p className="text-xs text-gray-600">{j.message}</p>}
              {j.error_sample.length > 0 && <p className="text-xs text-amber-700">{j.error_sample.slice(0, 3).join(" · ")}</p>}
            </div>
          );
        })()}
        {labelTarget === "inventory" && !invRunning && inv && inv.flagged_total > 0 && invFlagged && (
          <div>
            <p className="font-medium text-sm">
              {inv.flagged_total.toLocaleString()} product{inv.flagged_total === 1 ? "" : "s"} to look at (nothing was changed on them)
              {inv.flagged_total > invFlagged.flagged.length && <> &mdash; first {invFlagged.flagged.length} shown</>}
            </p>
            <div className="mt-1 max-h-96 overflow-auto rounded border border-gray-200">
              <table className="w-full text-left text-xs">
                <thead className="sticky top-0 bg-gray-50 uppercase text-gray-500">
                  <tr>
                    <th className="px-3 py-2">Product</th>
                    <th className="px-3 py-2">Product label</th>
                    <th className="px-3 py-2">Item number</th>
                    <th className="px-3 py-2">Label on that item</th>
                    <th className="px-3 py-2">Saved / eBay</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {invFlagged.flagged.map((f) =>
                    f.items.map((it, n) => (
                      <tr key={f.product_id + "-" + it.item} className={it.differs_from_product || it.problem ? "bg-amber-50" : ""}>
                        <td className="px-3 py-1.5 align-top">
                          {n === 0 && (
                            <a className="text-blue-700 underline" href={"/admin/products/" + f.product_id} target="_blank" rel="noreferrer">
                              {f.sku || "#" + f.product_id}
                            </a>
                          )}
                          {n === 0 && <div className="max-w-xs truncate text-gray-500">{f.title}</div>}
                        </td>
                        <td className="px-3 py-1.5 align-top">{n === 0 ? f.product_label || "(blank)" : ""}</td>
                        <td className="px-3 py-1.5">{it.item}</td>
                        <td className="px-3 py-1.5">{it.label || "(none)"}</td>
                        <td className="px-3 py-1.5 text-gray-600">
                          {it.saved || "—"} / {it.checked_on_ebay ? it.ebay || "(none)" : "not asked"}
                          {it.problem && <div className="text-amber-700">{it.problem}</div>}
                        </td>
                      </tr>
                    )),
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}
        {labelTarget === "channel" && (
          <>
        <label className="block max-w-sm text-sm">
          <span className="mb-1 block font-medium">eBay store name (optional)</span>
          <StoreInput value={labelsStore} onChange={setLabelsStore} placeholder="eandimotorsports or genuine-oem-parts" disabled={uploading || apiBusy} />
          <span className="mt-1 block text-xs text-gray-500">
            The store these listings belong to. Needed for &ldquo;Fill from eBay&rdquo;. For a report upload, left blank, no store is recorded for these listings (it is not assumed).
          </span>
        </label>
        <div className="flex flex-wrap items-center gap-3">
          <button type="button" className={btnCls} disabled={apiBusy || uploading} onClick={() => void fillFromEbay()} title="Asks eBay for the custom label of up to 100 listings that have none yet. Changes nothing on eBay.">
            {apiBusy ? "Asking eBay…" : "Fill from eBay"}
          </button>
          <span className="text-xs text-gray-500">Asks eBay for up to 100 listings per click (about a minute). Click again for more. Keep this page open.</span>
        </div>
        {apiLabels && (
          <div className="space-y-2 text-sm">
            <Notice kind={apiLabels.errors > 0 || apiLabels.rate_limited ? "error" : "success"}>
              {apiLabels.store}: {(apiLabels.labels_filled + apiLabels.filled_from_saved).toLocaleString()} labels filled in
              {apiLabels.filled_from_saved > 0 && <> ({apiLabels.filled_from_saved.toLocaleString()} from answers eBay already gave earlier)</>}.{" "}
              Asked eBay about {apiLabels.asked_ebay.toLocaleString()} listing{apiLabels.asked_ebay === 1 ? "" : "s"}
              {apiLabels.no_label_on_ebay > 0 && <>, {apiLabels.no_label_on_ebay.toLocaleString()} have no custom label on eBay</>}
              {apiLabels.not_active > 0 && <>, {apiLabels.not_active.toLocaleString()} are not active on eBay (use a report for those)</>}
              {apiLabels.other_store > 0 && <>, {apiLabels.other_store.toLocaleString()} belong to a different store</>}
              {apiLabels.errors > 0 && <>, {apiLabels.errors.toLocaleString()} could not be read ({apiLabels.error_sample.slice(0, 3).join(" · ")})</>}.{" "}
              {apiLabels.rate_limited ? `Stopped: ${apiLabels.rate_limited} ` : ""}
              {apiLabels.remaining > 0 ? `${apiLabels.remaining.toLocaleString()} still to ask: click again.` : "None left to ask for this store."}
              {apiLabels.blank_without_store > 0 && <> {apiLabels.blank_without_store.toLocaleString()} blank listings have no store recorded, so they were not asked; running the photo import for a store records it.</>}
            </Notice>
          </div>
        )}
        <div className="flex flex-wrap items-center gap-3">
          <input
            ref={fileRef}
            type="file"
            accept=".csv,text/csv"
            className="hidden"
            onChange={(e) => {
              setFile(e.target.files?.[0] ?? null);
              setLabels(null);
              setLabelsErr("");
            }}
          />
          <button type="button" className={btn2Cls} disabled={uploading || apiBusy} onClick={() => fileRef.current?.click()}>
            Choose report (CSV)
          </button>
          <button type="button" className={btnCls} disabled={!file || uploading || apiBusy} onClick={() => void uploadReport()}>
            {uploading ? "Reading…" : "Upload report"}
          </button>
        </div>
        <p className="text-xs text-gray-500">
          {uploading ? "Reading the report…" : file ? `Selected: ${file.name}` : "No file chosen yet."}
        </p>
          </>
        )}
        {labelsErr && <Notice kind="error">{labelsErr}</Notice>}
        {labelTarget === "channel" && labels && (
          <div className="space-y-2 text-sm">
            <Notice kind="success">
              {labels.listings_in_report.toLocaleString()} listings in the report. {labels.labels_filled.toLocaleString()} labels
              filled in, {labels.listings_added.toLocaleString()} listings added, {labels.already_had_same_label.toLocaleString()}{" "}
              already had the same label
              {labels.already_linked_skipped > 0 && <>, {labels.already_linked_skipped.toLocaleString()} skipped because they are already linked to a product</>}
              {labels.unreadable_rows > 0 && <>, {labels.unreadable_rows.toLocaleString()} rows could not be read</>}.
            </Notice>
            {labels.differs_count > 0 && (
              <div>
                <p className="font-medium">
                  {labels.differs_count.toLocaleString()} listing{labels.differs_count === 1 ? "" : "s"} where the report&rsquo;s label differs from the stored one (nothing was changed)
                  {labels.differs_count > labels.differs.length && <> &mdash; first {labels.differs.length} shown</>}
                </p>
                <div className="mt-1 max-h-64 overflow-auto rounded border border-gray-200">
                  <table className="w-full text-left text-xs">
                    <thead className="bg-gray-50 uppercase text-gray-500">
                      <tr><th className="px-3 py-2">Item</th><th className="px-3 py-2">Stored</th><th className="px-3 py-2">Report</th></tr>
                    </thead>
                    <tbody className="divide-y divide-gray-100">
                      {labels.differs.map((d) => (
                        <tr key={d.item}><td className="px-3 py-1.5">{d.item}</td><td className="px-3 py-1.5">{d.current}</td><td className="px-3 py-1.5">{d.report}</td></tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
