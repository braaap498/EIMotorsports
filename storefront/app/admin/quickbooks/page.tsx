"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Badge, Modal, Notice, btnCls, btn2Cls, inputCls } from "../../../components/admin/ui";

/* ------------------------------------------------------------------ types */

type EntityRow = {
  entity: string;
  records: number;
  removed: number;
  last_synced: string | null;
  last_success_at: string | null;
  last_full_at: string | null;
  last_error: string | null;
};
type RunError = { entity: string | null; code: string | null; message: string; at: string };
type Run = {
  id: number;
  started_at: string;
  finished_at: string | null;
  status: "running" | "ok" | "partial" | "failed";
  trigger: string | null;
  full_resync: boolean;
  step: number;
  steps: number;
  company_file?: string | null;
  counts: Record<string, number>;
  errors: RunError[];
};
type Status = {
  tables_ready: boolean;
  credentials_set: boolean;
  username: string;
  endpoint: string;
  entities_filter: string;
  full_refresh_hours: number;
  run_every_minutes: number;
  control?: { sync_requested: boolean; full_resync: boolean };
  entities?: EntityRow[];
  errors_by_entity?: { entity: string; last_error: string }[];
  last_run?: Run | null;
};
type Rec = {
  entity: string;
  qb_id: string;
  record_type: string | null;
  name: string | null;
  party: string | null;
  txn_date: string | null;
  amount: number | null;
  modified_at: string | null;
  synced_at: string;
};
type RecList = { total: number; items: Rec[] };
type RecFull = Rec & { deleted_at: string | null; data: Record<string, unknown> };

const PAGE = 50;
const when = (s: string | null) => (s ? new Date(s).toLocaleString() : "—");
const money = (v: number | null) => (v == null ? "" : v.toLocaleString(undefined, { style: "currency", currency: "USD" }));
const every = (n: number) => (n % 60 === 0 ? (n === 60 ? "hour" : `${n / 60} hours`) : `${n} minutes`);
const statusTone = (s: string) => (s === "ok" ? "green" : s === "running" ? "blue" : s === "partial" ? "amber" : "gray");

/* ----------------------------------------------------------- record viewer */

function RecordModal({ entity, id, onClose }: { entity: string; id: string; onClose: () => void }) {
  const [rec, setRec] = useState<RecFull | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    api<RecFull>(`/quickbooks/records/${encodeURIComponent(entity)}/${encodeURIComponent(id)}`)
      .then(setRec)
      .catch((e) => setError(errMsg(e)));
  }, [entity, id]);
  return (
    <Modal title={`${entity} ${rec?.name ?? ""}`.trim()} onClose={onClose}>
      {error && <Notice kind="error">{error}</Notice>}
      {!rec && !error && <p className="text-sm text-gray-500">Loading…</p>}
      {rec && (
        <div className="space-y-2">
          {rec.deleted_at && <Notice kind="info">QuickBooks no longer has this record (removed {when(rec.deleted_at)}).</Notice>}
          <pre className="max-h-[60vh] overflow-auto rounded border border-gray-200 bg-gray-50 p-3 text-xs">
            {JSON.stringify(rec.data, null, 2)}
          </pre>
        </div>
      )}
    </Modal>
  );
}

/* ------------------------------------------------------------------- page */

/* ------------------------------------------------- copy customers to the store */

type CopyResult = {
  dry_run: boolean;
  qb_customers: number;
  jobs_skipped: number;
  no_email: number;
  bad_email: number;
  duplicate_email_in_qb: number;
  already_imported: number;
  matched_existing: number;
  new_real: number;
  new_masked: Record<string, number>;
  new_masked_total: number;
  added: number;
  linked: number;
  stopped_at_limit: boolean;
  samples: { new_real: { email: string; name: string | null }[]; new_masked: { email: string; name: string | null; marketplace: string }[]; bad_email: string[] };
};

