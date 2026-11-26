"use client";

import { useEffect, useMemo, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Badge, Notice, btnCls, btn2Cls, dangerCls } from "./ui";

/* Kit fitment: the machines and years that EVERY part of a kit fits.
   The fitment of a part is the fitment of our own single-part product(s) for that same distributor part.
   1. Compare  - the server finds the overlap and lists it one machine at a time (nothing is written).
   2. Review   - each machine is shown with its years: accept (untick any year you don't want) or reject.
   3. Finish   - only the accepted years are added to the kit; existing fitment is never removed. */

type Year = { vehicle_id: number; year: number };
type Machine = { model_id: number; make: string; model: string; vehicle_type: string | null; years: Year[] };
type Part = {
  line_id: number; part_number: string; description: string | null;
  status: "ok" | "universal" | "no_fitment" | "no_product"; vehicles: number;
  products: { id: number; title: string; sku: string | null }[];
};
type Compare = { parts: Part[]; blocked: string | null; models: Machine[]; already_present: number; common_vehicles: number };
type Decision = { accepted: boolean; years: Set<number> }; // years = vehicle_ids kept

const STATUS_TEXT: Record<Part["status"], string> = {
  ok: "fitment found",
  universal: "universal (fits everything)",
  no_fitment: "its product has no fitment",
  no_product: "no single-part product matches",
};

