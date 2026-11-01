"use client";

import { useEffect, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Badge, Notice, btnCls, btn2Cls, inputCls } from "../../../components/admin/ui";

/* ------------------------------------------------------------------ types */

type Job = {
  id: number;
  store: string;
  status: "running" | "stopping" | "done" | "stopped" | "paused" | "failed";
  running: boolean;
  listings_found: number;
  already_saved: number;
  items_total: number;
  items_done: number;
  with_compat: number;
  compat_rows: number;
  no_list: number;
  count_only: number;
  not_found: number;
  failed: number;
  mode: "new_only" | "recheck";
  rechecked: number;
  changed: number;
  new_rows: number;
  ebay_calls: number;
  message: string | null;
  failures: string[];
  started_at: string;
  finished_at: string | null;
};
type Saved = { store: string; status: string; n: number };
type StatusResp = { job: Job | null; saved: Saved[] };

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
const SAVED_NAME: Record<string, string> = {
  ok: "With a compatibility list",
  count_only: "eBay gave a count only",
  no_list: "No compatibility on the listing",
  not_found: "Ended / not accessible",
};

/* ------------------------------------------------------------------- page */

export default function EbayCompatImportPage() {
  const [store, setStore] = useState("");
  const [job, setJob] = useState<Job | null>(null);
  const [saved, setSaved] = useState<Saved[]>([]);
  const [error, setError] = useState("");
  const [starting, setStarting] = useState(false);

  async function refresh() {
    try {
      const r = await api<StatusResp>("/ebay-compat/status");
      setJob(r.job);
      setSaved(r.saved);
    } catch (e) {
      setError(errMsg(e));
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  // While a run is going, ask for its progress every few seconds.
  const running = !!job?.running;
  useEffect(() => {
    if (!running) return;
    const t = setInterval(() => void refresh(), 3000);
    return () => clearInterval(t);
  }, [running]);

  async function start(recheck: boolean) {
    setStarting(true);
    setError("");
    try {
      const j = await api<Job>("/ebay-compat/start", { method: "POST", body: { store: store.trim(), recheck } });
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
      const r = await api<StatusResp>("/ebay-compat/stop", { method: "POST", body: {} });
      setJob(r.job);
      setSaved(r.saved);
    } catch (e) {
      setError(errMsg(e));
    }
  }

  const pct = job && job.items_total > 0 ? Math.min(100, Math.round((job.items_done / job.items_total) * 100)) : 0;
  const stores = Array.from(new Set(saved.map((s) => s.store)));

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-xl font-semibold">eBay compatibility import</h2>
        <p className="mt-1 max-w-3xl text-sm text-gray-600">
          Reads each active listing&rsquo;s &ldquo;compatible vehicles&rdquo; table from eBay and saves it by eBay item
          number, together with the make, model and years read from the listing title. It works for listings that are
          not products yet. Any item number that already has compatibility data saved is skipped, so only new listings (and
          ones that failed last time) are looked up. The store must be approved on the Live listings tab first.
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
          <span className="mt-1 block text-xs text-gray-500">Leave blank for eandimotorsports. Type genuine-oem-parts for the other store.</span>
        </label>
        <div className="flex flex-wrap gap-3">
          <button type="button" className={btnCls} disabled={running || starting} onClick={() => void start(false)}>
            Pull compatibility data for listings not saved yet
          </button>
          <button type="button" className={btnCls} disabled={running || starting} onClick={() => void start(true)}>
            Also re-check saved listings for new compatibility data
          </button>
        </div>
        <p className="max-w-3xl text-xs text-gray-500">
          The second button does the first, then re-reads listings that already have compatibility data and adds any vehicle
          eBay lists now that we did not have before (old rows are kept). It uses one eBay lookup per saved listing, so
          a big store takes a few days of the daily limit; listings checked in the last 7 days are skipped, so you can
          press it again each day until it reports nothing left.
        </p>
        <p className="max-w-3xl text-xs text-gray-500">
          eBay allows about 5,000 of these lookups a day. If the limit is reached the import pauses; start it again
          later and it carries on where it stopped.
        </p>
      </div>

      {job && (
        <div className="space-y-3 rounded-lg border border-gray-200 bg-white p-4">
          <div className="flex flex-wrap items-center gap-3">
            <Badge tone={STATUS_TONE[job.status]}>{STATUS_TEXT[job.status]}</Badge>
            <span className="text-sm font-medium">{job.store}</span>
            <span className="text-sm text-gray-500">{job.mode === "recheck" ? "With re-check of saved listings" : "New listings only"}</span>
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
                {job.items_done.toLocaleString()} of {job.items_total.toLocaleString()} looked up ({pct}%)
              </div>
            </div>
          )}

          {job.message && <p className="text-sm text-gray-700">{job.message}</p>}

          <dl className="grid max-w-3xl grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-3">
            <div><dt className="text-xs text-gray-500">Active listings on eBay</dt><dd>{job.listings_found.toLocaleString()}</dd></div>
            <div><dt className="text-xs text-gray-500">Skipped (already saved)</dt><dd>{job.already_saved.toLocaleString()}</dd></div>
            <div><dt className="text-xs text-gray-500">With a compatibility list</dt><dd>{job.with_compat.toLocaleString()}</dd></div>
            <div><dt className="text-xs text-gray-500">Vehicles received</dt><dd>{job.compat_rows.toLocaleString()}</dd></div>
            <div><dt className="text-xs text-gray-500">No compatibility on listing</dt><dd>{job.no_list.toLocaleString()}</dd></div>
            <div><dt className="text-xs text-gray-500">Count only / not accessible</dt><dd>{job.count_only.toLocaleString()} / {job.not_found.toLocaleString()}</dd></div>
            {job.mode === "recheck" && (
              <>
                <div><dt className="text-xs text-gray-500">Saved listings re-checked</dt><dd>{job.rechecked.toLocaleString()}</dd></div>
                <div><dt className="text-xs text-gray-500">Had new vehicles</dt><dd>{job.changed.toLocaleString()}</dd></div>
                <div><dt className="text-xs text-gray-500">New vehicles added</dt><dd>{job.new_rows.toLocaleString()}</dd></div>
              </>
            )}
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
              <p className="mt-2 text-xs text-gray-500">Failed listings are not saved, so the next run tries them again.</p>
            </details>
          )}
        </div>
      )}

      {stores.length > 0 && (
        <div className="space-y-3 rounded-lg border border-gray-200 bg-white p-4">
          <h3 className="text-base font-semibold">Saved so far</h3>
          <div className="grid gap-4 sm:grid-cols-2">
            {stores.map((s) => (
              <div key={s}>
                <div className="mb-1 text-sm font-medium">{s}</div>
                <table className="w-full text-left text-sm">
                  <tbody className="divide-y divide-gray-100">
                    {saved
                      .filter((x) => x.store === s)
                      .map((x) => (
                        <tr key={x.status}>
                          <td className="py-1 pr-3 text-gray-600">{SAVED_NAME[x.status] ?? x.status}</td>
                          <td className="py-1 text-right">{x.n.toLocaleString()}</td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
