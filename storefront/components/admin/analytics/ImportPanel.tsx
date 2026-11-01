"use client";

import { useRef, useState } from "react";
import { errMsg } from "../../../lib/adminApi";
import { Notice, btnCls, btn2Cls, inputCls } from "../ui";
import { readFiles, sendReport, type ImportResult, type ReportFile } from "./importCsv";

export type StoreInfo = { store: string; orders: number; first_date: string; last_date: string; last_import: string };

type Row = ReportFile & { storeName: string; state: "ready" | "sending" | "done" | "error"; done: number; result?: ImportResult; error?: string };

const fmtDay = (d: string) => new Date(d + "T00:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });

export default function ImportPanel({ stores, onDone }: { stores: StoreInfo[]; onDone: () => void }) {
  const [rows, setRows] = useState<Row[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const input = useRef<HTMLInputElement>(null);

  async function pick(files: File[]) {
    setError("");
    if (!files.length) return;
    try {
      const reps = await readFiles(files);
      setRows((old) => [
        ...old,
        ...reps.map((r) => ({ ...r, storeName: r.store ?? "", state: "ready" as const, done: 0 })),
      ]);
    } catch (e) {
      setError(errMsg(e));
    }
    if (input.current) input.current.value = "";
  }

  const patch = (i: number, p: Partial<Row>) => setRows((rs) => rs.map((r, k) => (k === i ? { ...r, ...p } : r)));

  async function run() {
    setBusy(true);
    setError("");
    for (let i = 0; i < rows.length; i++) {
      const r = rows[i];
      if (r.state === "done") continue;
      if (!r.storeName.trim()) {
        patch(i, { state: "error", error: "Type the store name first." });
        continue;
      }
      patch(i, { state: "sending", done: 0, error: undefined });
      try {
        const res = await sendReport(r, r.storeName.trim(), (done) => patch(i, { done }));
        patch(i, { state: "done", result: res, done: r.orders });
      } catch (e) {
        patch(i, { state: "error", error: errMsg(e) });
      }
    }
    setBusy(false);
    onDone();
  }

  const waiting = rows.some((r) => r.state !== "done");

  return (
    <div className="space-y-3 rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      <div>
        <h2 className="text-sm font-semibold">Add eBay order data</h2>
        <p className="text-xs text-gray-500">
          In Seller Hub go to Reports &gt; Downloads and download the “All orders” report for a store and date range. Drop the .csv or the .zip
          here, one store at a time or several together. Orders already in the database are updated, not duplicated, so overlapping date ranges are fine.
        </p>
      </div>

      {stores.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="text-gray-500">
              <tr><th className="py-1 pr-4 font-medium">Store</th><th className="pr-4 font-medium">Orders</th><th className="pr-4 font-medium">First sale</th><th className="pr-4 font-medium">Last sale</th></tr>
            </thead>
            <tbody>
              {stores.map((s) => (
                <tr key={s.store} className="border-t border-gray-100">
                  <td className="py-1 pr-4 font-medium">{s.store}</td>
                  <td className="pr-4 tabular-nums">{s.orders.toLocaleString()}</td>
                  <td className="pr-4">{fmtDay(s.first_date)}</td>
                  <td className="pr-4">{fmtDay(s.last_date)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => {
          e.preventDefault();
          pick(Array.from(e.dataTransfer.files));
        }}
        className="flex flex-col items-center gap-2 rounded-lg border-2 border-dashed border-gray-300 p-6 text-sm text-gray-600"
      >
        <span>Drop report files here</span>
        <input ref={input} type="file" accept=".csv,.zip" multiple className="hidden" onChange={(e) => pick(Array.from(e.target.files ?? []))} />
        <button type="button" className={btn2Cls} onClick={() => input.current?.click()} disabled={busy}>Choose files…</button>
      </div>

      {error && <Notice kind="error">{error}</Notice>}

      {rows.length > 0 && (
        <div className="space-y-2">
          <datalist id="analytics-stores">{stores.map((s) => <option key={s.store} value={s.store} />)}</datalist>
          {rows.map((r, i) => (
            <div key={i} className="flex flex-wrap items-center gap-2 rounded border border-gray-200 p-2 text-xs">
              <span className="min-w-0 flex-1 basis-48 truncate font-medium" title={r.name}>{r.name}</span>
              <span className="text-gray-500">{r.orders.toLocaleString()} orders</span>
              <input
                list="analytics-stores"
                className={`${inputCls} !w-52`}
                placeholder="Store (eBay seller id)"
                value={r.storeName}
                disabled={busy || r.state === "done"}
                onChange={(e) => patch(i, { storeName: e.target.value })}
              />
              {r.state === "sending" && <span className="text-gray-600">Sending… {r.done.toLocaleString()} / {r.orders.toLocaleString()}</span>}
              {r.state === "done" && r.result && (
                <span className="text-green-700">Done: {r.result.added.toLocaleString()} new, {r.result.updated.toLocaleString()} updated{r.result.skipped ? `, ${r.result.skipped} skipped` : ""}</span>
              )}
              {r.state === "error" && <span className="text-red-700">{r.error}</span>}
              {r.state !== "sending" && r.state !== "done" && !busy && (
                <button type="button" className="text-gray-500 underline" onClick={() => setRows((rs) => rs.filter((_, k) => k !== i))}>Remove</button>
              )}
            </div>
          ))}
          <div className="flex gap-2">
            <button type="button" className={btnCls} onClick={run} disabled={busy || !waiting}>{busy ? "Importing…" : "Import"}</button>
            {!busy && <button type="button" className={btn2Cls} onClick={() => setRows([])}>Clear list</button>}
          </div>
        </div>
      )}
    </div>
  );
}
