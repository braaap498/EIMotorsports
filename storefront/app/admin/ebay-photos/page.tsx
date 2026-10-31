"use client";

import { useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Badge, Notice, btnCls, btn2Cls, inputCls } from "../../../components/admin/ui";

/* ------------------------------------------------------------------ types */

type Job = {
  id: number;
  store: string;
  mode: "new_only" | "all_missing";
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
type LabelsResp = {
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
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const [starting, setStarting] = useState(false);

  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [labelsErr, setLabelsErr] = useState("");
  const [labels, setLabels] = useState<LabelsResp | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

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

  async function start(mode: Job["mode"]) {
    setStarting(true);
    setError("");
    try {
      const j = await api<Job>("/ebay-photos/import", { method: "POST", body: { store: store.trim(), mode } });
      setJob(j);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setStarting(false);
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
      setLabels(await api<LabelsResp>("/ebay-photos/labels", { method: "POST", body: { csv } }));
      setFile(null);
      if (fileRef.current) fileRef.current.value = "";
    } catch (e) {
      setLabelsErr(errMsg(e));
    } finally {
      setUploading(false);
    }
  }

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
          <input
            className={inputCls}
            value={store}
            onChange={(e) => setStore(e.target.value)}
            placeholder="eandimotorsports"
            disabled={running}
          />
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
          <h2 className="text-base font-semibold">Fill in custom labels from a Seller Hub report</h2>
          <p className="mt-1 max-w-3xl text-sm text-gray-600">
            Upload an active or inactive listings report (CSV). Empty custom labels are filled in by item number, and
            listings in the report that are not in the import table are added. A label that is already set is never
            changed; if the report disagrees, it is listed below for you to look at.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <input
            ref={fileRef}
            type="file"
            accept=".csv,text/csv"
            className="text-sm"
            onChange={(e) => {
              setFile(e.target.files?.[0] ?? null);
              setLabels(null);
              setLabelsErr("");
            }}
          />
          <button type="button" className={btnCls} disabled={!file || uploading} onClick={() => void uploadReport()}>
            {uploading ? "Reading…" : "Upload report"}
          </button>
        </div>
        {labelsErr && <Notice kind="error">{labelsErr}</Notice>}
        {labels && (
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
