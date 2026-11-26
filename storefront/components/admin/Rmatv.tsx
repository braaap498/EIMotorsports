"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Badge, Notice, btnCls, btn2Cls, inputCls } from "./ui";
import { parseDelimited } from "./rmatvCsv";
import RmatvCatalog from "./RmatvCatalog";
import RmatvLinks from "./RmatvLinks";
import RmatvSftp, { SFTP_SOURCE_LABELS } from "./RmatvSftp";

// Rocky Mountain ATV/MC: its catalog (read from a file you upload) and the tools around it. Owners and admins only.
// This is the distributor's licensed data: dealer cost and stock stay on this screen and are never shown on the public store.
// Purchase orders and "Distributor parts" on a product already work for any distributor; this adds the catalog they search.

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
type FieldDef = { key: string; label: string; required: boolean };
type Finish = { ok: boolean; needs_review: boolean; missing?: number; held?: number; stored: number; new: number; skipped?: number; retired?: number };
type Staged = { fileName: string; header: string[]; data: string[][]; fields: FieldDef[]; mapping: Record<string, number | ""> };

const PIECE = 3000; // data lines per request
const n = (x: number) => x.toLocaleString("en-US");
const tone = (s: string) => (s === "done" ? "green" : s === "running" ? "blue" : "amber");

function runLabel(r: Run) {
  if (r.source === "sftp_price") return `prices: ${n(r.rows_new)} of ${n(r.rows_seen)} lines matched a stored part`;
  if (r.source === "sftp_photos") return `photo links: ${n(r.rows_new)} parts updated (${n(r.rows_seen)} lines read)`;
  return (
    `${n(r.rows_seen)} parts, ${n(r.rows_new)} new` +
    (r.rows_skipped ? `, ${n(r.rows_skipped)} duplicate or unreadable lines skipped` : "") +
    (r.rows_deactivated ? `, ${n(r.rows_deactivated)} retired` : "")
  );
}

type Msg = { kind: "error" | "success" | "info"; text: string } | null;

/* ------------------------------------------------------------------- import */

