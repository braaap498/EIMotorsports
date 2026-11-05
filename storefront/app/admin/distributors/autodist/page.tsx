"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../../../lib/adminApi";
import { Badge, Notice, btnCls, btn2Cls, inputCls } from "../../../../components/admin/ui";
import { useAccess } from "../../../../components/admin/AdminAccess";
import AutodistCatalog from "../../../../components/admin/AutodistCatalog";
import AutodistOpportunities from "../../../../components/admin/AutodistOpportunities";
import AutodistOrders from "../../../../components/admin/AutodistOrders";

// Automatic Distributors supplier data (owners and admins only). Licensed data: dealer cost and stock
// are for this screen only and must never be shown on the public store.

type Counts = { total: number; active: number; in_stock: number; brands: number; last_seen: string | null };
type Run = {
  id: number;
  source: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  rows_seen: number;
  rows_new: number;
  rows_skipped: number;
  rows_deactivated: number;
  note: string | null;
  created_by: string | null;
};
type Match = {
  total_products: number;
  counts: { upc: number; upc_many: number; mpn: number; mpn_many: number; none: number };
  review: { id: number; sku: string; title: string; supplier_sku: string; manufacturer: string | null; supplier_name: string | null; dealer_cost: string | null }[];
  review_limited: boolean;
};
type FtpStatus = { configured: boolean; host: string; user: string | null; inventory_file: string | null; tls: boolean };
type FtpCheck = {
  top: { name: string; dir: boolean | null; size: number | null }[];
  price_files: { name: string; size: number | null }[];
  inventory_file: string | null;
  login_folder?: string | null;
  raw_listing?: string[];
  tree?: { path: string; dir: boolean; size: number | null; image: boolean }[];
  image_files?: number;
  error?: string | null;
  columns?: string[];
  mapped?: Record<string, string>;
  unmapped?: string[];
  sample?: string[][];
  note?: string;
};
type ApiStatus = { configured: boolean; server: string; mode: "live" | "test" };
type ApiAvail = {
  server: string;
  mode: "live" | "test";
  number: string;
  quantity: number;
  image: string | null;
  warehouses: { warehouse: string; quantity: number | null; price: number | null; extended_price: number | null }[];
  total: number;
};
type Peek = { files: { path: string; columns: string[] | null; sample: string[]; error: string | null }[] };
type Schedule = {
  auto_enabled: boolean;
  interval_minutes: number;
  last_checked_at: string | null;
  last_changed_at: string | null;
  last_summary: string | null;
  last_error: string | null;
  running: boolean;
};
type Finish = { ok: boolean; needs_review: boolean; missing?: number; held?: number; stored: number; new: number; skipped?: number; retired?: number };

