"use client";

import { useEffect, useMemo, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Notice, btn2Cls, inputCls } from "./ui";

/* The compatible-vehicle table of an eBay listing or draft, editable.

   Rows are {pairs: [[name, value], ...], notes}. Vehicles to ADD are picked from eBay's own lists (Make, then Model, then
   Year, plus Trim / Engine when the category uses them), asked live from /ebay-vocab, so any vehicle eBay knows can be
   added, not only the ones saved on the website. Nothing is sent to eBay from here: the parent saves / sends the whole
   list. */

export type CompatRow = { pairs: string[][]; notes: string };

const SHOW = 200; // rows drawn at once; the filter narrows the list

const valueOf = (r: CompatRow, name: string) => r.pairs.find((p) => p[0].toLowerCase() === name.toLowerCase())?.[1] ?? "";
const sigOf = (r: CompatRow) => JSON.stringify([[...r.pairs].map((p) => [p[0], p[1]]).sort(), r.notes.trim()]);

export default function CompatEditor({
  categoryId,
  rows,
  baseline,
  onChange,
  disabled,
  lockedReason,
}: {
  categoryId: string;
  rows: CompatRow[];
  baseline?: CompatRow[]; // what eBay has now (live listing), to show how many were added / removed
  onChange: (rows: CompatRow[]) => void;
  disabled?: boolean;
  lockedReason?: string | null; // when set the list is shown but cannot be changed
}) {
  const [filter, setFilter] = useState("");
  const [open, setOpen] = useState(false);
  const [props, setProps] = useState<string[] | null>(null);
  const [vocabErr, setVocabErr] = useState("");
  const [make, setMake] = useState("");
  const [model, setModel] = useState("");
  const [extra, setExtra] = useState<Record<string, string>>({});
  const [makes, setMakes] = useState<string[]>([]);
  const [models, setModels] = useState<string[]>([]);
  const [years, setYears] = useState<string[]>([]);
  const [extras, setExtras] = useState<Record<string, string[]>>({});
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [note, setNote] = useState("");
  const [loading, setLoading] = useState("");
  const [msg, setMsg] = useState("");

  const locked = !!lockedReason;
  const off = !!disabled || locked;

  const nameOf = (want: string) => props?.find((p) => p.toLowerCase() === want.toLowerCase()) ?? want;
  const makeName = nameOf("Make");
  const modelName = nameOf("Model");
  const yearName = nameOf("Year");
  const otherProps = (props ?? []).filter((p) => ![makeName, modelName, yearName].includes(p));

  const q = (path: string, params: Record<string, string>) =>
    api<{ values: string[] }>(`/ebay-vocab/values?category_id=${encodeURIComponent(categoryId)}&property=${encodeURIComponent(path)}&filters=${encodeURIComponent(JSON.stringify(params))}`);

  // Open the adder: ask eBay which properties this category uses and which makes it knows.
  useEffect(() => {
    if (!open || props !== null || !categoryId) return;
    let dead = false;
    setLoading("props");
    setVocabErr("");
    api<{ properties: string[] }>(`/ebay-vocab/properties?category_id=${encodeURIComponent(categoryId)}`)
      .then(async (r) => {
        if (dead) return;
        setProps(r.properties);
        const mk = r.properties.find((p) => p.toLowerCase() === "make") ?? "Make";
        const v = await q(mk, {});
        if (!dead) setMakes(v.values);
      })
      .catch((e) => {
        if (!dead) setVocabErr(errMsg(e));
      })
      .finally(() => {
        if (!dead) setLoading("");
      });
    return () => {
      dead = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, categoryId]);

  useEffect(() => {
    setModel("");
    setModels([]);
    setYears([]);
    setPicked(new Set());
    setExtra({});
    setExtras({});
    if (!make || !props) return;
    let dead = false;
    setLoading("models");
    q(modelName, { [makeName]: make })
      .then((r) => !dead && setModels(r.values))
      .catch((e) => !dead && setVocabErr(errMsg(e)))
      .finally(() => !dead && setLoading(""));
    return () => {
      dead = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [make]);

  useEffect(() => {
    setYears([]);
    setPicked(new Set());
    setExtra({});
    setExtras({});
    if (!make || !model || !props) return;
    let dead = false;
    setLoading("years");
    const f = { [makeName]: make, [modelName]: model };
    Promise.all([q(yearName, f), ...otherProps.map((p) => q(p, f).then((r) => [p, r.values] as const))])
      .then(([y, ...rest]) => {
        if (dead) return;
        setYears([...(y as { values: string[] }).values].sort());
        setExtras(Object.fromEntries(rest as (readonly [string, string[]])[]));
      })
      .catch((e) => !dead && setVocabErr(errMsg(e)))
      .finally(() => !dead && setLoading(""));
    return () => {
      dead = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [model]);

  const have = useMemo(() => new Set(rows.map(sigOf)), [rows]);

  const shown = useMemo(() => {
    const t = filter.trim().toLowerCase();
    const list = rows.map((r, i) => ({ r, i }));
    if (!t) return list;
    return list.filter(({ r }) => (r.pairs.map((p) => p[1]).join(" ") + " " + r.notes).toLowerCase().includes(t));
  }, [rows, filter]);

  const columns = useMemo(() => {
    const first = [makeName, modelName, yearName];
    const more = Array.from(new Set(rows.flatMap((r) => r.pairs.map((p) => p[0])))).filter((n) => !first.map((x) => x.toLowerCase()).includes(n.toLowerCase()));
    return [...first, ...more];
  }, [rows, makeName, modelName, yearName]);

  const added = baseline ? rows.filter((r) => !new Set(baseline.map(sigOf)).has(sigOf(r))).length : 0;
  const removed = baseline ? baseline.filter((r) => !have.has(sigOf(r))).length : 0;

  function newRows(): CompatRow[] {
    const out: CompatRow[] = [];
    const tail = Object.entries(extra).filter(([, v]) => v).map(([k, v]) => [k, v]);
    for (const y of Array.from(picked).sort()) {
      out.push({ pairs: [[makeName, make], [modelName, model], [yearName, y], ...tail], notes: note.trim() });
    }
    return out;
  }

  function add() {
    const fresh = newRows().filter((r) => !have.has(sigOf(r)));
    const skipped = picked.size - fresh.length;
    if (fresh.length === 0) {
      setMsg(picked.size ? "Those vehicles are already in the list." : "Pick at least one year first.");
      return;
    }
    onChange([...rows, ...fresh]);
    setMsg(`Added ${fresh.length} vehicle${fresh.length === 1 ? "" : "s"} (${make} ${model})${skipped > 0 ? `; ${skipped} already in the list` : ""}. Not sent to eBay yet.`);
    setPicked(new Set());
  }

  function selectRange() {
    const a = parseInt(from, 10);
    const b = parseInt(to, 10);
    if (!Number.isFinite(a) || !Number.isFinite(b)) return;
    const lo = Math.min(a, b);
    const hi = Math.max(a, b);
    setPicked(new Set(years.filter((y) => Number(y) >= lo && Number(y) <= hi)));
  }

  const dropIdx = (idx: Set<number>) => onChange(rows.filter((_, i) => !idx.has(i)));

  return (
    <div className="space-y-3 text-sm">
      {locked && <Notice>{lockedReason}</Notice>}
      <div className="flex flex-wrap items-center gap-3">
        <span className="text-xs text-gray-600">
          {rows.length} vehicle{rows.length === 1 ? "" : "s"}
          {baseline && (added > 0 || removed > 0) ? ` (${added} added, ${removed} removed since eBay's list)` : ""}
        </span>
        <div className="relative w-56">
          <input className={`${inputCls} pr-8`} placeholder="Filter (e.g. Honda CR85)" value={filter} onChange={(e) => setFilter(e.target.value)} />
          {filter && (
            <button type="button" aria-label="Clear filter" className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-500 hover:text-black" onClick={() => setFilter("")}>
              ×
            </button>
          )}
        </div>
        {filter.trim() && shown.length > 0 && !off && (
          <button
            type="button"
            className="text-xs text-red-700 hover:underline"
            onClick={() => {
              if (window.confirm(`Remove the ${shown.length} vehicle${shown.length === 1 ? "" : "s"} matching the filter from this list?`)) dropIdx(new Set(shown.map((s) => s.i)));
            }}
          >
            Remove the {shown.length} shown
          </button>
        )}
        {!off && (
          <button type="button" className={btn2Cls} onClick={() => setOpen(!open)}>
            {open ? "Close vehicle picker" : "Add vehicles"}
          </button>
        )}
      </div>

      {open && !off && (
        <div className="space-y-3 rounded border border-blue-200 bg-blue-50 p-3">
          <p className="text-xs text-gray-600">
            Choices come from eBay&rsquo;s own vehicle list, so anything eBay lists can be added, not only the fitment saved on the website.
          </p>
          {!categoryId && <Notice kind="error">This has no eBay category yet, so eBay&rsquo;s vehicle list cannot be read.</Notice>}
          {vocabErr && <Notice kind="error">{vocabErr}</Notice>}
          {loading === "props" && <p className="text-xs text-gray-500">Asking eBay for its vehicle list…</p>}
          {props && (
            <>
              <div className="flex flex-wrap items-end gap-3">
                <label className="block">
                  <span className="mb-1 block font-medium">{makeName}</span>
                  <select className={`${inputCls} w-48`} value={make} onChange={(e) => setMake(e.target.value)}>
                    <option value="">Choose…</option>
                    {makes.map((m) => <option key={m} value={m}>{m}</option>)}
                  </select>
                </label>
                <label className="block">
                  <span className="mb-1 block font-medium">{modelName}</span>
                  <select className={`${inputCls} w-56`} value={model} disabled={!make || loading === "models"} onChange={(e) => setModel(e.target.value)}>
                    <option value="">{loading === "models" ? "Loading…" : "Choose…"}</option>
                    {models.map((m) => <option key={m} value={m}>{m}</option>)}
                  </select>
                </label>
                {otherProps.map((p) => (
                  <label key={p} className="block">
                    <span className="mb-1 block font-medium">{p} (optional)</span>
                    <select className={`${inputCls} w-44`} value={extra[p] ?? ""} disabled={!model} onChange={(e) => setExtra({ ...extra, [p]: e.target.value })}>
                      <option value="">Any</option>
                      {(extras[p] ?? []).map((m) => <option key={m} value={m}>{m}</option>)}
                    </select>
                  </label>
                ))}
              </div>
              {model && (
                <div className="space-y-2">
                  <div className="flex flex-wrap items-center gap-2 text-xs">
                    <span className="font-medium">{yearName}s eBay lists for {make} {model}</span>
                    <button type="button" className="text-blue-700 hover:underline" onClick={() => setPicked(new Set(years))}>All</button>
                    <button type="button" className="text-blue-700 hover:underline" onClick={() => setPicked(new Set())}>None</button>
                    <span className="text-gray-500">or range</span>
                    <input className={`${inputCls} w-20`} placeholder="from" value={from} onChange={(e) => setFrom(e.target.value)} />
                    <input className={`${inputCls} w-20`} placeholder="to" value={to} onChange={(e) => setTo(e.target.value)} />
                    <button type="button" className="text-blue-700 hover:underline" onClick={selectRange}>Select</button>
                  </div>
                  {loading === "years" && <p className="text-xs text-gray-500">Loading years…</p>}
                  {loading !== "years" && years.length === 0 && <p className="text-xs text-amber-700">eBay lists no years for this make and model.</p>}
                  <div className="flex flex-wrap gap-2">
                    {years.map((y) => {
                      const on = picked.has(y);
                      return (
                        <label key={y} className={`cursor-pointer rounded border px-2 py-1 text-xs ${on ? "border-black bg-black text-white" : "border-gray-300 bg-white"}`}>
                          <input
                            type="checkbox"
                            className="sr-only"
                            checked={on}
                            onChange={() => {
                              const n = new Set(picked);
                              if (on) n.delete(y);
                              else n.add(y);
                              setPicked(n);
                            }}
                          />
                          {y}
                        </label>
                      );
                    })}
                  </div>
                  <div className="flex flex-wrap items-end gap-3">
                    <label className="block min-w-[14rem] flex-1">
                      <span className="mb-1 block text-xs font-medium">Note for these rows (optional, shown on the listing)</span>
                      <input className={inputCls} value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} />
                    </label>
                    <button type="button" className="rounded bg-black px-4 py-2 text-sm font-semibold text-white hover:bg-gray-800 disabled:opacity-40" disabled={picked.size === 0} onClick={add}>
                      Add {picked.size || ""} vehicle{picked.size === 1 ? "" : "s"}
                    </button>
                  </div>
                </div>
              )}
            </>
          )}
          {msg && <p className="text-xs text-green-800">{msg}</p>}
        </div>
      )}

      {rows.length === 0 ? (
        <p className="text-xs text-gray-500">No compatible vehicles on this list.</p>
      ) : (
        <div className="max-h-96 overflow-auto rounded border border-gray-200">
          <table className="w-full text-left text-xs">
            <thead className="sticky top-0 bg-gray-50 uppercase text-gray-500">
              <tr>
                {columns.map((c) => <th key={c} className="px-2 py-1">{c}</th>)}
                <th className="px-2 py-1">Notes</th>
                <th className="px-2 py-1" />
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {shown.slice(0, SHOW).map(({ r, i }) => (
                <tr key={i}>
                  {columns.map((c) => <td key={c} className="px-2 py-1">{valueOf(r, c)}</td>)}
                  <td className="px-2 py-1">
                    {off ? (
                      r.notes
                    ) : (
                      <input
                        className="w-40 rounded border border-gray-200 px-1 py-0.5"
                        value={r.notes}
                        maxLength={500}
                        onChange={(e) => onChange(rows.map((x, k) => (k === i ? { ...x, notes: e.target.value } : x)))}
                      />
                    )}
                  </td>
                  <td className="px-2 py-1 text-right">
                    {!off && (
                      <button type="button" className="text-red-700 hover:underline" onClick={() => dropIdx(new Set([i]))}>
                        Remove
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {shown.length > SHOW && <p className="border-t border-gray-100 p-2 text-xs text-gray-500">Showing the first {SHOW} of {shown.length}. Use the filter to find the rest.</p>}
        </div>
      )}
    </div>
  );
}
