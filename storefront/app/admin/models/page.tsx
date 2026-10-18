"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, errMsg } from "../../../lib/adminApi";
import ArrowBar from "../../../components/admin/ArrowBar";
import { Badge, Field, Modal, Notice, Pager, btnCls, btn2Cls, dangerCls, inputCls } from "../../../components/admin/ui";

type Opt = { id: number; name: string };
type ModelRow = {
  id: number;
  name: string;
  slug: string;
  make_id: number;
  make: string;
  vehicle_type_id: number | null;
  vehicle_type: string | null;
  year_min: number | null;
  year_max: number | null;
  products: number;
};
type ModelsResp = { total: number; items: ModelRow[] };
type YearRow = { vehicle_id: number; year: number; submodel: string | null; products: number };

const LIMIT = 50;

function yearSpan(m: { year_min: number | null; year_max: number | null }) {
  if (m.year_min == null) return "no years";
  return m.year_min === m.year_max ? String(m.year_min) : `${m.year_min}–${m.year_max}`;
}

// ---------------------------------------------------------------- add form
function AddModel({ makes, types, onDone }: { makes: Opt[]; types: Opt[]; onDone: () => void }) {
  const [makeId, setMakeId] = useState("");
  const [typeId, setTypeId] = useState("");
  const [name, setName] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (!makeId || !typeId || !name.trim()) return setError("Make, type and name are required");
    if ((from && !to) || (!from && to)) return setError("Enter both years, or neither");
    setBusy(true);
    try {
      await api("/models", {
        method: "POST",
        body: {
          make_id: Number(makeId),
          vehicle_type_id: Number(typeId),
          name: name.trim(),
          year_from: from ? Number(from) : null,
          year_to: to ? Number(to) : null,
        },
      });
      setName("");
      setFrom("");
      setTo("");
      onDone();
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-3 rounded-lg border border-gray-200 bg-white p-4">
      <h2 className="font-semibold">Add a model</h2>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <Field label="Make">
          <select className={inputCls} value={makeId} onChange={(e) => setMakeId(e.target.value)}>
            <option value="">Select make</option>
            {makes.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
          </select>
        </Field>
        <Field label="Machine type">
          <select className={inputCls} value={typeId} onChange={(e) => setTypeId(e.target.value)}>
            <option value="">Select type</option>
            {types.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select>
        </Field>
        <Field label="Model name" className="lg:col-span-1">
          <input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Years from (optional)">
          <input className={inputCls} type="number" value={from} onChange={(e) => setFrom(e.target.value)} />
        </Field>
        <Field label="Years to">
          <input className={inputCls} type="number" value={to} onChange={(e) => setTo(e.target.value)} />
        </Field>
      </div>
      {error && <Notice kind="error">{error}</Notice>}
      <button className={btnCls} disabled={busy}>Add model</button>
    </form>
  );
}

// -------------------------------------------------------------- edit modal
function EditModal({
  model,
  makes,
  types,
  onClose,
  onChanged,
  position,
  canUp,
  canDown,
  onStep,
}: {
  model: ModelRow;
  makes: Opt[];
  types: Opt[];
  onClose: () => void;
  onChanged: () => void;
  position?: string;
  canUp: boolean;
  canDown: boolean;
  onStep: (dir: -1 | 1) => void;
}) {
  const [name, setName] = useState(model.name);
  const [makeId, setMakeId] = useState(String(model.make_id));
  const [typeId, setTypeId] = useState(model.vehicle_type_id ? String(model.vehicle_type_id) : "");
  const [years, setYears] = useState<YearRow[]>([]);
  const [yearsLoaded, setYearsLoaded] = useState(false);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [mergeQ, setMergeQ] = useState("");
  const [mergeResults, setMergeResults] = useState<ModelRow[]>([]);
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const loadYears = useCallback(() => {
    api<YearRow[]>(`/models/${model.id}/years`)
      .then((y) => {
        setYears(y);
        setYearsLoaded(true);
      })
      .catch((e) => setMsg({ kind: "error", text: errMsg(e) }));
  }, [model.id]);
  useEffect(loadYears, [loadYears]);

  async function run(fn: () => Promise<string | void>, refresh = true) {
    setBusy(true);
    setMsg(null);
    try {
      const text = await fn();
      if (text) setMsg({ kind: "success", text });
      if (refresh) onChanged();
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy(false);
    }
  }

  async function removeYear(year: number) {
    await run(async () => {
      try {
        await api(`/models/${model.id}/years/${year}`, { method: "DELETE" });
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) {
          if (!confirm(`${e.message}\n\nRemove ${year} and those product fits anyway?`)) return;
          await api(`/models/${model.id}/years/${year}?force=true`, { method: "DELETE" });
        } else throw e;
      }
      loadYears();
    });
  }

  const hasUnsaved =
    name !== model.name ||
    makeId !== String(model.make_id) ||
    typeId !== (model.vehicle_type_id ? String(model.vehicle_type_id) : "") ||
    from !== "" ||
    to !== "";

  function step(dir: -1 | 1) {
    if (hasUnsaved && !confirm("You have unsaved changes on this model. Leave without saving?")) return;
    onStep(dir);
  }

  const uniqueYears = Array.from(new Map(years.map((y) => [y.year, y])).values());

  return (
    <Modal title={`${model.make} ${model.name}`} onClose={onClose}>
      <div className="space-y-6">
        <ArrowBar
          upLabel="Previous model in the list"
          downLabel="Next model in the list"
          position={position}
          canUp={canUp}
          canDown={canDown}
          busy={busy}
          onUp={() => step(-1)}
          onDown={() => step(1)}
        />
        {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}

        <section className="space-y-3">
          <h3 className="font-medium">Details</h3>
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="Model name"><input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} /></Field>
            <Field label="Make">
              <select className={inputCls} value={makeId} onChange={(e) => setMakeId(e.target.value)}>
                {makes.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
              </select>
            </Field>
            <Field label="Machine type">
              <select className={inputCls} value={typeId} onChange={(e) => setTypeId(e.target.value)}>
                <option value="">(none)</option>
                {types.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
              </select>
            </Field>
          </div>
          <button
            className={btnCls}
            disabled={busy || !name.trim() || !typeId}
            onClick={() =>
              run(async () => {
                await api(`/models/${model.id}`, {
                  method: "PATCH",
                  body: { name: name.trim(), make_id: Number(makeId), vehicle_type_id: Number(typeId) },
                });
                return "Saved";
              })
            }
          >
            Save details
          </button>
        </section>

        <section className="space-y-3">
          <h3 className="font-medium">Years</h3>
          {/* Fixed height, so the window does not grow and shrink while stepping between models. */}
          <div className="flex h-40 flex-wrap content-start gap-2 overflow-y-auto rounded border border-gray-100 bg-white p-2">
            {!yearsLoaded && <span className="text-sm text-gray-400">Loading years…</span>}
            {uniqueYears.map((y) => (
              <span key={y.year} className="inline-flex items-center gap-1 rounded border border-gray-300 bg-gray-50 px-2 py-1 text-sm">
                {y.year}
                {y.products > 0 && <span className="text-xs text-gray-400">({y.products})</span>}
                <button type="button" className="text-gray-400 hover:text-red-600" onClick={() => removeYear(y.year)} disabled={busy} aria-label={`Remove ${y.year}`}>
                  ×
                </button>
              </span>
            ))}
            {yearsLoaded && uniqueYears.length === 0 && <span className="text-sm text-gray-500">No years yet.</span>}
          </div>
          <div className="flex flex-wrap items-end gap-2">
            <Field label="Add years from"><input className={`${inputCls} w-28`} type="number" value={from} onChange={(e) => setFrom(e.target.value)} /></Field>
            <Field label="to"><input className={`${inputCls} w-28`} type="number" value={to} onChange={(e) => setTo(e.target.value)} /></Field>
            <button
              className={btn2Cls}
              disabled={busy || !from || !to}
              onClick={() =>
                run(async () => {
                  const r = await api<{ years_added: number }>(`/models/${model.id}/years`, {
                    method: "POST",
                    body: { year_from: Number(from), year_to: Number(to) },
                  });
                  setFrom("");
                  setTo("");
                  loadYears();
                  return `${r.years_added} year(s) added`;
                })
              }
            >
              Add range
            </button>
          </div>
          <p className="text-xs text-gray-500">Number in brackets = products that fit that year.</p>
        </section>

        <section className="space-y-3">
          <h3 className="font-medium">Merge into another model</h3>
          <p className="text-xs text-gray-500">
            Moves this model&apos;s years and product fitment into the model you pick, then deletes this one.
          </p>
          <form
            className="flex gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              api<ModelsResp>(`/models?q=${encodeURIComponent(mergeQ)}&limit=10`)
                .then((r) => setMergeResults(r.items.filter((m) => m.id !== model.id)))
                .catch((err) => setMsg({ kind: "error", text: errMsg(err) }));
            }}
          >
            <input className={inputCls} placeholder="Search target model name" value={mergeQ} onChange={(e) => setMergeQ(e.target.value)} />
            <button className={btn2Cls}>Search</button>
          </form>
          <ul className="divide-y divide-gray-100 text-sm">
            {mergeResults.map((m) => (
              <li key={m.id} className="flex items-center justify-between py-2">
                <span>{m.make} {m.name} <span className="text-gray-400">({yearSpan(m)})</span></span>
                <button
                  className={dangerCls}
                  disabled={busy}
                  onClick={() => {
                    if (!confirm(`Merge "${model.make} ${model.name}" into "${m.make} ${m.name}"? This cannot be undone.`)) return;
                    run(async () => {
                      await api(`/models/${model.id}/merge`, { method: "POST", body: { into_id: m.id } });
                      onClose();
                    });
                  }}
                >
                  Merge into this
                </button>
              </li>
            ))}
          </ul>
        </section>

        <section className="border-t border-gray-200 pt-4">
          <button
            className={dangerCls}
            disabled={busy}
            onClick={() => {
              if (!confirm(`Delete "${model.make} ${model.name}" and all its years?`)) return;
              run(async () => {
                await api(`/models/${model.id}`, { method: "DELETE" });
                onClose();
              });
            }}
          >
            Delete model
          </button>
        </section>
      </div>
    </Modal>
  );
}