const MINUTES = [1, 2, 5, 10, 15, 30, 60, 120, 360, 1440];
const minLabel = (m: number) => (m < 60 ? `${m} minute${m === 1 ? "" : "s"}` : m === 60 ? "1 hour" : m === 1440 ? "24 hours" : `${m / 60} hours`);
function ago(iso: string | null) {
  if (!iso) return "never";
  const sec = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (sec < 90) return "just now";
  if (sec < 5400) return `${Math.round(sec / 60)} min ago`;
  if (sec < 172800) return `${Math.round(sec / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}
const PIECE = 5000; // data lines per request
const n = (x: number) => x.toLocaleString("en-US");
const money = (v: string | null) => (v == null ? "—" : `$${Number(v).toFixed(2)}`);
const tone = (s: string) => (s === "done" ? "green" : s === "running" ? "blue" : "amber");

type Tab = "catalog" | "opps" | "orders" | "data" | "match";
const TABS: { key: Tab; label: string }[] = [
  { key: "catalog", label: "Catalog" },
  { key: "opps", label: "Opportunities" },
  { key: "orders", label: "Orders" },
  { key: "data", label: "Data feed" },
  { key: "match", label: "Match report" },
];
const SOURCE_LABEL: Record<string, string> = {
  csv_upload: "file upload",
  ftp: "FTP, by hand",
  ftp_auto: "FTP, automatic",
  ftp_price: "prices",
  ftp_photos: "photo links",
};

/** One line describing an import, used by the log and by its search box. */
function runLabel(r: Run) {
  const base =
    r.source === "ftp_price"
      ? `Our prices: ${n(r.rows_seen)} lines, ${n(r.rows_new)} matched a part`
      : r.source === "ftp_photos"
      ? `Photo links: ${n(r.rows_seen)} lines read, ${n(r.rows_new)} parts updated`
      : `${n(r.rows_seen)} parts, ${n(r.rows_new)} new`;
  return base + (r.rows_skipped ? `, ${n(r.rows_skipped)} skipped` : "") + (r.rows_deactivated ? `, ${n(r.rows_deactivated)} retired` : "");
}

function AutodistInner() {
  const [counts, setCounts] = useState<Counts | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [msg, setMsg] = useState<{ kind: "error" | "success" | "info"; text: string } | null>(null);
  const [progress, setProgress] = useState("");
  const [busy, setBusy] = useState("");
  const [pending, setPending] = useState<{ runId: number; rowsSent: number; missing: number; held: number } | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [match, setMatch] = useState<Match | null>(null);
  const [ftp, setFtp] = useState<FtpStatus | null>(null);
  const [check, setCheck] = useState<FtpCheck | null>(null);
  const [sched, setSched] = useState<Schedule | null>(null);
  const [peek, setPeek] = useState<Peek | null>(null);
  const [tab, setTab] = useState<Tab>("catalog");
  const [opened, setOpened] = useState<Record<string, boolean>>({ catalog: true });
  const [feedOpen, setFeedOpen] = useState(true);
  const [feedTab, setFeedTab] = useState<"connection" | "api" | "log">("connection");
  const [logQ, setLogQ] = useState("");
  const [apiStatus, setApiStatus] = useState<ApiStatus | null>(null);
  const [apiNumber, setApiNumber] = useState("");
  const [apiRes, setApiRes] = useState<ApiAvail | null>(null);
  const [apiErr, setApiErr] = useState<string | null>(null);
  const [findReq, setFindReq] = useState<{ q: string; n: number } | null>(null);

  function pick(k: Tab) {
    setTab(k);
    setOpened((o) => (o[k] ? o : { ...o, [k]: true }));
  }
  function findInCatalog(sku: string) {
    setFindReq({ q: sku, n: Date.now() });
    pick("catalog");
  }
  const logWords = logQ.toLowerCase().split(/\s+/).filter(Boolean);
  const logRows = runs.filter((r) => {
    if (logWords.length === 0) return true;
    const hay = `${new Date(r.started_at).toLocaleString()} ${r.status} ${SOURCE_LABEL[r.source] ?? r.source} ${runLabel(r)} ${r.note ?? ""} ${r.created_by ?? ""}`.toLowerCase();
    return logWords.every((w) => hay.includes(w));
  });

  async function refresh() {
    try {
      const r = await api<{ counts: Counts; runs: Run[] }>("/autodist/summary");
      setCounts(r.counts);
      setRuns(r.runs);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    }
  }

  useEffect(() => {
    void refresh();
    api<FtpStatus>("/autodist/ftp/status").then(setFtp).catch(() => setFtp(null));
    api<ApiStatus>("/autodist/api/status").then(setApiStatus).catch(() => setApiStatus(null));
    void loadSched();
    const t = setInterval(() => void loadSched(), 30000);
    return () => clearInterval(t);
  }, []);

  async function loadSched() {
    try {
      setSched(await api<Schedule>("/autodist/ftp/schedule"));
    } catch {
      setSched(null); // the settings table is not there yet; the manual button still works
    }
  }

  async function saveSched(body: { enabled?: boolean; interval_minutes?: number }) {
    setBusy("sched");
    setMsg(null);
    try {
      setSched(await api<Schedule>("/autodist/ftp/schedule", { method: "PUT", body }));
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  // While an FTP import runs in the background, keep the list fresh.
  const ftpRunning = runs.some((r) => r.source.startsWith("ftp") && r.status === "running");
  useEffect(() => {
    if (!ftpRunning) return;
    const t = setInterval(() => void refresh(), 4000);
    return () => clearInterval(t);
  }, [ftpRunning]);

  // An FTP import that stopped for review: offer the same confirm button as a file upload.
  const ftpReview = runs.find((r) => r.source.startsWith("ftp") && r.status === "needs_review" && r.id === runs[0]?.id);

  async function ftpSync() {
    setBusy("ftp");
    setMsg(null);
    setPending(null);
    try {
      await api("/autodist/import/ftp", { method: "POST" });
      setMsg({ kind: "info", text: "Started. The import runs on the server and appears in the list below; this page updates by itself." });
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
      void refresh();
    }
  }

  async function apiTest() {
    setBusy("api");
    setApiErr(null);
    setApiRes(null);
    try {
      const q = new URLSearchParams({ quantity: "1" });
      if (apiNumber.trim()) q.set("number", apiNumber.trim());
      setApiRes(await api<ApiAvail>(`/autodist/api/avail?${q.toString()}`, { method: "POST" }));
    } catch (e) {
      setApiErr(errMsg(e));
    } finally {
      setBusy("");
    }
  }

  async function photoSync() {
    setBusy("photos");
    setMsg(null);
    try {
      await api("/autodist/import/ftp-photos", { method: "POST" });
      setMsg({ kind: "info", text: "Started. It reads Automatic Distributors' 140 MB product file, so it takes several minutes. Progress shows in the list below." });
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
      void refresh();
    }
  }

  async function ftpPeek() {
    setBusy("peek");
    setMsg(null);
    setPeek(null);
    try {
      setPeek(await api<Peek>("/autodist/ftp/peek", { method: "POST" }));
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  async function ftpTest() {
    setBusy("ftpcheck");
    setMsg(null);
    setCheck(null);
    try {
      setCheck(await api<FtpCheck>("/autodist/ftp/check", { method: "POST" }));
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  async function importFile(file: File | null) {
    if (!file) return;
    setBusy("import");
    setMsg(null);
    setPending(null);
    try {
      setProgress("Reading the file…");
      const text = (await file.text()).replace(/^\uFEFF/, "");
      const lines = text.split(/\r?\n/).filter((l) => l.trim() !== "");
      if (lines.length < 2) throw new Error("The file is empty.");
      const header = lines[0];
      const data = lines.slice(1);
      const { run_id } = await api<{ run_id: number }>("/autodist/import/start", { method: "POST", body: { source: "csv_upload" } });
      for (let i = 0; i < data.length; i += PIECE) {
        setProgress(`Storing parts ${n(i + 1)} to ${n(Math.min(i + PIECE, data.length))} of ${n(data.length)}…`);
        await api("/autodist/import/chunk", { method: "POST", body: { run_id, csv: header + "\r\n" + data.slice(i, i + PIECE).join("\r\n") } });
      }
      setProgress("Checking the whole file arrived…");
      await finish(run_id, data.length, false);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
      setProgress("");
      if (fileRef.current) fileRef.current.value = "";
      void refresh();
    }
  }

  async function finish(runId: number, rowsSent: number, force: boolean) {
    const r = await api<Finish>("/autodist/import/finish", { method: "POST", body: { run_id: runId, rows_sent: rowsSent, force } });
    if (r.needs_review) {
      setPending({ runId, rowsSent, missing: r.missing ?? 0, held: r.held ?? 0 });
      setMsg({
        kind: "error",
        text: `Stored ${n(r.stored)} parts, but ${n(r.missing ?? 0)} of the ${n(r.held ?? 0)} parts already held are not in this file. Nothing was retired yet.`,
      });
      return;
    }
    setPending(null);
    setMsg({
      kind: "success",
      text: `Done. ${n(r.stored)} parts stored (${n(r.new)} new), ${n(r.retired ?? 0)} no longer in the file${r.skipped ? `, ${n(r.skipped)} unreadable lines skipped` : ""}.`,
    });
  }

  async function confirmRetire() {
    if (!pending) return;
    setBusy("import");
    try {
      await finish(pending.runId, pending.rowsSent, true);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
      void refresh();
    }
  }

  async function runMatch() {
    setBusy("match");
    setMsg(null);
    try {
      setMatch(await api<Match>("/autodist/match-report"));
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="space-y-5">
      <div>
        <Link href="/admin/distributors" className="text-sm text-gray-600 underline">
          ← All distributors
        </Link>
        <h1 className="mt-1 text-2xl font-semibold">Automatic Distributors</h1>
        <p className="mt-1 text-sm text-gray-600">
          Supplier parts, cost and warehouse stock. This is licensed data: it stays on this screen and is never shown on the public store.
        </p>
      </div>

      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}
      {!pending && ftpReview && (
        <div className="flex flex-wrap items-center gap-3 rounded border border-amber-300 bg-amber-50 p-3 text-sm">
          <span>The FTP file is much smaller than what is stored ({ftpReview.note}). Nothing was retired. If Automatic Distributors really dropped these parts, confirm.</span>
          <button
            type="button"
            className={btnCls}
            disabled={busy !== ""}
            onClick={async () => {
              setBusy("import");
              try {
                await finish(ftpReview.id, ftpReview.rows_seen + ftpReview.rows_skipped, true);
              } catch (e) {
                setMsg({ kind: "error", text: errMsg(e) });
              } finally {
                setBusy("");
                void refresh();
              }
            }}
          >
            Retire the missing parts
          </button>
        </div>
      )}
      {pending && (
        <div className="flex flex-wrap items-center gap-3 rounded border border-amber-300 bg-amber-50 p-3 text-sm">
          <span>
            That file is much smaller than what is stored. If it is the wrong file, leave it: nothing was retired. If Automatic Distributors really dropped these parts, confirm.
          </span>
          <button type="button" className={btnCls} disabled={busy !== ""} onClick={() => void confirmRetire()}>
            Retire the {n(pending.missing)} missing parts
          </button>
        </div>
      )}

      {counts && (
        <p className="text-sm text-gray-600">
          <b className="text-gray-900">{n(counts.active)}</b> parts · <b className="text-gray-900">{n(counts.in_stock)}</b> with stock at Automatic Distributors · <b className="text-gray-900">{n(counts.brands)}</b>{" "}
          brands · last update {counts.last_seen ? new Date(counts.last_seen).toLocaleString() : "never"}
        </p>
      )}

      <div className="flex flex-wrap gap-1 border-b border-gray-200">
        {TABS.map((x) => (
          <button
            key={x.key}
            type="button"
            onClick={() => pick(x.key)}
            className={`-mb-px rounded-t px-4 py-2 text-sm ${tab === x.key ? "border border-gray-200 border-b-white bg-white font-medium" : "text-gray-600 hover:text-gray-900"}`}
          >
            {x.label}
          </button>
        ))}
      </div>

      <div className={tab === "catalog" ? "" : "hidden"}>
        <AutodistCatalog hasData={!!counts?.active} search={findReq} />
      </div>

      {opened.opps && (
        <div className={tab === "opps" ? "" : "hidden"}>
          <AutodistOpportunities hasData={!!counts?.active} onFind={findInCatalog} />
        </div>
      )}

      {opened.orders && (
        <div className={tab === "orders" ? "" : "hidden"}>
          <AutodistOrders status={apiStatus} />
        </div>
      )}

      {tab === "data" && (
        <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-3 rounded border border-gray-200 bg-white p-4 text-sm">
          <input ref={fileRef} type="file" accept=".csv,text/csv" className="hidden" onChange={(e) => void importFile(e.target.files?.[0] ?? null)} />
          <button type="button" className={btnCls} disabled={busy !== ""} onClick={() => fileRef.current?.click()}>
            {busy === "import" ? "Importing…" : "Import Inventory Feed (CSV)"}
          </button>
          <span className="text-xs text-gray-500">{progress || "Choose the Inventory Feed CSV from the Automatic Distributors dealer site. Takes a minute or two."}</span>
        </div>

          <div className="rounded border border-gray-200 bg-white text-sm">
            <button
              type="button"
              className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left"
              onClick={() => setFeedOpen((o) => !o)}
              aria-expanded={feedOpen}
            >
              <span className="font-semibold">
                <span className="mr-2 inline-block w-3 text-gray-500">{feedOpen ? "▾" : "▸"}</span>Connection &amp; FTP
              </span>
              <span className="flex items-center gap-2 text-xs text-gray-500">
                {sched?.auto_enabled ? <Badge tone="green">auto sync on</Badge> : <Badge tone="gray">auto sync off</Badge>}
                {ftpRunning && <Badge tone="blue">importing…</Badge>}
              </span>
            </button>
            {feedOpen && (
              <div className="border-t border-gray-100 p-4">
                <div className="mb-3 flex gap-1 border-b border-gray-200">
                  {(["connection", "api", "log"] as const).map((k) => (
                    <button
                      key={k}
                      type="button"
                      onClick={() => setFeedTab(k)}
                      className={`-mb-px rounded-t px-3 py-1.5 text-sm ${feedTab === k ? "border border-gray-200 border-b-white bg-white font-medium" : "text-gray-600 hover:text-gray-900"}`}
                    >
                      {k === "connection" ? "Connection" : k === "api" ? "Ordering API" : `Log (${runs.length})`}
                    </button>
                  ))}
                </div>
                {feedTab === "connection" && (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-3">
            <button type="button" className={btnCls} disabled={busy !== "" || ftpRunning || !ftp?.configured} onClick={() => void ftpSync()}>
              {ftpRunning ? "FTP import running…" : busy === "ftp" ? "Starting…" : "Sync from Automatic Distributors FTP"}
            </button>
            <button type="button" className={btn2Cls} disabled={busy !== "" || !ftp?.configured} onClick={() => void ftpTest()}>
              {busy === "ftpcheck" ? "Connecting…" : "Test FTP connection"}
            </button>
            <button type="button" className={btn2Cls} disabled={busy !== "" || ftpRunning || !ftp?.configured} onClick={() => void photoSync()} title="Reads the big All Products file. Only needed now and then.">
              {busy === "photos" ? "Starting…" : "Update photo links"}
            </button>
            <button type="button" className={btn2Cls} disabled={busy !== "" || !ftp?.configured} onClick={() => void ftpPeek()}>
              {busy === "peek" ? "Reading files…" : "Look inside the data files"}
            </button>
            <span className="text-xs text-gray-500">
              {ftp == null
                ? "…"
                : ftp.configured
                ? `Logging in to ${ftp.host} as ${ftp.user}. Pulls the "all products price and inventory" file, then your own prices (priceupdate.txt).`
                : "Not set up yet: add AUTODIST_FTP_USER and AUTODIST_FTP_PASSWORD in the Render environment, then redeploy."}
            </span>
          </div>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={!!sched?.auto_enabled}
                disabled={!sched || !ftp?.configured || busy === "sched"}
                onChange={(e) => void saveSched({ enabled: e.target.checked })}
              />
              <span>Check Automatic Distributors for changes every</span>
            </label>
            <select
              className={`${inputCls} !w-auto`}
              value={sched?.interval_minutes ?? 15}
              disabled={!sched || busy === "sched"}
              onChange={(e) => void saveSched({ interval_minutes: Number(e.target.value) })}
            >
              {sched && !MINUTES.includes(sched.interval_minutes) && <option value={sched.interval_minutes}>{minLabel(sched.interval_minutes)}</option>}
              {MINUTES.map((m) => (
                <option key={m} value={m}>
                  {minLabel(m)}
                </option>
              ))}
            </select>
            {sched?.running && <Badge tone="blue">Importing now…</Badge>}
          </div>
          {sched ? (
            <div className="text-xs text-gray-500">
              {sched.auto_enabled ? "Automatic sync is ON." : "Automatic sync is OFF."} Each check only asks the FTP server for the file’s date and size; it downloads and imports only when Automatic Distributors has
              published a newer file. Last checked {ago(sched.last_checked_at)}
              {sched.last_changed_at ? ` · last import ${ago(sched.last_changed_at)}` : ""}
              {sched.last_summary ? ` · ${sched.last_summary}` : ""}
              {" "}While the server is asleep nothing is checked; the next check after it wakes catches up.
            </div>
          ) : (
            <div className="text-xs text-amber-700">Automatic sync needs the new settings table: run sql/autodist_sync_settings.sql in Supabase, then reload.</div>
          )}
          {sched?.last_error && <div className="text-xs text-red-700">Last problem: {sched.last_error}</div>}
          {peek && (
            <div className="space-y-3 rounded bg-gray-50 p-3 text-xs">
              {peek.files.map((f) => (
                <div key={f.path}>
                  <div className="font-medium">{f.path}</div>
                  {f.error ? (
                    <div className="text-red-700">{f.error}</div>
                  ) : (
                    <>
                      <div className="text-gray-700">{f.columns?.map((c, i) => `${i + 1}. ${c}`).join("   ")}</div>
                      {f.sample.map((r, i) => (
                        <div key={i} className="overflow-x-auto whitespace-pre text-gray-500">
                          {r}
                        </div>
                      ))}
                    </>
                  )}
                </div>
              ))}
            </div>
          )}
          {check && (
            <div className="space-y-1 rounded bg-gray-50 p-3 text-xs">
              {check.error && <div className="text-red-700">{check.error}</div>}
              {check.inventory_file && (
                <div>
                  Found inventory file: <b>{check.inventory_file}</b>
                  {check.note ? ` (${check.note})` : ""}
                </div>
              )}
              {check.login_folder && <div className="text-gray-500">Logged in at folder: {check.login_folder}</div>}
              {check.mapped && (
                <div>
                  Columns read: {Object.entries(check.mapped).map(([k, v]) => `${k} ← ${v}`).join(", ")}
                  {check.unmapped && check.unmapped.length > 0 && <span className="text-gray-500"> · not used: {check.unmapped.join(", ")}</span>}
                </div>
              )}
              {check.sample && check.sample.length > 0 && <div className="overflow-x-auto whitespace-pre text-gray-600">{check.sample.map((r) => r.join(" | ")).join("\n")}</div>}
              <div className="text-gray-500">
                Top folder: {check.top.map((t) => t.name + (t.dir ? "/" : "")).join(", ") || "(empty)"}
                {check.price_files.length > 0 && ` · pricefile folder: ${check.price_files.map((f) => f.name).join(", ")}`}
              </div>
              {check.tree && check.tree.length > 0 && (
                <details>
                  <summary className="cursor-pointer text-gray-500">
                    Everything on the server ({check.tree.length}
                    {check.tree.length >= 400 ? "+" : ""} items, {check.image_files ?? 0} image files)
                  </summary>
                  <div className="overflow-x-auto whitespace-pre text-gray-600">
                    {check.tree.map((e) => `${e.dir ? "📁 " : e.image ? "🖼 " : "   "}${e.path}${e.size != null && !e.dir ? `  (${n(e.size)} bytes)` : ""}`).join("\n")}
                  </div>
                </details>
              )}
              {check.raw_listing && check.raw_listing.length > 0 && (
                <details>
                  <summary className="cursor-pointer text-gray-500">What the server sent</summary>
                  <div className="overflow-x-auto whitespace-pre text-gray-600">{check.raw_listing.join("\n")}</div>
                </details>
              )}
            </div>
          )}
        </div>

                )}
                {feedTab === "api" && (
                  <div className="space-y-3 text-sm">
                    <div className="flex flex-wrap items-center gap-2 text-xs text-gray-600">
                      {apiStatus == null ? (
                        "…"
                      ) : (
                        <>
                          <Badge tone={apiStatus.mode === "test" ? "amber" : "blue"}>{apiStatus.mode === "test" ? "TEST server" : "LIVE server"}</Badge>
                          <span>{apiStatus.server}</span>
                          {!apiStatus.configured && (
                            <span className="text-amber-700">The key is not set: add AUTOMATIC_DIST_API_KEY in the Render environment, then redeploy.</span>
                          )}
                        </>
                      )}
                    </div>
                    <p className="text-xs text-gray-500">
                      The key stays on the server. Which server is used is set by AUTOMATIC_DIST_API_URL in Render; leave it empty for the live server, or use the test address while we try ordering.
                    </p>
                    <div className="flex flex-wrap items-center gap-3">
                      <input
                        className={`${inputCls} !w-56`}
                        placeholder="SKU (blank = any stored part)"
                        value={apiNumber}
                        onChange={(e) => setApiNumber(e.target.value)}
                        onKeyDown={(e) => e.key === "Enter" && !busy && apiStatus?.configured && void apiTest()}
                      />
                      <button type="button" className={btnCls} disabled={busy !== "" || !apiStatus?.configured} onClick={() => void apiTest()}>
                        {busy === "api" ? "Asking…" : "Test API connection"}
                      </button>
                    </div>
                    {apiErr && <Notice kind="error">{apiErr}</Notice>}
                    {apiRes && (
                      <div className="space-y-1 rounded bg-gray-50 p-3 text-xs">
                        <div>
                          <b>{apiRes.number}</b> · works on the {apiRes.mode === "test" ? "test" : "live"} server · {n(apiRes.total)} in stock right now
                        </div>
                        <table className="text-left">
                          <thead className="text-gray-500">
                            <tr>
                              <th className="py-0.5 pr-4">Warehouse</th>
                              <th className="py-0.5 pr-4 text-right">Quantity</th>
                              <th className="py-0.5 text-right">Our price</th>
                            </tr>
                          </thead>
                          <tbody>
                            {apiRes.warehouses.map((w) => (
                              <tr key={w.warehouse}>
                                <td className="py-0.5 pr-4">{w.warehouse}</td>
                                <td className="py-0.5 pr-4 text-right">{n(w.quantity ?? 0)}</td>
                                <td className="py-0.5 text-right">{money(w.price == null ? null : String(w.price))}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>
                )}
                {feedTab === "log" && (
                  <div className="space-y-2">
                    <div className="flex flex-wrap items-center gap-3">
                      <input
                        className={`${inputCls} !w-72`}
                        placeholder="Search the log (date, status, text…)"
                        value={logQ}
                        onChange={(e) => setLogQ(e.target.value)}
                      />
                      <span className="text-xs text-gray-500">
                        {logRows.length} of {runs.length} entries{runs.length >= 300 ? " (the latest 300)" : ""}
                      </span>
                    </div>
                    <ul className="max-h-[28rem] divide-y divide-gray-100 overflow-y-auto rounded border border-gray-100 text-xs">
                      {logRows.map((r) => (
                        <li key={r.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-2 py-1.5">
                          <span className="text-gray-500">{new Date(r.started_at).toLocaleString()}</span>
                          <Badge tone={tone(r.status)}>{r.status === "needs_review" ? "needs review" : r.status}</Badge>
                          <span className="text-gray-500">{SOURCE_LABEL[r.source] ?? r.source}</span>
                          <span>{runLabel(r)}</span>
                          {r.created_by && <span className="text-gray-400">{r.created_by}</span>}
                          {r.note && <span className="text-red-700">{r.note}</span>}
                        </li>
                      ))}
                      {logRows.length === 0 && <li className="px-2 py-4 text-center text-gray-500">{runs.length === 0 ? "Nothing has been imported yet." : "No entries match."}</li>}
                    </ul>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      )}

      {tab === "match" && (
      <div className="space-y-3 rounded border border-gray-200 bg-white p-4 text-sm">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="font-semibold">Which of our products does Automatic Distributors carry?</h2>
          <button type="button" className={btn2Cls} disabled={busy !== "" || !counts?.active} onClick={() => void runMatch()}>
            {busy === "match" ? "Checking…" : "Run match report"}
          </button>
        </div>
        <p className="text-xs text-gray-500">Read only. Nothing is linked or changed. A barcode match is exact; a part-number match can be a different brand, so those are listed to check.</p>
        {match && (
          <>
            <div className="flex flex-wrap gap-x-8 gap-y-1">
              <span><b>{n(match.counts.upc)}</b> match by barcode</span>
              <span><b>{n(match.counts.mpn)}</b> by part number only</span>
              <span><b>{n(match.counts.mpn_many)}</b> part number fits several</span>
              <span><b>{n(match.counts.none)}</b> no match</span>
              <span className="text-gray-500">of {n(match.total_products)} products</span>
            </div>
            {match.review.length > 0 && (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead className="uppercase text-gray-500">
                    <tr>
                      <th className="py-1 pr-3">Our part</th>
                      <th className="py-1 pr-3">Automatic Distributors part</th>
                      <th className="py-1">Cost</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100">
                    {match.review.map((r, i) => (
                      <tr key={`${r.id}-${r.supplier_sku}-${i}`} className="align-top">
                        <td className="py-1 pr-3">
                          <span className="font-medium">{r.sku}</span> {r.title}
                        </td>
                        <td className="py-1 pr-3">
                          <span className="font-medium">{r.manufacturer}</span> {r.supplier_sku} {r.supplier_name}
                        </td>
                        <td className="py-1">{money(r.dealer_cost)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {match.review_limited && <p className="mt-1 text-xs text-gray-500">Showing the first 300 products that need checking.</p>}
              </div>
            )}
          </>
        )}
      </div>
      )}


    </div>
  );
}

// Dealer cost and stock are licensed data: owners and admins only.
export default function AutodistPage() {
  const { isAdmin } = useAccess();
  if (!isAdmin) return <Notice kind="error">Your account doesn’t have access to this page.</Notice>;
  return <AutodistInner />;
}
