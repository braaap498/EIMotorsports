"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Badge, Notice, btnCls, btn2Cls, inputCls } from "./ui";

// Rocky Mountain's SFTP feed: the same tools the Automatic Distributors page has for its FTP (test the login, look at the files,
// sync, prices, photo links, automatic sync), plus a "which file is which" step because Rocky Mountain's file names and columns
// are not known yet. The login itself lives in the server's environment, never here.

type Run = { id: number; source: string; status: string; rows_seen: number; rows_skipped: number; note: string | null };
type Status = { configured: boolean; host: string; port: number; user: string | null; host_key_pinned: boolean };
type Config = {
  catalog_file?: string | null; oem_file?: string | null; price_file?: string | null; photo_file?: string | null;
  catalog_map?: Record<string, string> | null; oem_map?: Record<string, string> | null; price_map?: Record<string, string> | null; photo_map?: Record<string, string> | null;
};
type TreeItem = { path: string; dir: boolean; size: number | null; mtime: number | null; image: boolean; more: boolean };
type Check = { fingerprint: string; login_folder: string | null; tree: TreeItem[]; image_files: number; data_files: string[]; other_files: string[]; config: Config };
type PeekFile = { path: string; columns: string[] | null; sample: string[]; error: string | null; note: string | null; role: string | null; size: number | null; mtime: number | null };
type Schedule = { auto_enabled: boolean; interval_minutes: number; last_checked_at: string | null; last_changed_at: string | null; last_summary: string | null; last_error: string | null; running: boolean };
type Msg = { kind: "error" | "success" | "info"; text: string } | null;

const ROLES = [
  { key: "catalog_file", map: "catalog_map", label: "Catalog (parts, your cost, stock)", short: "catalog" },
  { key: "oem_file", map: "oem_map", label: "Second catalog: OEM parts (optional; adds the parts the first catalog lacks)", short: "OEM catalog" },
  { key: "price_file", map: "price_map", label: "Your prices (optional)", short: "prices" },
  { key: "photo_file", map: "photo_map", label: "Photo links (optional; else the catalog file)", short: "photo links" },
] as const;
const CATALOG_FIELDS: [string, string][] = [["sku", "SKU"], ["name", "Name"], ["manufacturer", "Brand"], ["mfr_number", "Mfr #"], ["dealer_cost", "Your cost"], ["list_price", "List price (MSRP)"], ["map_price", "MAP price (used as list price when present)"], ["qty_ut", "Stock in Utah"], ["qty_ky", "Stock in Kentucky"], ["qty", "Total stock (only if no Utah / Kentucky columns)"], ["upc", "Barcode"], ["unit_of_measure", "Unit"], ["images", "Photo link(s)"], ["fitment", "Fitment"]];
const FIELDS: Record<string, [string, string][]> = {
  catalog_map: CATALOG_FIELDS,
  oem_map: CATALOG_FIELDS,
  price_map: [["sku", "SKU"], ["my_cost", "Your cost"], ["map_price", "MAP"], ["list_price", "List price"], ["weight_lb", "Weight (lb)"], ["length_in", "Length"], ["width_in", "Width"], ["height_in", "Height"]],
  photo_map: [["sku", "SKU"], ["images", "All links in one column"], ["image_1", "Link 1"], ["image_2", "Link 2"], ["image_3", "Link 3"], ["image_4", "Link 4"], ["image_5", "Link 5"], ["image_6", "Link 6"]],
};
const SOURCE: Record<string, string> = { sftp: "SFTP, by hand", sftp_auto: "SFTP, automatic", sftp_price: "prices", sftp_photos: "photo links" };
const bytes = (v: number | null) => (v == null ? "" : v > 1048576 ? `${(v / 1048576).toFixed(1)} MB` : v > 1024 ? `${Math.round(v / 1024)} KB` : `${v} B`);
const when = (t: number | null) => (t ? new Date(t * 1000).toLocaleString() : "");