function RmatvImport({ runs, onChanged }: { runs: Run[]; onChanged: () => void }) {
  const [msg, setMsg] = useState<Msg>(null);
  const [progress, setProgress] = useState("");
  const [busy, setBusy] = useState(false);
  const [staged, setStaged] = useState<Staged | null>(null);
  const [pending, setPending] = useState<{ runId: number; rowsSent: number; missing: number; held: number } | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);

  async function chooseFile(file: File | null) {
    if (!file) return;
    setMsg(null);
    setPending(null);
    setStaged(null);
    try {
      const { rows } = parseDelimited(await file.text());
      if (rows.length < 2) throw new Error("The file has no data lines. Save the catalog as CSV (comma separated) with a header row, then try again.");
      const header = rows[0].map((h) => h.trim());
      const r = await api<{ mapped: Record<string, number>; fields: FieldDef[] }>("/rmatv/import/detect", { method: "POST", body: { columns: header } });
      const mapping: Record<string, number | ""> = {};
      r.fields.forEach((fd) => (mapping[fd.key] = r.mapped[fd.key] ?? ""));
      setStaged({ fileName: file.name, header, data: rows.slice(1), fields: r.fields, mapping });
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  const chosen = (s: Staged) => Object.entries(s.mapping).filter(([, v]) => v !== "") as [string, number][];
  const mapped = staged ? Object.fromEntries(chosen(staged)) : {};
  const columnsReused = staged ? new Set(chosen(staged).map(([, v]) => v)).size !== chosen(staged).length : false;

  async function finish(runId: number, rowsSent: number, force: boolean) {
    const r = await api<Finish>("/rmatv/import/finish", { method: "POST", body: { run_id: runId, rows_sent: rowsSent, force } });
    if (r.needs_review) {
      setPending({ runId, rowsSent, missing: r.missing ?? 0, held: r.held ?? 0 });
      setMsg({
        kind: "error",
        text: `Stored ${n(r.stored)} parts, but ${n(r.missing ?? 0)} of the ${n(r.held ?? 0)} parts already held are not in this file. Nothing was retired yet.`,
      });
      return;
    }
    setPending(null);
    setStaged(null);
    setMsg({
      kind: "success",
      text: `Done. ${n(r.stored)} parts stored (${n(r.new)} new), ${n(r.retired ?? 0)} no longer in the file${r.skipped ? `, ${n(r.skipped)} duplicate or unreadable lines skipped` : ""}.`,
    });
  }

  async function runImport() {
    if (!staged) return;
    setBusy(true);
    setMsg(null);
    setPending(null);
    try {
      const { run_id } = await api<{ run_id: number }>("/rmatv/import/start", { method: "POST", body: { source: "file_upload" } });
      for (let i = 0; i < staged.data.length; i += PIECE) {
        setProgress(`Storing parts ${n(i + 1)} to ${n(Math.min(i + PIECE, staged.data.length))} of ${n(staged.data.length)}…`);
        await api("/rmatv/import/chunk", { method: "POST", body: { run_id, mapping: mapped, rows: staged.data.slice(i, i + PIECE) } });
      }
      setProgress("Checking the whole file arrived…");
      await finish(run_id, staged.data.length, false);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy(false);
      setProgress("");
      onChanged();
    }
  }

  async function confirmRetire() {
    if (!pending) return;
    setBusy(true);
    try {
      await finish(pending.runId, pending.rowsSent, true);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy(false);
      onChanged();
    }
  }

  const sample = (s: Staged, col: number) => (s.data[0]?.[col] ?? "").slice(0, 40);

  return (
    <div className="space-y-4">
      <RmatvSftp runs={runs} onChanged={onChanged} />
      <div className="space-y-2 rounded border border-gray-200 bg-white p-4 text-sm">
        <div className="flex flex-wrap items-center gap-3">
          <input ref={fileRef} type="file" accept=".csv,.tsv,.txt,text/csv,text/plain" className="hidden" onChange={(e) => void chooseFile(e.target.files?.[0] ?? null)} />
          <button type="button" className={btnCls} disabled={busy} onClick={() => fileRef.current?.click()}>
            Choose catalog file (CSV)
          </button>
          <span className="text-xs text-gray-500">{progress || "A part list or price list from the Rocky Mountain dealer site, saved as CSV with a header row. Run it again any time to refresh prices and stock."}</span>
        </div>
        <p className="text-xs text-gray-500">
          Uploading a file by hand still works and fills the same catalog as the SFTP feed. If Rocky Mountain later gives you API access, it will fill this same catalog too and nothing else here changes.
        </p>
      </div>

      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}
      {pending && (
        <div className="flex flex-wrap items-center gap-3 rounded border border-amber-300 bg-amber-50 p-3 text-sm">
          <span>That file is much smaller than what is stored. If it is the wrong file, leave it: nothing was retired. If Rocky Mountain really dropped these parts, confirm.</span>
          <button type="button" className={btnCls} disabled={busy} onClick={() => void confirmRetire()}>
            Retire the {n(pending.missing)} missing parts
          </button>
        </div>
      )}

      {staged && (
        <div className="space-y-3 rounded border border-gray-200 bg-white p-4 text-sm">
          <div>
            <b>{staged.fileName}</b> · {n(staged.data.length)} lines. Check which column holds what. Nothing is stored until you click Import.
          </div>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {staged.fields.map((fd) => (
              <label key={fd.key} className="block">
                <span className="mb-1 block text-xs text-gray-600">
                  {fd.label}
                  {fd.required && <span className="text-red-700"> *</span>}
                </span>
                <select
                  className={inputCls}
                  value={staged.mapping[fd.key]}
                  onChange={(e) => setStaged({ ...staged, mapping: { ...staged.mapping, [fd.key]: e.target.value === "" ? "" : Number(e.target.value) } })}
                >
                  <option value="">{fd.required ? "(choose a column)" : "(not in the file)"}</option>
                  {staged.header.map((h, i) => (
                    <option key={i} value={i}>
                      {h || `Column ${i + 1}`}
                      {sample(staged, i) ? ` — e.g. ${sample(staged, i)}` : ""}
                    </option>
                  ))}
                </select>
              </label>
            ))}
          </div>
          {columnsReused && <Notice kind="error">Two fields use the same column. Each field needs its own column.</Notice>}
          {staged.mapping.sku === "" && <Notice kind="error">Choose the column that holds the SKU (the Rocky Mountain part number). It is what goes on the purchase order.</Notice>}
          {staged.mapping.dealer_cost === "" && staged.mapping.sku !== "" && (
            <Notice kind="info">No cost column chosen. Parts will be stored without a cost, so purchase orders would start at $0.00. Pick your dealer price column if the file has one.</Notice>
          )}
          <div className="overflow-x-auto rounded border border-gray-100">
            <table className="w-full text-left text-xs">
              <thead className="bg-gray-50 text-gray-500">
                <tr>
                  {staged.fields.filter((fd) => staged.mapping[fd.key] !== "").map((fd) => (
                    <th key={fd.key} className="px-2 py-1">
                      {fd.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {staged.data.slice(0, 5).map((row, i) => (
                  <tr key={i}>
                    {staged.fields.filter((fd) => staged.mapping[fd.key] !== "").map((fd) => (
                      <td key={fd.key} className="px-2 py-1">
                        {row[staged.mapping[fd.key] as number] ?? ""}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="flex gap-2">
            <button type="button" className={btnCls} disabled={busy || columnsReused || staged.mapping.sku === ""} onClick={() => void runImport()}>
              {busy ? "Importing…" : `Import ${n(staged.data.length)} lines`}
            </button>
            <button type="button" className={btn2Cls} disabled={busy} onClick={() => setStaged(null)}>
              Cancel
            </button>
          </div>
        </div>
      )}

      <div className="rounded border border-gray-200 bg-white p-4 text-sm">
        <h2 className="mb-2 font-semibold">Import log ({runs.length})</h2>
        <ul className="max-h-[24rem] divide-y divide-gray-100 overflow-y-auto rounded border border-gray-100 text-xs">
          {runs.map((r) => (
            <li key={r.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-2 py-1.5">
              <span className="text-gray-500">{new Date(r.started_at).toLocaleString()}</span>
              <Badge tone={tone(r.status)}>{r.status === "needs_review" ? "needs review" : r.status}</Badge>
              <span>{SFTP_SOURCE_LABELS[r.source] ? <span className="mr-1 text-gray-500">[{SFTP_SOURCE_LABELS[r.source]}]</span> : null}{runLabel(r)}</span>
              {r.created_by && <span className="text-gray-400">{r.created_by}</span>}
              {r.note && <span className="text-red-700">{r.note}</span>}
            </li>
          ))}
          {runs.length === 0 && <li className="px-2 py-4 text-center text-gray-500">Nothing has been imported yet.</li>}
        </ul>
      </div>
    </div>
  );
}

/* --------------------------------------------------------------------- page */

type Tab = "catalog" | "import";

export default function RmatvInner() {
  const [counts, setCounts] = useState<Counts | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [tab, setTab] = useState<Tab>("catalog");
  const [version, setVersion] = useState(0); // bumps after an import so the catalog reloads
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const r = await api<{ counts: Counts; runs: Run[] }>("/rmatv/summary");
      setCounts(r.counts);
      setRuns(r.runs);
      setVersion((v) => v + 1);
    } catch (e) {
      setError(errMsg(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <div className="space-y-5">
      <p className="text-sm text-gray-600">
        Rocky Mountain ATV/MC parts and your cost. This is licensed data: it stays on this screen and is never shown on the public store. To use a part, open a product and add it under Distributor parts; it then
        fills in on purchase orders to Rocky Mountain.
      </p>
      {error && <Notice kind="error">{error}</Notice>}
      {counts && (
        <p className="text-sm text-gray-600">
          <b className="text-gray-900">{n(counts.active)}</b> parts · <b className="text-gray-900">{n(counts.in_stock)}</b> in stock at Rocky Mountain · <b className="text-gray-900">{n(counts.brands)}</b> brands · last update{" "}
          {counts.last_seen ? new Date(counts.last_seen).toLocaleString() : "never"}
        </p>
      )}

      <div className="flex flex-wrap gap-1 border-b border-gray-200">
        {(["catalog", "import"] as const).map((k) => (
          <button
            key={k}
            type="button"
            onClick={() => setTab(k)}
            className={`-mb-px rounded-t px-4 py-2 text-sm ${tab === k ? "border border-gray-200 border-b-white bg-white font-medium" : "text-gray-600 hover:text-gray-900"}`}
          >
            {k === "catalog" ? "Catalog" : "Import"}
          </button>
        ))}
      </div>

      <div className={tab === "catalog" ? "" : "hidden"}>
        <RmatvCatalog hasData={!!counts?.active} version={version} />
      </div>
      <div className={tab === "import" ? "" : "hidden"}>
        <RmatvImport runs={runs} onChanged={() => void refresh()} />
        <div className="mt-5"><RmatvLinks onChanged={() => void refresh()} /></div>
      </div>
    </div>
  );
}