function CustomerCopy() {
  const [res, setRes] = useState<CopyResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function run(dryRun: boolean) {
    if (!dryRun && !window.confirm("Add the new QuickBooks customers to the store's customers now? Existing customers are never overwritten.")) return;
    setBusy(true);
    setError("");
    try {
      setRes(await api<CopyResult>("/quickbooks/customers-to-store", { method: "POST", body: { dry_run: dryRun } }));
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  const n = (v: number) => v.toLocaleString();
  const masked = res ? Object.entries(res.new_masked).map(([k, v]) => `${k} ${n(v)}`).join(", ") : "";

  return (
    <section className="space-y-3 rounded-lg border border-gray-200 bg-white p-4">
      <div>
        <h2 className="text-lg font-semibold">Copy customers to the store</h2>
        <p className="mt-1 max-w-3xl text-sm text-gray-600">
          Adds QuickBooks customers that are not in the store yet. Run it again any time: only new ones are added, and anything already in the store is
          left as it is (blank fields are filled in). Masked marketplace emails (eBay, Amazon) are tagged so they sort apart from real ones.
        </p>
      </div>
      <div className="flex flex-wrap gap-3">
        <button type="button" className={btn2Cls} disabled={busy} onClick={() => void run(true)}>
          {busy ? "Working…" : "Preview"}
        </button>
        <button type="button" className={btnCls} disabled={busy || !res || !res.dry_run} onClick={() => void run(false)}>
          Add new customers
        </button>
      </div>
      {error && <Notice kind="error">{error}</Notice>}
      {res && (
        <div className="space-y-2 text-sm">
          <Notice kind={res.dry_run ? "info" : "success"}>
            {res.dry_run
              ? `Preview only, nothing was changed. Would add ${n(res.new_real + res.new_masked_total)} new customers (${n(res.new_real)} real email, ${n(res.new_masked_total)} masked${masked ? `: ${masked}` : ""}).`
              : `Added ${n(res.added)} new customers and linked ${n(res.linked)} existing ones.${res.stopped_at_limit ? " More are waiting: click Preview, then Add again." : ""}`}
          </Notice>
          <ul className="grid gap-x-8 gap-y-1 text-gray-700 sm:grid-cols-2">
            <li>QuickBooks customers read: {n(res.qb_customers)}</li>
            <li>Already copied before: {n(res.already_imported)}</li>
            <li>Already in the store (same email): {n(res.matched_existing)}</li>
            <li>No email, not copied: {n(res.no_email)}</li>
            <li>Unusable email, not copied: {n(res.bad_email)}</li>
            <li>Same email used by another QuickBooks customer: {n(res.duplicate_email_in_qb)}</li>
            <li>Jobs under a customer, skipped: {n(res.jobs_skipped)}</li>
          </ul>
          {(res.samples.new_real.length > 0 || res.samples.new_masked.length > 0) && (
            <details className="text-gray-700">
              <summary className="cursor-pointer text-gray-600 underline">Examples</summary>
              <ul className="mt-1 list-disc pl-5">
                {res.samples.new_real.map((s) => (
                  <li key={s.email}>real: {s.name || "(no name)"}, {s.email}</li>
                ))}
                {res.samples.new_masked.map((s) => (
                  <li key={s.email}>masked ({s.marketplace}): {s.name || "(no name)"}, {s.email}</li>
                ))}
              </ul>
            </details>
          )}
        </div>
      )}
    </section>
  );
}

export default function QuickBooksPage() {
  const [status, setStatus] = useState<Status | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");
  const [busy, setBusy] = useState(false);
  const [showSetup, setShowSetup] = useState(false);

  // record browser
  const [entity, setEntity] = useState("");
  const [q, setQ] = useState("");
  const [removed, setRemoved] = useState(false);
  const [page, setPage] = useState(0);
  const [list, setList] = useState<RecList | null>(null);
  const [open, setOpen] = useState<Rec | null>(null);

  const loadStatus = useCallback(async () => {
    try {
      const s = await api<Status>("/quickbooks/status");
      setStatus(s);
      if (s.tables_ready) setRuns(await api<Run[]>("/quickbooks/runs?limit=8"));
    } catch (e) {
      setError(errMsg(e));
    }
  }, []);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  // While a sync is queued or running, refresh every few seconds.
  const active = status?.last_run?.status === "running" || status?.control?.sync_requested;
  // Each refresh waits for the previous one to finish (they used to overlap and pile up on the database),
  // and nothing is requested while this browser tab is in the background.
  useEffect(() => {
    if (!active) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      if (!document.hidden) await loadStatus();
      if (!stopped) timer = setTimeout(() => void tick(), 8000);
    };
    timer = setTimeout(() => void tick(), 8000);
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [active, loadStatus]);

  const loadList = useCallback(async () => {
    if (!status?.tables_ready) return;
    try {
      const qs = new URLSearchParams({ limit: String(PAGE), offset: String(page * PAGE) });
      if (entity) qs.set("entity", entity);
      if (q.trim()) qs.set("q", q.trim());
      if (removed) qs.set("removed", "true");
      setList(await api<RecList>(`/quickbooks/records?${qs}`));
    } catch (e) {
      setError(errMsg(e));
    }
  }, [status?.tables_ready, entity, q, removed, page]);

  useEffect(() => {
    void loadList();
  }, [loadList]);

  async function syncNow(full: boolean) {
    setBusy(true);
    setError("");
    setInfo("");
    try {
      await api("/quickbooks/sync-now", { method: "POST", body: { full } });
      setInfo(
        full
          ? `Full re-read requested. It starts the next time the Web Connector checks in (every ${every(status?.run_every_minutes ?? 60)} by default), or right away if you click Update Selected in the Web Connector on the QuickBooks PC.`
          : `Sync requested. It starts the next time the Web Connector checks in (every ${every(status?.run_every_minutes ?? 60)} by default), or right away if you click Update Selected in the Web Connector on the QuickBooks PC.`,
      );
      await loadStatus();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  async function downloadQwc() {
    setError("");
    try {
      const r = await api<{ filename: string; xml: string }>("/quickbooks/qwc");
      const url = URL.createObjectURL(new Blob([r.xml], { type: "text/xml" }));
      const a = document.createElement("a");
      a.href = url;
      a.download = r.filename;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(errMsg(e));
    }
  }

  const last = status?.last_run ?? null;
  const entities = status?.entities ?? [];
  const totalRecords = entities.reduce((n, e) => n + e.records, 0);
  const pages = list ? Math.max(1, Math.ceil(list.total / PAGE)) : 1;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">QuickBooks</h1>
        <p className="mt-1 max-w-3xl text-sm text-gray-600">
          A read-only copy of your QuickBooks Desktop data: customers, items, invoices, payments and more. QuickBooks is
          never changed from here. The copy is kept separate from your products and orders.
        </p>
      </div>

      {error && <Notice kind="error">{error}</Notice>}
      {info && <Notice kind="success">{info}</Notice>}

      {status && !status.tables_ready && (
        <Notice kind="error">
          The QuickBooks tables have not been created yet. Run <code>qb_tables.sql</code> once in the Supabase SQL editor,
          then reload this page.
        </Notice>
      )}

      {status && status.tables_ready && (
        <>
          {/* status + controls */}
          <section className="space-y-3 rounded-lg border border-gray-200 bg-white p-4">
            <div className="flex flex-wrap items-center gap-3">
              <button type="button" className={btnCls} disabled={busy || !!status.control?.sync_requested} onClick={() => void syncNow(false)}>
                {status.control?.sync_requested ? "Sync requested…" : "Sync now"}
              </button>
              <button type="button" className={btn2Cls} disabled={busy || !!status.control?.sync_requested} onClick={() => void syncNow(true)}>
                Full re-read
              </button>
              <button type="button" className={btn2Cls} onClick={() => void downloadQwc()} disabled={!status.credentials_set}>
                Download connection file
              </button>
              <button type="button" className="text-sm text-gray-600 underline" onClick={() => setShowSetup((v) => !v)}>
                {showSetup ? "Hide setup steps" : "Setup steps"}
              </button>
            </div>

            {!status.credentials_set && (
              <Notice kind="info">
                Set <code>QBWC_USERNAME</code> and <code>QBWC_PASSWORD</code> on the API (Render), redeploy, then reload.
              </Notice>
            )}

            <dl className="grid gap-x-8 gap-y-2 text-sm sm:grid-cols-2 lg:grid-cols-[auto_auto_minmax(0,1fr)_auto]">
              <div>
                <dt className="text-xs uppercase text-gray-500">Last sync</dt>
                <dd>
                  {last ? (
                    <>
                      <Badge tone={statusTone(last.status)}>{last.status}</Badge>{" "}
                      <span className="text-gray-700">{when(last.finished_at ?? last.started_at)}</span>
                    </>
                  ) : (
                    <span className="text-gray-500">Never. Waiting for the Web Connector.</span>
                  )}
                </dd>
              </div>
              <div>
                <dt className="text-xs uppercase text-gray-500">Records saved</dt>
                <dd>{totalRecords.toLocaleString()}</dd>
              </div>
              <div>
                <dt className="text-xs uppercase text-gray-500">QuickBooks company file</dt>
                <dd className="truncate" title={last?.company_file ?? ""}>{last?.company_file || "—"}</dd>
              </div>
              <div>
                <dt className="text-xs uppercase text-gray-500">Connection address</dt>
                <dd className="break-all lg:whitespace-nowrap lg:break-normal">{status.endpoint}</dd>
              </div>
            </dl>

            {status.entities_filter && (
              <p className="text-xs text-amber-700">
                Only syncing: {status.entities_filter} (set by QB_ENTITIES). Remove that setting to sync everything.
              </p>
            )}

            {showSetup && (
              <ol className="list-decimal space-y-1.5 pl-5 text-sm text-gray-700">
                <li>On Render, set <code>QBWC_USERNAME</code> and <code>QBWC_PASSWORD</code> (you choose both) and redeploy the API.</li>
                <li>Click &ldquo;Download connection file&rdquo; and copy the .qwc file to the PC that runs QuickBooks.</li>
                <li>On that PC, open QuickBooks and your company file as an admin, in single-user mode.</li>
                <li>Open the QuickBooks Web Connector (Start menu; it is installed with QuickBooks), choose Add an Application, and pick the .qwc file.</li>
                <li>
                  QuickBooks asks whether to let &ldquo;E&amp;I Motorsports&rdquo; read the file. Choose &ldquo;Yes, always; allow access even if
                  QuickBooks is not running&rdquo; and pick an admin user. The file only asks for read access.
                </li>
                <li>Back in the Web Connector, type the password you set in step 1 into the Password box, then click Update Selected to run the first sync now.</li>
                <li>The first sync reads all of your history and can take a while. After that it checks in about every {every(status.run_every_minutes)} (you can change this in the Web Connector window), and the QuickBooks PC needs to be on.</li>
              </ol>
            )}
          </section>

          <CustomerCopy />

          {/* errors */}
          {(status.errors_by_entity?.length ?? 0) > 0 && (
            <section className="rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm">
              <h2 className="font-semibold text-amber-900">Things QuickBooks would not give us</h2>
              <ul className="mt-1 list-disc space-y-0.5 pl-5 text-amber-900">
                {status.errors_by_entity!.map((e) => (
                  <li key={e.entity}>
                    <span className="font-medium">{e.entity}</span>: {e.last_error}
                  </li>
                ))}
              </ul>
              <p className="mt-1 text-xs text-amber-800">
                Some kinds of records do not exist in every QuickBooks edition (for example assemblies), so a message here is not always a problem.
              </p>
            </section>
          )}

          {/* what we have */}
          {entities.length > 0 && (
            <section>
              <h2 className="mb-2 text-lg font-semibold">What has been saved</h2>
              <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white">
                <table className="w-full text-left text-sm">
                  <thead className="bg-gray-50 text-xs uppercase text-gray-500">
                    <tr>
                      <th className="px-3 py-2">Kind</th>
                      <th className="px-3 py-2 text-right">Records</th>
                      <th className="px-3 py-2 text-right">Removed in QuickBooks</th>
                      <th className="px-3 py-2">Last updated</th>
                      <th className="px-3 py-2"></th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100">
                    {entities.map((e) => (
                      <tr key={e.entity}>
                        <td className="px-3 py-2 font-medium">{e.entity}</td>
                        <td className="px-3 py-2 text-right">{e.records.toLocaleString()}</td>
                        <td className="px-3 py-2 text-right text-gray-500">{e.removed || ""}</td>
                        <td className="whitespace-nowrap px-3 py-2 text-gray-600">{when(e.last_success_at ?? e.last_synced)}</td>
                        <td className="px-3 py-2 text-right">
                          <button
                            type="button"
                            className="text-sm underline"
                            onClick={() => {
                              setEntity(e.entity);
                              setPage(0);
                            }}
                          >
                            Browse
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {/* browser */}
          <section className="space-y-3">
            <h2 className="text-lg font-semibold">Browse records</h2>
            <div className="flex flex-wrap items-center gap-3">
              <select
                className={`${inputCls} max-w-xs`}
                value={entity}
                onChange={(e) => {
                  setEntity(e.target.value);
                  setPage(0);
                }}
              >
                <option value="">All kinds</option>
                {entities.map((e) => (
                  <option key={e.entity} value={e.entity}>
                    {e.entity} ({e.records.toLocaleString()})
                  </option>
                ))}
              </select>
              <input
                className={`${inputCls} max-w-xs`}
                placeholder="Search name, customer or number"
                value={q}
                onChange={(e) => {
                  setQ(e.target.value);
                  setPage(0);
                }}
              />
              <label className="flex items-center gap-2 text-sm text-gray-700">
                <input
                  type="checkbox"
                  checked={removed}
                  onChange={(e) => {
                    setRemoved(e.target.checked);
                    setPage(0);
                  }}
                />
                Show removed
              </label>
            </div>

            <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white">
              <table className="w-full text-left text-sm">
                <thead className="bg-gray-50 text-xs uppercase text-gray-500">
                  <tr>
                    <th className="px-3 py-2">Kind</th>
                    <th className="px-3 py-2">Name / number</th>
                    <th className="px-3 py-2">Customer / vendor</th>
                    <th className="px-3 py-2">Date</th>
                    <th className="px-3 py-2 text-right">Amount</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {list?.items.map((r) => (
                    <tr key={`${r.entity}:${r.qb_id}`} className="cursor-pointer hover:bg-gray-50" onClick={() => setOpen(r)}>
                      <td className="whitespace-nowrap px-3 py-2">
                        {r.entity}
                        {r.entity === "Item" && r.record_type && (
                          <span className="ml-1 text-xs text-gray-500">{r.record_type.replace(/^Item|Ret$/g, "")}</span>
                        )}
                      </td>
                      <td className="px-3 py-2 font-medium">{r.name || r.qb_id}</td>
                      <td className="px-3 py-2 text-gray-600">{r.party || ""}</td>
                      <td className="whitespace-nowrap px-3 py-2 text-gray-600">{r.txn_date ?? ""}</td>
                      <td className="whitespace-nowrap px-3 py-2 text-right">{money(r.amount)}</td>
                    </tr>
                  ))}
                  {list && list.items.length === 0 && (
                    <tr>
                      <td colSpan={5} className="px-3 py-6 text-center text-gray-500">
                        Nothing here yet.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>

            {list && list.total > PAGE && (
              <div className="flex items-center justify-between text-sm text-gray-600">
                <span>
                  Page {page + 1} of {pages} ({list.total.toLocaleString()} records)
                </span>
                <span className="flex gap-2">
                  <button type="button" className={btn2Cls} disabled={page === 0} onClick={() => setPage((p) => p - 1)}>
                    Previous
                  </button>
                  <button type="button" className={btn2Cls} disabled={page + 1 >= pages} onClick={() => setPage((p) => p + 1)}>
                    Next
                  </button>
                </span>
              </div>
            )}
          </section>

          {/* history */}
          {runs.length > 0 && (
            <section>
              <h2 className="mb-2 text-lg font-semibold">Recent syncs</h2>
              <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white">
                <table className="w-full text-left text-sm">
                  <thead className="bg-gray-50 text-xs uppercase text-gray-500">
                    <tr>
                      <th className="px-3 py-2">Started</th>
                      <th className="px-3 py-2">Result</th>
                      <th className="px-3 py-2">How</th>
                      <th className="px-3 py-2 text-right">Records read</th>
                      <th className="px-3 py-2">Problems</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100">
                    {runs.map((r) => (
                      <tr key={r.id} className="align-top">
                        <td className="whitespace-nowrap px-3 py-2">{when(r.started_at)}</td>
                        <td className="px-3 py-2">
                          <Badge tone={statusTone(r.status)}>{r.status}</Badge>
                          {r.status === "running" && (
                            <span className="ml-1 text-xs text-gray-500">
                              step {r.step} of {r.steps}
                            </span>
                          )}
                        </td>
                        <td className="px-3 py-2 text-gray-600">
                          {r.trigger === "manual" ? "Requested" : "Scheduled"}
                          {r.full_resync ? ", full" : ""}
                        </td>
                        <td className="px-3 py-2 text-right">
                          {Object.values(r.counts || {}).reduce((n, v) => n + v, 0).toLocaleString()}
                        </td>
                        <td className="max-w-md px-3 py-2 text-xs text-gray-600">
                          {r.errors?.length ? (
                            <ul className="space-y-0.5">
                              {r.errors.slice(0, 4).map((e, i) => (
                                <li key={i}>
                                  {e.entity ? <span className="font-medium">{e.entity}: </span> : null}
                                  {e.message}
                                </li>
                              ))}
                              {r.errors.length > 4 && <li>and {r.errors.length - 4} more</li>}
                            </ul>
                          ) : (
                            ""
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}
        </>
      )}

      {open && <RecordModal entity={open.entity} id={open.qb_id} onClose={() => setOpen(null)} />}
    </div>
  );
}