export default function RmatvSftp({ runs, onChanged }: { runs: Run[]; onChanged: () => void }) {
  const [open, setOpen] = useState(true);
  const [status, setStatus] = useState<Status | null>(null);
  const [cfg, setCfg] = useState<Config>({});
  const [check, setCheck] = useState<Check | null>(null);
  const [peek, setPeek] = useState<PeekFile[] | null>(null);
  const [sched, setSched] = useState<Schedule | null>(null);
  const [cols, setCols] = useState<Record<string, string[]>>({}); // file path -> its column names
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<Msg>(null);
  const [saved, setSaved] = useState(true);

  const sftpRunning = runs.some((r) => r.source.startsWith("sftp") && r.status === "running");
  const review = runs.find((r) => r.source.startsWith("sftp") && r.status === "needs_review" && r.id === runs[0]?.id);

  const loadAll = useCallback(() => {
    api<Status>("/rmatv/sftp/status").then(setStatus).catch(() => setStatus(null));
    api<Config>("/rmatv/sftp/config").then((c) => { setCfg(c); setSaved(true); }).catch(() => {});
    api<Schedule>("/rmatv/sftp/schedule").then(setSched).catch(() => setSched(null));
  }, []);
  useEffect(loadAll, [loadAll]);

  // while an import runs in the background, keep the log and the schedule fresh
  useEffect(() => {
    if (!sftpRunning) return;
    const t = setInterval(() => { onChanged(); loadAll(); }, 4000);
    return () => clearInterval(t);
  }, [sftpRunning, onChanged, loadAll]);

  async function act<T>(name: string, fn: () => Promise<T>, ok?: (r: T) => Msg) {
    setBusy(name);
    setMsg(null);
    try {
      const r = await fn();
      if (ok) setMsg(ok(r));
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
      onChanged();
    }
  }

  const test = () => act("check", async () => { const r = await api<Check>("/rmatv/sftp/check", { method: "POST" }); setCheck(r); return r; },
    (r) => ({ kind: "success", text: `Logged in. ${r.data_files.length} data file${r.data_files.length === 1 ? "" : "s"}, ${r.image_files} image file${r.image_files === 1 ? "" : "s"} found. Server key ${r.fingerprint}` }));

  const lookAtFiles = () => act("peek", async () => {
    const r = await api<{ files: PeekFile[] }>("/rmatv/sftp/peek", { method: "POST", body: { paths: [] } });
    setPeek(r.files);
    setCols((c) => ({ ...c, ...Object.fromEntries(r.files.filter((f) => f.columns).map((f) => [f.path, f.columns as string[]])) }));
  });

  async function loadColumns(path: string) {
    if (!path || cols[path]) return;
    try {
      const r = await api<{ files: PeekFile[] }>("/rmatv/sftp/peek", { method: "POST", body: { paths: [path] } });
      const f = r.files[0];
      if (f?.columns) setCols((c) => ({ ...c, [path]: f.columns as string[] }));
    } catch { /* the column choices just stay empty */ }
  }

  function setFile(key: (typeof ROLES)[number]["key"], path: string) {
    setCfg((c) => ({ ...c, [key]: path || null }));
    setSaved(false);
    void loadColumns(path);
  }
  function setMapping(mapKey: string, field: string, header: string) {
    setCfg((c) => ({ ...c, [mapKey]: { ...((c as Record<string, Record<string, string> | null | undefined>)[mapKey] ?? {}), [field]: header } }));
    setSaved(false);
  }
  const saveCfg = () => act("save", async () => {
    const r = await api<Config>("/rmatv/sftp/config", { method: "PUT", body: {
      catalog_file: cfg.catalog_file ?? "", oem_file: cfg.oem_file ?? "", price_file: cfg.price_file ?? "", photo_file: cfg.photo_file ?? "",
      catalog_map: cfg.catalog_map ?? {}, oem_map: cfg.oem_map ?? {}, price_map: cfg.price_map ?? {}, photo_map: cfg.photo_map ?? {},
    } });
    setCfg(r);
    setSaved(true);
    return r;
  }, () => ({ kind: "success", text: "Saved." }));

  const start = (name: string, path: string, text: string) => act(name, async () => api(path, { method: "POST" }), () => ({ kind: "info", text }));
  const changeSched = (body: { enabled?: boolean; interval_minutes?: number }) => act("sched", async () => setSched(await api<Schedule>("/rmatv/sftp/schedule", { method: "PUT", body })));

  const dataFiles = Array.from(new Set([...(check?.data_files ?? []), ...(peek ?? []).map((p) => p.path), ...ROLES.map((r) => cfg[r.key]).filter(Boolean) as string[]]));
  const off = busy !== "" || sftpRunning;

  return (
    <div className="rounded border border-gray-200 bg-white p-4 text-sm">
      <button type="button" className="flex w-full items-center gap-2 text-left font-semibold" onClick={() => setOpen((o) => !o)}>
        <span className="inline-block w-3 text-gray-500">{open ? "▾" : "▸"}</span>SFTP feed (Rocky Mountain)
        {sftpRunning && <Badge tone="blue">importing…</Badge>}
        {sched?.auto_enabled && <Badge tone="green">auto sync on</Badge>}
        {status && !status.configured && <Badge tone="amber">login not set</Badge>}
      </button>

      {open && (
        <div className="mt-3 space-y-4">
          <p className="text-xs text-gray-500">
            {status == null
              ? "Checking…"
              : status.configured
              ? `Logs in to ${status.host}:${status.port} as ${status.user}${status.host_key_pinned ? " (server key pinned)" : ""}. The password stays on the server.`
              : "Not set up yet: add RMATV_SFTP_USER and RMATV_SFTP_PASSWORD in the server environment (and add paramiko to requirements.txt), then redeploy."}
          </p>

          {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}
          {review && (
            <div className="flex flex-wrap items-center gap-3 rounded border border-amber-300 bg-amber-50 p-3">
              <span>The SFTP file is much smaller than what is stored ({review.note}). Nothing was retired. If Rocky Mountain really dropped these parts, confirm.</span>
              <button type="button" className={btnCls} disabled={busy !== ""} onClick={() => void act("retire", async () => api("/rmatv/import/finish", { method: "POST", body: { run_id: review.id, rows_sent: review.rows_seen + review.rows_skipped, force: true } }))}>
                Retire the missing parts
              </button>
            </div>
          )}

          <div className="flex flex-wrap items-center gap-2">
            <button type="button" className={btnCls} disabled={off || !status?.configured || !cfg.catalog_file} onClick={() => void start("sync", "/rmatv/import/sftp", "Sync started. It runs in the background; watch the import log below.")}>
              {sftpRunning ? "SFTP import running…" : busy === "sync" ? "Starting…" : "Sync from Rocky Mountain SFTP"}
            </button>
            <button type="button" className={btn2Cls} disabled={off || !status?.configured} onClick={() => void test()}>{busy === "check" ? "Connecting…" : "Test SFTP connection"}</button>
            <button type="button" className={btn2Cls} disabled={off || !status?.configured} onClick={() => void lookAtFiles()}>{busy === "peek" ? "Reading…" : "Look at the files"}</button>
            <button type="button" className={btn2Cls} disabled={off || !status?.configured || !cfg.price_file} title="Needs a price file chosen below" onClick={() => void start("price", "/rmatv/import/sftp-prices", "Price update started.")}>Pull my prices</button>
            <button type="button" className={btn2Cls} disabled={off || !status?.configured || !(cfg.photo_file || cfg.catalog_file)} title="Reads the photo links. Only needed now and then." onClick={() => void start("photo", "/rmatv/import/sftp-photos", "Reading photo links in the background.")}>Read photo links</button>
          </div>

          {check && (
            <details open className="rounded border border-gray-100 p-2 text-xs">
              <summary className="cursor-pointer font-medium">On the server{check.login_folder ? ` (${check.login_folder})` : ""} — {check.tree.length} entries shown</summary>
              <ul className="mt-2 max-h-64 space-y-0.5 overflow-auto font-mono">
                {check.tree.map((t) => (
                  <li key={t.path} className={t.dir ? "font-semibold" : t.more ? "text-gray-400" : ""}>
                    {t.dir ? "📁 " : ""}{t.path}{t.size != null && !t.dir ? <span className="text-gray-400"> · {bytes(t.size)}{t.mtime ? ` · ${when(t.mtime)}` : ""}</span> : null}
                  </li>
                ))}
              </ul>
              {check.image_files > 0 && <p className="mt-1 text-gray-600">{check.image_files} picture files are on the server. They are not imported (photo links are); tell me their folder and how they are named and I can match them to parts.</p>}
            </details>
          )}

          {peek && (
            <details open className="rounded border border-gray-100 p-2 text-xs">
              <summary className="cursor-pointer font-medium">Data files and their columns ({peek.length})</summary>
              <div className="mt-2 space-y-3">
                {peek.map((f) => (
                  <div key={f.path} className="rounded border border-gray-100 p-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-mono font-medium">{f.path}</span>
                      <span className="text-gray-400">{bytes(f.size)}{f.mtime ? ` · ${when(f.mtime)}` : ""}</span>
                      {f.role && <Badge tone="blue">looks like {f.role === "price" ? "prices" : f.role === "photos" ? "photo links" : "the catalog"}</Badge>}
                      {f.columns && (
                        <span className="ml-auto flex flex-wrap gap-1">
                          {ROLES.map((r) => (
                            <button key={r.key} type="button" className="rounded border border-gray-300 px-1.5 hover:bg-gray-50" onClick={() => setFile(r.key, f.path)}>Use as {r.short}</button>
                          ))}
                        </span>
                      )}
                    </div>
                    {f.error && <p className="text-red-700">{f.error}</p>}
                    {f.note && <p className="text-gray-500">{f.note}</p>}
                    {f.columns && <p className="mt-1 text-gray-600">{f.columns.join(" · ")}</p>}
                    {f.sample.map((s, i) => <p key={i} className="truncate text-gray-400" title={s}>{s}</p>)}
                  </div>
                ))}
                {peek.length === 0 && <p className="text-gray-500">No data files (csv / txt / tsv / zip) were found in the top two folder levels.</p>}
              </div>
            </details>
          )}

          <div className="space-y-3 rounded border border-gray-200 p-3">
            <h3 className="font-medium">Which file is which</h3>
            <p className="text-xs text-gray-500">Run <b>Test SFTP connection</b> or <b>Look at the files</b> first, then pick them. A column the guess got wrong can be set by hand under each file; columns are matched by name, so a file whose columns move around still imports.</p>
            {ROLES.map((r) => {
              const path = (cfg[r.key] as string | null | undefined) ?? "";
              const header = cols[path];
              const m = ((cfg as Record<string, Record<string, string> | null | undefined>)[r.map] ?? {}) as Record<string, string>;
              return (
                <div key={r.key} className="space-y-1">
                  <label className="block">
                    <span className="mb-1 block text-xs text-gray-600">{r.label}</span>
                    <select className={inputCls} value={path} onChange={(e) => setFile(r.key, e.target.value)}>
                      <option value="">(none)</option>
                      {dataFiles.map((p) => <option key={p} value={p}>{p}</option>)}
                    </select>
                  </label>
                  {path && (
                    <details className="text-xs">
                      <summary className="cursor-pointer text-gray-600">Column choices{Object.keys(m).length ? ` (${Object.keys(m).length} set by hand)` : " (guessed from the headings)"}</summary>
                      {!header ? (
                        <button type="button" className="mt-1 underline" onClick={() => void loadColumns(path)}>Load this file&apos;s columns</button>
                      ) : (
                        <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                          {FIELDS[r.map].map(([field, label]) => (
                            <label key={field} className="block">
                              <span className="mb-0.5 block text-gray-500">{label}</span>
                              <select className={inputCls} value={m[field] ?? ""} onChange={(e) => setMapping(r.map, field, e.target.value)}>
                                <option value="">(guess)</option>
                                {header.map((h, i) => <option key={i} value={h}>{h || `Column ${i + 1}`}</option>)}
                              </select>
                            </label>
                          ))}
                        </div>
                      )}
                    </details>
                  )}
                </div>
              );
            })}
            <div className="flex items-center gap-2">
              <button type="button" className={btnCls} disabled={busy !== "" || saved} onClick={() => void saveCfg()}>{busy === "save" ? "Saving…" : "Save"}</button>
              {!saved && <span className="text-xs text-amber-700">Unsaved changes</span>}
            </div>
          </div>

          <div className="space-y-2 rounded border border-gray-200 p-3">
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={!!sched?.auto_enabled} disabled={!sched || !status?.configured || !cfg.catalog_file || busy === "sched" || !saved} onChange={(e) => void changeSched({ enabled: e.target.checked })} />
              <span className="font-medium">Check for new files automatically</span>
              <select className={`${inputCls} !w-40`} value={sched?.interval_minutes ?? 60} disabled={!sched || busy === "sched"} onChange={(e) => void changeSched({ interval_minutes: Number(e.target.value) })}>
                {[5, 15, 30, 60, 180, 360, 720, 1440].map((m) => <option key={m} value={m}>{m < 60 ? `every ${m} min` : `every ${m / 60} h`}</option>)}
              </select>
            </label>
            {sched && (
              <p className="text-xs text-gray-500">
                {sched.auto_enabled ? "Automatic sync is ON." : "Automatic sync is OFF."} Each check only asks the server for the file&apos;s date and size; it downloads and imports only when Rocky Mountain has published something new.
                {sched.last_checked_at ? ` Last check ${new Date(sched.last_checked_at).toLocaleString()}.` : ""}
                {sched.last_changed_at ? ` Last change imported ${new Date(sched.last_changed_at).toLocaleString()}.` : ""}
              </p>
            )}
            {sched?.last_summary && <p className="text-xs text-gray-700">{sched.last_summary}</p>}
            {sched?.last_error && <p className="text-xs text-red-700">{sched.last_error}</p>}
          </div>
        </div>
      )}
    </div>
  );
}

export { SOURCE as SFTP_SOURCE_LABELS };