export default function KitFitmentReview({ productId, onClose, onDone }: { productId: number; onClose: () => void; onDone: () => void }) {
  const [data, setData] = useState<Compare | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [i, setI] = useState(0);
  const [dec, setDec] = useState<Record<number, Decision>>({});
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api<Compare>(`/fitment-research/kits/${productId}/compare`)
      .then((r) => live && setData(r))
      .catch((e) => live && setErr(errMsg(e)));
    return () => {
      live = false;
    };
  }, [productId]);

  const models = data?.models ?? [];
  const finished = data !== null && models.length > 0 && i >= models.length;
  const cur = models[i];

  const keptIds = useMemo(
    () => models.flatMap((m) => (dec[m.model_id]?.accepted ? [...dec[m.model_id].years] : [])),
    [models, dec]
  );
  const acceptedModels = models.filter((m) => dec[m.model_id]?.accepted && dec[m.model_id].years.size > 0).length;

  // the years ticked for the machine on screen (all of them until the person unticks some)
  const shown = (m: Machine): Set<number> => dec[m.model_id]?.years ?? new Set(m.years.map((y) => y.vehicle_id));

  function toggleYear(m: Machine, vid: number) {
    const next = new Set(shown(m));
    if (next.has(vid)) next.delete(vid);
    else next.add(vid);
    setDec((d) => ({ ...d, [m.model_id]: { accepted: d[m.model_id]?.accepted ?? false, years: next } }));
  }
  function decide(m: Machine, accepted: boolean) {
    setDec((d) => ({ ...d, [m.model_id]: { accepted, years: shown(m) } }));
    setI((n) => n + 1);
  }

  async function finish() {
    setBusy(true);
    setErr(null);
    try {
      const r = await api<{ added: number; ignored: number; already_present: number }>(`/fitment-research/kits/${productId}/apply`, {
        method: "POST",
        body: { vehicle_ids: keptIds },
      });
      setResult(
        `${r.added} model year${r.added === 1 ? "" : "s"} added to this kit` +
          (r.already_present ? `, ${r.already_present} already there` : "") +
          (r.ignored ? `, ${r.ignored} skipped (a part no longer fits them)` : "") +
          "."
      );
      onDone();
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  const yearsText = (ys: number[]) => {
    const s = [...ys].sort((a, b) => a - b);
    const out: string[] = [];
    let a = s[0], b = s[0];
    for (const y of s.slice(1)) {
      if (y === b + 1) b = y;
      else {
        out.push(a === b ? `${a}` : `${a}–${b}`);
        a = b = y;
      }
    }
    if (s.length) out.push(a === b ? `${a}` : `${a}–${b}`);
    return out.join(", ");
  };

  return (
    <div className="space-y-3 rounded border border-gray-300 bg-gray-50 p-4">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-gray-800">Compare the fitment of this kit&apos;s parts</h3>
        <button type="button" className={btn2Cls} onClick={onClose}>Close</button>
      </div>

      {err && <Notice kind="error">{err}</Notice>}
      {!data && !err && <p className="text-sm text-gray-500">Comparing…</p>}

      {data && (
        <ul className="space-y-1 text-xs text-gray-600">
          {data.parts.map((p) => (
            <li key={p.line_id}>
              <span className="font-mono">{p.part_number}</span>
              {p.description ? ` ${p.description}` : ""} –{" "}
              <span className={p.status === "no_fitment" || p.status === "no_product" ? "text-red-700" : ""}>
                {STATUS_TEXT[p.status]}
                {p.status === "ok" ? ` (${p.vehicles} model years)` : ""}
              </span>
              {p.products.length > 0 && <span className="text-gray-400"> · {p.products.map((x) => x.sku || `#${x.id}`).join(", ")}</span>}
            </li>
          ))}
        </ul>
      )}

      {data?.blocked && <Notice kind="info">{data.blocked}</Notice>}

      {data && !data.blocked && models.length === 0 && (
        <Notice kind="info">
          {data.common_vehicles === 0
            ? "No machine and year is fitted by every part, so there is nothing to add."
            : `All ${data.common_vehicles} shared model years are already on this kit. Nothing new to review.`}
        </Notice>
      )}

      {result && <Notice kind="success">{result}</Notice>}

      {data && !data.blocked && models.length > 0 && !result && !finished && cur && (
        <div className="space-y-3 rounded border border-gray-200 bg-white p-3">
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-gray-500">
            <span>Machine {i + 1} of {models.length}</span>
            <span>{data.already_present ? `${data.already_present} shared years already on the kit` : ""}</span>
          </div>
          <div className="text-base font-semibold">
            {cur.make} {cur.model} {cur.vehicle_type && <Badge>{cur.vehicle_type}</Badge>}
          </div>
          <p className="text-xs text-gray-500">Every part fits these years. Untick any year you don&apos;t want.</p>
          <div className="flex flex-wrap gap-2">
            {cur.years.map((y) => {
              const on = shown(cur).has(y.vehicle_id);
              return (
                <label key={y.vehicle_id} className={`cursor-pointer rounded border px-2 py-1 text-sm ${on ? "border-black bg-black text-white" : "border-gray-300 bg-white text-gray-400 line-through"}`}>
                  <input type="checkbox" className="sr-only" checked={on} onChange={() => toggleYear(cur, y.vehicle_id)} />
                  {y.year}
                </label>
              );
            })}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <button type="button" className={btnCls} disabled={shown(cur).size === 0} onClick={() => decide(cur, true)}>
              Accept {shown(cur).size === cur.years.length ? "all years" : `${shown(cur).size} year${shown(cur).size === 1 ? "" : "s"}`}
            </button>
            <button type="button" className={dangerCls} onClick={() => decide(cur, false)}>Reject</button>
            <button type="button" className={btn2Cls} disabled={i === 0} onClick={() => setI((n) => Math.max(0, n - 1))}>Back</button>
          </div>
        </div>
      )}

      {finished && !result && (
        <div className="space-y-3 rounded border border-gray-200 bg-white p-3">
          <p className="text-sm">
            Reviewed {models.length} machine{models.length === 1 ? "" : "s"}: <b>{acceptedModels}</b> accepted ({keptIds.length} model year{keptIds.length === 1 ? "" : "s"}), {models.length - acceptedModels} rejected.
          </p>
          <ul className="max-h-48 overflow-auto text-xs text-gray-700">
            {models.filter((m) => dec[m.model_id]?.accepted && dec[m.model_id].years.size > 0).map((m) => (
              <li key={m.model_id}>{m.make} {m.model}: {yearsText(m.years.filter((y) => dec[m.model_id].years.has(y.vehicle_id)).map((y) => y.year))}</li>
            ))}
          </ul>
          <div className="flex flex-wrap gap-2">
            <button type="button" className={btnCls} disabled={busy || keptIds.length === 0} onClick={() => void finish()}>
              {busy ? "Adding…" : `Add ${keptIds.length} model year${keptIds.length === 1 ? "" : "s"} to this kit`}
            </button>
            <button type="button" className={btn2Cls} disabled={busy} onClick={() => setI(0)}>Review again</button>
          </div>
        </div>
      )}
    </div>
  );
}
