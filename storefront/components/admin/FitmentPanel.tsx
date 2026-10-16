"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Badge, Field, Notice, btnCls, btn2Cls, dangerCls, inputCls } from "./ui";

type FitYear = { vehicle_id: number; year: number; submodel: string | null };
type FitModel = {
  model_id: number;
  make: string;
  model: string;
  vehicle_type: string | null;
  years: FitYear[];
};
type Opt = { id: number; name: string };
type ModelOpt = { id: number; name: string; make: string; year_min: number | null; year_max: number | null };
type YearRow = { vehicle_id: number; year: number };

export default function FitmentPanel({ productId, isUniversal }: { productId: number; isUniversal: boolean }) {
  const [fits, setFits] = useState<FitModel[]>([]);
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  // pickers
  const [types, setTypes] = useState<Opt[]>([]);
  const [makes, setMakes] = useState<Opt[]>([]);
  const [typeId, setTypeId] = useState("");
  const [makeId, setMakeId] = useState("");
  const [models, setModels] = useState<ModelOpt[]>([]);
  const [modelId, setModelId] = useState("");
  const [years, setYears] = useState<number[]>([]);
  const [allYears, setAllYears] = useState(false);
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [rangeFrom, setRangeFrom] = useState("");
  const [rangeTo, setRangeTo] = useState("");

  const loadFits = useCallback(() => {
    api<{ models: FitModel[] }>(`/products/${productId}/fitment`)
      .then((r) => setFits(r.models))
      .catch((e) => setMsg({ kind: "error", text: errMsg(e) }));
  }, [productId]);
  useEffect(loadFits, [loadFits]);

  useEffect(() => {
    api<Opt[]>("/vehicle-types").then(setTypes).catch(() => {});
    api<Opt[]>("/makes").then(setMakes).catch(() => {});
  }, []);

  // models for the chosen make (and type, if one is chosen)
  useEffect(() => {
    setModelId("");
    setModels([]);
    if (!makeId) return;
    const p = new URLSearchParams({ make_id: makeId, limit: "200" });
    if (typeId) p.set("type_id", typeId);
    api<{ items: ModelOpt[] }>(`/models?${p}`)
      .then((r) => setModels(r.items))
      .catch((e) => setMsg({ kind: "error", text: errMsg(e) }));
  }, [makeId, typeId]);

  // years the chosen model has
  useEffect(() => {
    setYears([]);
    setPicked(new Set());
    setAllYears(false);
    setRangeFrom("");
    setRangeTo("");
    if (!modelId) return;
    api<YearRow[]>(`/models/${modelId}/years`)
      .then((rows) => setYears(Array.from(new Set(rows.map((r) => r.year))).sort((a, b) => a - b)))
      .catch((e) => setMsg({ kind: "error", text: errMsg(e) }));
  }, [modelId]);

  function toggle(year: number) {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(year)) next.delete(year);
      else next.add(year);
      return next;
    });
  }

  function applyRange() {
    const a = Number(rangeFrom);
    const b = Number(rangeTo);
    if (!a || !b) return;
    const lo = Math.min(a, b);
    const hi = Math.max(a, b);
    setPicked((prev) => {
      const next = new Set(prev);
      years.forEach((y) => {
        if (y >= lo && y <= hi) next.add(y);
      });
      return next;
    });
  }

  async function add() {
    setBusy(true);
    setMsg(null);
    try {
      const r = await api<{ added: number; already_present: number; skipped_years: number[] }>(
        `/products/${productId}/fitment`,
        {
          method: "POST",
          body: {
            model_id: Number(modelId),
            all_years: allYears,
            years: allYears ? undefined : Array.from(picked),
          },
        }
      );
      const parts = [`${r.added} added`];
      if (r.already_present) parts.push(`${r.already_present} already present`);
      if (r.skipped_years.length) parts.push(`skipped ${r.skipped_years.join(", ")} (model has no such year)`);
      setMsg({ kind: "success", text: parts.join(", ") });
      setPicked(new Set());
      setAllYears(false);
      loadFits();
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy(false);
    }
  }

  async function remove(body: { vehicle_ids?: number[]; model_id?: number }) {
    setBusy(true);
    setMsg(null);
    try {
      await api(`/products/${productId}/fitment/remove`, { method: "POST", body });
      loadFits();
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy(false);
    }
  }

  const canAdd = !!modelId && (allYears || picked.size > 0);

  return (
    <section className="space-y-5 rounded-lg border border-gray-200 bg-white p-4">
      <h2 className="text-lg font-semibold">Fitment</h2>

      {isUniversal && (
        <Notice kind="info">
          This product is marked universal, so it shows for every machine. You don&apos;t need to add fitment below.
        </Notice>
      )}
      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}

      <div>
        <h3 className="mb-2 text-sm font-medium text-gray-700">Fits these machines</h3>
        {fits.length === 0 ? (
          <p className="text-sm text-gray-500">No fitment added yet.</p>
        ) : (
          <ul className="space-y-3">
            {fits.map((f) => (
              <li key={f.model_id} className="rounded border border-gray-200 p-3">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <div className="text-sm font-medium">
                    {f.make} {f.model} {f.vehicle_type && <Badge>{f.vehicle_type}</Badge>}
                    <span className="ml-2 text-xs font-normal text-gray-500">{f.years.length} year(s)</span>
                  </div>
                  <button
                    className={dangerCls}
                    disabled={busy}
                    onClick={() => {
                      if (confirm(`Remove ${f.make} ${f.model} (all years)?`)) remove({ model_id: f.model_id });
                    }}
                  >
                    Remove model
                  </button>
                </div>
                <div className="flex flex-wrap gap-2">
                  {f.years.map((y) => (
                    <span key={y.vehicle_id} className="inline-flex items-center gap-1 rounded border border-gray-300 bg-gray-50 px-2 py-1 text-sm">
                      {y.year}
                      <button type="button" className="text-gray-400 hover:text-red-600" disabled={busy} onClick={() => remove({ vehicle_ids: [y.vehicle_id] })} aria-label={`Remove ${y.year}`}>
                        ×
                      </button>
                    </span>
                  ))}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="space-y-3 border-t border-gray-200 pt-4">
        <h3 className="text-sm font-medium text-gray-700">Add fitment</h3>
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Machine type (narrows models)">
            <select className={inputCls} value={typeId} onChange={(e) => setTypeId(e.target.value)}>
              <option value="">All types</option>
              {types.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
            </select>
          </Field>
          <Field label="Make">
            <select className={inputCls} value={makeId} onChange={(e) => setMakeId(e.target.value)}>
              <option value="">Select make</option>
              {makes.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
            </select>
          </Field>
          <Field label="Model">
            <select className={inputCls} value={modelId} onChange={(e) => setModelId(e.target.value)} disabled={!makeId}>
              <option value="">Select model</option>
              {models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}{m.year_min != null ? ` (${m.year_min === m.year_max ? m.year_min : `${m.year_min}–${m.year_max}`})` : ""}
                </option>
              ))}
            </select>
          </Field>
        </div>

        {modelId && (
          <div className="space-y-3 rounded border border-gray-200 bg-gray-50 p-3">
            {years.length === 0 ? (
              <p className="text-sm text-gray-500">This model has no years yet. Add them under Models &amp; years first.</p>
            ) : (
              <>
                <label className="flex items-center gap-2 text-sm font-medium">
                  <input type="checkbox" checked={allYears} onChange={(e) => setAllYears(e.target.checked)} />
                  Fits all years ({years[0]}–{years[years.length - 1]})
                </label>

                <div className={allYears ? "pointer-events-none opacity-40" : ""}>
                  <div className="mb-3 flex flex-wrap items-end gap-2">
                    <Field label="Pick a range: from">
                      <select className={`${inputCls} w-28`} value={rangeFrom} onChange={(e) => setRangeFrom(e.target.value)}>
                        <option value="">—</option>
                        {years.map((y) => <option key={y} value={y}>{y}</option>)}
                      </select>
                    </Field>
                    <Field label="to">
                      <select className={`${inputCls} w-28`} value={rangeTo} onChange={(e) => setRangeTo(e.target.value)}>
                        <option value="">—</option>
                        {years.map((y) => <option key={y} value={y}>{y}</option>)}
                      </select>
                    </Field>
                    <button type="button" className={btn2Cls} onClick={applyRange} disabled={!rangeFrom || !rangeTo}>
                      Select range
                    </button>
                    <button type="button" className={btn2Cls} onClick={() => setPicked(new Set())}>Clear</button>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {years.map((y) => (
                      <label key={y} className={`cursor-pointer rounded border px-2 py-1 text-sm ${picked.has(y) ? "border-black bg-black text-white" : "border-gray-300 bg-white"}`}>
                        <input type="checkbox" className="sr-only" checked={picked.has(y)} onChange={() => toggle(y)} />
                        {y}
                      </label>
                    ))}
                  </div>
                </div>
              </>
            )}
            <button className={btnCls} disabled={busy || !canAdd} onClick={add}>
              {allYears ? "Add all years" : `Add ${picked.size} year${picked.size === 1 ? "" : "s"}`}
            </button>
          </div>
        )}
      </div>
    </section>
  );
}