// -------------------------------------------------------------------- page
export default function ModelsPage() {
  const [makes, setMakes] = useState<Opt[]>([]);
  const [types, setTypes] = useState<Opt[]>([]);
  const [makeId, setMakeId] = useState("");
  const [typeId, setTypeId] = useState("");
  const [untyped, setUntyped] = useState(false);
  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<ModelsResp>({ total: 0, items: [] });
  const [editing, setEditing] = useState<ModelRow | null>(null);
  const [error, setError] = useState("");
  const [ready, setReady] = useState(false);

  // ?untyped=1 (from the dashboard) pre-ticks "Needs a type".
  useEffect(() => {
    if (new URLSearchParams(window.location.search).get("untyped") === "1") setUntyped(true);
    setReady(true);
    api<Opt[]>("/makes").then(setMakes).catch((e) => setError(errMsg(e)));
    api<Opt[]>("/vehicle-types").then(setTypes).catch((e) => setError(errMsg(e)));
  }, []);

  const load = useCallback(() => {
    if (!ready) return;
    const p = new URLSearchParams({ limit: String(LIMIT), offset: String(offset) });
    if (makeId) p.set("make_id", makeId);
    if (typeId) p.set("type_id", typeId);
    if (untyped) p.set("untyped", "true");
    if (q) p.set("q", q);
    api<ModelsResp>(`/models?${p}`)
      .then((d) => {
        setData(d);
        setError("");
      })
      .catch((e) => setError(errMsg(e)));
  }, [ready, makeId, typeId, untyped, q, offset]);
  useEffect(load, [load]);

  function filterChange(fn: () => void) {
    fn();
    setOffset(0);
  }

  // ---- up / down arrows inside the edit window
  const pendingPick = useRef<"first" | "last" | null>(null);
  const editIdx = editing ? data.items.findIndex((i) => i.id === editing.id) : -1;
  const editPos = editIdx >= 0 ? offset + editIdx : -1; // 0-based place in the whole list
  const canUp = editPos > 0;
  const canDown = editPos >= 0 && editPos < data.total - 1;

  function stepModel(dir: -1 | 1) {
    if (editIdx < 0) return;
    const j = editIdx + dir;
    if (j >= 0 && j < data.items.length) {
      setEditing(data.items[j]);
    } else if (dir > 0 && offset + LIMIT < data.total) {
      pendingPick.current = "first";
      setOffset(offset + LIMIT);
    } else if (dir < 0 && offset > 0) {
      pendingPick.current = "last";
      setOffset(Math.max(0, offset - LIMIT));
    }
  }

  // After the neighbouring page has loaded, open its first (or last) model.
  useEffect(() => {
    if (!pendingPick.current || data.items.length === 0) return;
    const pick = pendingPick.current === "first" ? data.items[0] : data.items[data.items.length - 1];
    pendingPick.current = null;
    setEditing(pick);
  }, [data]);

  async function setType(m: ModelRow, value: string) {
    try {
      await api(`/models/${m.id}`, { method: "PATCH", body: { vehicle_type_id: Number(value) } });
      const t = types.find((x) => x.id === Number(value));
      setData((d) => ({
        ...d,
        items: d.items.map((i) =>
          i.id === m.id ? { ...i, vehicle_type_id: Number(value), vehicle_type: t?.name ?? null } : i
        ),
      }));
    } catch (e) {
      setError(errMsg(e));
    }
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Models &amp; years</h1>
      {error && <Notice kind="error">{error}</Notice>}

      <AddModel makes={makes} types={types} onDone={load} />

      <div className="flex flex-wrap items-end gap-3">
        <Field label="Make">
          <select className={inputCls} value={makeId} onChange={(e) => filterChange(() => setMakeId(e.target.value))}>
            <option value="">All makes</option>
            {makes.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
          </select>
        </Field>
        <Field label="Type">
          <select className={inputCls} value={typeId} onChange={(e) => filterChange(() => setTypeId(e.target.value))}>
            <option value="">All types</option>
            {types.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select>
        </Field>
        <form className="flex items-end gap-2" onSubmit={(e) => { e.preventDefault(); filterChange(() => setQ(qInput.trim())); }}>
          <Field label="Search"><input className={inputCls} value={qInput} onChange={(e) => setQInput(e.target.value)} placeholder="Model name" /></Field>
          <button className={btn2Cls}>Search</button>
        </form>
        <label className="flex items-center gap-2 pb-2 text-sm">
          <input type="checkbox" checked={untyped} onChange={(e) => filterChange(() => setUntyped(e.target.checked))} />
          Needs a type
        </label>
      </div>

      <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white">
        <table className="w-full text-left text-sm">
          <thead className="bg-gray-50 text-xs uppercase text-gray-500">
            <tr>
              <th className="px-3 py-2">Make</th>
              <th className="px-3 py-2">Model</th>
              <th className="px-3 py-2">Type</th>
              <th className="px-3 py-2">Years</th>
              <th className="px-3 py-2">Products</th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {data.items.map((m) => (
              <tr key={m.id}>
                <td className="px-3 py-2">{m.make}</td>
                <td className="px-3 py-2 font-medium">{m.name}</td>
                <td className="px-3 py-2">
                  <select
                    className={`${inputCls} ${m.vehicle_type_id ? "" : "border-amber-400"}`}
                    value={m.vehicle_type_id ?? ""}
                    onChange={(e) => e.target.value && setType(m, e.target.value)}
                  >
                    {!m.vehicle_type_id && <option value="">Choose type…</option>}
                    {types.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
                  </select>
                </td>
                <td className="px-3 py-2">{m.year_min == null ? <Badge tone="amber">no years</Badge> : yearSpan(m)}</td>
                <td className="px-3 py-2">{m.products}</td>
                <td className="px-3 py-2 text-right">
                  <button className={btn2Cls} onClick={() => setEditing(m)}>Edit</button>
                </td>
              </tr>
            ))}
            {data.items.length === 0 && (
              <tr><td colSpan={6} className="px-3 py-6 text-center text-gray-500">No models match.</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <Pager total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />

      {editing && (
        <EditModal
          key={editing.id}
          model={editing}
          makes={makes}
          types={types}
          onClose={() => setEditing(null)}
          onChanged={load}
          position={editPos >= 0 ? `${editPos + 1} of ${data.total}` : undefined}
          canUp={canUp}
          canDown={canDown}
          onStep={stepModel}
        />
      )}
    </div>
  );
}
