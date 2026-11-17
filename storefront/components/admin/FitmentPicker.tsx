"use client";

import { useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Notice, btnCls, btn2Cls, inputCls } from "./ui";

/* Choose which makes, models and years of the PRODUCT's website fitment go on THIS listing's compatibility table.
   The website fitment is not changed. The ticked vehicles are matched to eBay's own vehicle list for the listing's category.
   Used on the draft screen (the server writes the draft) and on the live-listing screen (the new rows go into the table on screen
   and reach eBay only through the normal preview and confirm). The caller does the request; this is only the choosing. */

type Model = { model_id: number; make: string; model: string; years: number[] };

export type Pick = { model_id: number; years: number[] };

export default function FitmentPicker({
  choicesUrl,
  disabled,
  allowReplace,
  addLabel,
  beforeApply,
  apply: applyPicks,
}: {
  choicesUrl: string;
  disabled: boolean;
  allowReplace: boolean;
  addLabel: string;
  beforeApply?: () => Promise<boolean>; // saves unsaved edits first, when that matters
  apply: (picks: Pick[], mode: "add" | "replace") => Promise<string>; // does the work and returns the message to show
}) {
  const [open, setOpen] = useState(false);
  const [models, setModels] = useState<Model[] | null>(null);
  const [sel, setSel] = useState<Record<number, number[]>>({});
  const [filter, setFilter] = useState("");
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [done, setDone] = useState("");
  const [product, setProduct] = useState("");

  async function openIt() {
    setOpen(true);
    if (models) return;
    setBusy("load");
    setErr("");
    try {
      const r = await api<{ models: Model[]; product?: string }>(choicesUrl);
      setModels(r.models);
      setProduct(r.product ?? "");
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy("");
    }
  }

  const shown = (models ?? []).filter((m) => `${m.make} ${m.model}`.toLowerCase().includes(filter.trim().toLowerCase()));
  const picked = Object.values(sel).reduce((n, ys) => n + ys.length, 0);

  const toggleYear = (m: Model, y: number) =>
    setSel((s) => {
      const cur = s[m.model_id] ?? [];
      return { ...s, [m.model_id]: cur.includes(y) ? cur.filter((x) => x !== y) : [...cur, y].sort((a, b) => a - b) };
    });
  const toggleModel = (m: Model) => setSel((s) => ({ ...s, [m.model_id]: (s[m.model_id] ?? []).length === m.years.length ? [] : [...m.years] }));
  const setShown = (on: boolean) =>
    setSel((s) => {
      const n = { ...s };
      shown.forEach((m) => (n[m.model_id] = on ? [...m.years] : []));
      return n;
    });

  async function apply(mode: "add" | "replace") {
    if (picked === 0) return;
    if (mode === "replace" && !window.confirm("Replace the whole compatibility table with only the ticked vehicles? Rows you added by hand are lost.")) return;
    if (beforeApply && !(await beforeApply())) return;
    setBusy("apply");
    setErr("");
    setDone("");
    try {
      setDone(await applyPicks(Object.entries(sel).filter(([, ys]) => ys.length > 0).map(([id, years]) => ({ model_id: Number(id), years })), mode));
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="space-y-2">
      <button type="button" className={btn2Cls} disabled={disabled || busy !== ""} onClick={() => (open ? setOpen(false) : void openIt())}>
        {open ? "Close the fitment chooser" : "Choose makes, models and years from the product's fitment"}
      </button>
      {open && (
        <div className="space-y-2 rounded border border-gray-200 bg-gray-50 p-3">
          <p className="text-xs text-gray-600">
            Tick the vehicles this listing should carry{product ? ` (from ${product})` : ""}. The product&apos;s website fitment is not changed.
          </p>
          {busy === "load" && <p className="text-xs text-gray-500">Reading the product&apos;s fitment…</p>}
          {err && <Notice kind="error">{err}</Notice>}
          {done && <Notice kind="success">{done}</Notice>}
          {models && models.length === 0 && <p className="text-xs text-gray-500">This product has no fitment with years to choose from.</p>}
          {models && models.length > 0 && (
            <>
              <div className="flex flex-wrap items-center gap-3">
                <input className={`${inputCls} w-64`} placeholder="Filter by make or model" value={filter} onChange={(e) => setFilter(e.target.value)} />
                <button type="button" className="text-xs text-blue-700 hover:underline" onClick={() => setShown(true)}>Tick all shown</button>
                <button type="button" className="text-xs text-blue-700 hover:underline" onClick={() => setShown(false)}>Untick all shown</button>
                <span className="text-xs text-gray-500">{picked} year{picked === 1 ? "" : "s"} ticked</span>
              </div>
              <div className="max-h-96 overflow-auto rounded border border-gray-200 bg-white">
                <table className="w-full text-left text-xs">
                  <thead className="sticky top-0 bg-gray-50 text-gray-500">
                    <tr><th className="w-8 px-2 py-1" /><th className="px-2 py-1">Make and model</th><th className="px-2 py-1">Years (click to tick)</th></tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100">
                    {shown.map((m) => {
                      const on = sel[m.model_id] ?? [];
                      return (
                        <tr key={m.model_id} className="align-top">
                          <td className="px-2 py-1"><input type="checkbox" aria-label={`All years of ${m.make} ${m.model}`} checked={on.length === m.years.length} onChange={() => toggleModel(m)} /></td>
                          <td className="px-2 py-1 font-medium">{m.make} {m.model}</td>
                          <td className="px-2 py-1">
                            <span className="flex flex-wrap gap-1">
                              {m.years.map((y) => (
                                <button key={y} type="button" className={`rounded border px-1.5 py-0.5 ${on.includes(y) ? "border-green-700 bg-green-50 font-semibold text-green-800" : "border-gray-300 bg-white text-gray-500"}`} onClick={() => toggleYear(m, y)}>
                                  {y}
                                </button>
                              ))}
                            </span>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <div className="flex flex-wrap gap-2">
                <button type="button" className={btnCls} disabled={busy !== "" || picked === 0} onClick={() => void apply("add")}>
                  {busy === "apply" ? "Working…" : addLabel}
                </button>
                {allowReplace && (
                  <button type="button" className={btn2Cls} disabled={busy !== "" || picked === 0} onClick={() => void apply("replace")}>Make the table only these</button>
                )}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
