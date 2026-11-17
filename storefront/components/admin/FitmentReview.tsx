"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Notice, btnCls, btn2Cls, inputCls } from "./ui";

/* Review what an eBay compatibility table would add to ONE product's website fitment, settle the machines our vehicle list does not
   know, then add only what was ticked. This is its own step: it does not need a listing to be made, and it never removes anything.

   1. Preview  - matches the eBay makes, models and years to ours and shows what is NEW for the product (nothing is written).
   2. Settle   - for a machine we do not have: use one of our models, create the make / model (names are editable), or skip it.
   3. Commit   - adds the years that are ticked, and only years that are still new. */

type Src = "listing" | "own" | "both";
type Suggestion = { model_id: number; name: string; make: string; score: number };
type FitModel = { model_id: number; make: string; model: string; ebay_names: string[]; new_years: number[]; already_years: number[] };
type Unknown = { make: string; model: string; years: number[]; subs: string[]; make_exists: boolean; ambiguous: boolean; suggestions: Suggestion[] };
type Plan = {
  product: string;
  rows_read: number;
  no_year: number;
  skipped: string[];
  models: FitModel[];
  unresolved: Unknown[];
  from_listing_rows?: number;
  own_listings_with_table?: number;
  own_listings?: number;
};
type Dec = { mode: "later" | "map" | "create" | "skip"; model_id: number | null; label: string; make: string; model: string; type_id: string };
type PickedModel = { id: number; name: string; make: string };

const ukey = (u: { make: string; model: string }) => `${u.make}|${u.model}`;

function yearsText(ys: number[]): string {
  if (ys.length === 0) return "none";
  const s = [...ys].sort((a, b) => a - b);
  const out: string[] = [];
  let a = s[0];
  let b = s[0];
  for (const y of s.slice(1)) {
    if (y === b + 1) b = y;
    else {
      out.push(a === b ? `${a}` : `${a}–${b}`);
      a = b = y;
    }
  }
  out.push(a === b ? `${a}` : `${a}–${b}`);
  return out.join(", ");
}

function ModelPicker({ onPick }: { onPick: (m: PickedModel) => void }) {
  const [q, setQ] = useState("");
  const [items, setItems] = useState<PickedModel[]>([]);
  const [busy, setBusy] = useState(false);
  const seq = useRef(0);

  useEffect(() => {
    if (q.trim().length < 2) {
      setItems([]);
      return;
    }
    const n = ++seq.current;
    setBusy(true);
    const t = setTimeout(() => {
      api<{ items: PickedModel[] }>(`/ebay-compat/fitment/models?q=${encodeURIComponent(q.trim())}&limit=15`)
        .then((r) => n === seq.current && setItems(r.items))
        .catch(() => n === seq.current && setItems([]))
        .finally(() => n === seq.current && setBusy(false));
    }, 250);
    return () => clearTimeout(t);
  }, [q]);

  return (
    <div className="space-y-1">
      <input className={`${inputCls} w-72 max-w-full`} placeholder="Search our models (e.g. raptor 700)" value={q} onChange={(e) => setQ(e.target.value)} />
      {busy && <p className="text-xs text-gray-500">Searching…</p>}
      {items.length > 0 && (
        <ul className="max-h-48 w-72 max-w-full overflow-auto rounded border border-gray-200 bg-white text-xs">
          {items.map((m) => (
            <li key={m.id}>
              <button type="button" className="block w-full px-2 py-1 text-left hover:bg-gray-50" onClick={() => { onPick(m); setQ(""); setItems([]); }}>
                {m.make} {m.name}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function FitmentReview({ product, itemId, store }: { product: { id: number; title: string; listings: number }; itemId: string | null; store: string }) {
  const canListing = !!itemId && !!store;
  const [src, setSrc] = useState<Src>(canListing ? "listing" : "own");
  const [plan, setPlan] = useState<Plan | null>(null);
  const [sel, setSel] = useState<Record<number, number[]>>({});
  const [dec, setDec] = useState<Record<string, Dec>>({});
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ kind: "error" | "success" | "info"; text: string } | null>(null);
  const [makes, setMakes] = useState<string[]>([]);
  const [types, setTypes] = useState<{ id: number; name: string }[]>([]);
  const loadedLists = useRef(false);

  const preview = useCallback(
    async (s: Src, keepMsg = false) => {
      setBusy("preview");
      if (!keepMsg) setMsg(null);
      setSrc(s);
      try {
        const p = await api<Plan>("/ebay-lookup/fitment/preview", {
          method: "POST",
          body: { product_id: product.id, item_id: s === "own" ? null : itemId, store: store || null, from_listings: s !== "listing" },
        });
        setPlan(p);
        setSel(Object.fromEntries(p.models.filter((m) => m.new_years.length).map((m) => [m.model_id, [...m.new_years]])));
        setDec(Object.fromEntries(p.unresolved.map((u) => [ukey(u), { mode: "later", model_id: null, label: "", make: u.make, model: u.model, type_id: "" } as Dec])));
      } catch (e) {
        setPlan(null);
        setMsg({ kind: "error", text: errMsg(e) });
      } finally {
        setBusy("");
      }
    },
    [product.id, itemId, store]
  );

  // the review starts by itself when the product is chosen (the parent re-mounts this for each product)
  useEffect(() => {
    if (canListing || product.listings > 0) void preview(canListing ? "listing" : "own");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // makes and vehicle types are only needed when there is a machine to settle
  useEffect(() => {
    if (!plan || plan.unresolved.length === 0 || loadedLists.current) return;
    loadedLists.current = true;
    api<{ items: { name: string }[] }>("/ebay-compat/fitment/makes").then((r) => setMakes(r.items.map((i) => i.name))).catch(() => {});
    api<{ id: number; name: string }[]>("/vehicle-types").then((r) => setTypes(r.map((t) => ({ id: t.id, name: t.name })))).catch(() => {});
  }, [plan]);

  const setD = (u: Unknown, patch: Partial<Dec>) => setDec((d) => ({ ...d, [ukey(u)]: { ...d[ukey(u)], ...patch } }));

  const picked = Object.values(sel).reduce((n, ys) => n + ys.length, 0);
  const pickedModels = Object.values(sel).filter((ys) => ys.length > 0).length;
  const totalNew = plan ? plan.models.reduce((n, m) => n + m.new_years.length, 0) : 0;

  function toggleYear(m: FitModel, y: number) {
    setSel((s) => {
      const cur = s[m.model_id] ?? [];
      return { ...s, [m.model_id]: cur.includes(y) ? cur.filter((x) => x !== y) : [...cur, y].sort((a, b) => a - b) };
    });
  }
  const toggleModel = (m: FitModel) => setSel((s) => ({ ...s, [m.model_id]: (s[m.model_id] ?? []).length === m.new_years.length ? [] : [...m.new_years] }));
  const setAll = (on: boolean) => plan && setSel(Object.fromEntries(plan.models.filter((m) => m.new_years.length).map((m) => [m.model_id, on ? [...m.new_years] : []])));

  const settled = plan ? plan.unresolved.filter((u) => dec[ukey(u)]?.mode && dec[ukey(u)].mode !== "later") : [];
  const incomplete = (u: Unknown) => {
    const d = dec[ukey(u)];
    if (!d) return false;
    if (d.mode === "map") return !d.model_id;
    if (d.mode === "create") return !d.make.trim() || !d.model.trim();
    return false;
  };

  async function saveDecisions() {
    if (!plan || settled.length === 0) return;
    if (settled.some(incomplete)) {
      setMsg({ kind: "error", text: "Finish the machines marked in red: choose a model to use, or fill in the make and model to create." });
      return;
    }
    const creating = settled.filter((u) => dec[ukey(u)].mode === "create").length;
    if (creating > 0 && !window.confirm(`Create ${creating} new model${creating === 1 ? "" : "s"} (and any make that is missing) on the website? Nothing is added to the product yet.`)) return;
    setBusy("settle");
    setMsg(null);
    try {
      const r = await api<{ mapped: number; created: number; skipped: number }>("/ebay-lookup/fitment/settle", {
        method: "POST",
        body: {
          decisions: settled.map((u) => {
            const d = dec[ukey(u)];
            return {
              ebay_make: u.make,
              ebay_model: u.model,
              action: d.mode,
              model_id: d.mode === "map" ? d.model_id : null,
              make: d.mode === "create" ? d.make.trim() : "",
              model: d.mode === "create" ? d.model.trim() : "",
              type_id: d.mode === "create" && d.type_id ? Number(d.type_id) : null,
            };
          }),
        },
      });
      setMsg({ kind: "success", text: `Saved: ${r.mapped} matched to an existing model, ${r.created} created, ${r.skipped} skipped. They now appear below with their years, ready to tick.` });
      await preview(src, true);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
      setBusy("");
    }
  }

  async function commit() {
    if (!plan || picked === 0) return;
    if (!window.confirm(`Add ${picked} ticked year${picked === 1 ? "" : "s"} across ${pickedModels} model${pickedModels === 1 ? "" : "s"} to the website fitment of "${product.title}"? Only additions are made; nothing is removed.`)) return;
    setBusy("commit");
    setMsg(null);
    try {
      const r = await api<{ models: number; links_added: number; vehicle_years_added: number }>("/ebay-lookup/fitment/commit", {
        method: "POST",
        body: {
          product_id: product.id,
          item_id: src === "own" ? null : itemId,
          store: store || null,
          from_listings: src !== "listing",
          picks: Object.entries(sel).filter(([, ys]) => ys.length > 0).map(([id, years]) => ({ model_id: Number(id), years })),
        },
      });
      setMsg({
        kind: "success",
        text: `Added ${r.links_added} year${r.links_added === 1 ? "" : "s"} across ${r.models} model${r.models === 1 ? "" : "s"} to the product's fitment${r.vehicle_years_added ? `, including ${r.vehicle_years_added} year${r.vehicle_years_added === 1 ? "" : "s"} that were new to the model itself` : ""}.`,
      });
      await preview(src, true);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
      setBusy("");
    }
  }

  const srcBtn = (s: Src, label: string, off: boolean, why: string) => (
    <button type="button" className={`${btn2Cls} ${src === s && plan ? "border-black bg-gray-100" : ""}`} disabled={busy !== "" || off} title={off ? why : ""} onClick={() => void preview(s)}>
      {label}
    </button>
  );

  return (
    <div className="space-y-3 text-sm">
      <div className="flex flex-wrap gap-2">
        {srcBtn("listing", "From this listing", !canListing, "Read a listing and choose a store first")}
        {srcBtn("own", "From this product's own eBay listings", product.listings === 0, "This product has no eBay listings of its own")}
        {srcBtn("both", "From both", !canListing || product.listings === 0, "Needs a listing read and a product with eBay listings")}
      </div>
      {busy === "preview" && <p className="text-gray-500">Matching the eBay vehicles to ours…</p>}
      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}

      {plan && (
        <div className="space-y-4">
          <p className="text-gray-700">
            {plan.rows_read.toLocaleString()} eBay row{plan.rows_read === 1 ? "" : "s"} read
            {plan.from_listing_rows !== undefined ? ` (${plan.from_listing_rows} from this listing)` : ""}
            {plan.own_listings !== undefined ? `; ${plan.own_listings_with_table} of ${plan.own_listings} of the product's own listings have a saved table` : ""}.
            {plan.no_year > 0 ? ` ${plan.no_year} row${plan.no_year === 1 ? " had" : "s had"} no usable year and were left out.` : ""}
          </p>

          {plan.unresolved.length > 0 && (
            <div className="space-y-3 rounded border border-amber-300 bg-amber-50 p-3">
              <div>
                <h4 className="font-semibold">{plan.unresolved.length} machine{plan.unresolved.length === 1 ? "" : "s"} not in our vehicle list</h4>
                <p className="text-xs text-gray-600">
                  Decide each one, edit the names if you are creating it, then save. Nothing is added to the product by this step, and a machine you leave on “Decide later” is not touched.
                  Decisions are remembered, so the eBay compatibility import uses them too.
                </p>
              </div>
              {plan.unresolved.map((u) => {
                const d = dec[ukey(u)];
                if (!d) return null;
                const bad = incomplete(u);
                return (
                  <div key={ukey(u)} className={`space-y-2 rounded border bg-white p-3 ${bad ? "border-red-400" : "border-gray-200"}`}>
                    <div className="flex flex-wrap items-baseline justify-between gap-2">
                      <span className="font-medium">{u.make} {u.model} <span className="font-normal text-gray-500">(as eBay spells it)</span></span>
                      <span className="text-xs text-gray-500">{u.years.length} year{u.years.length === 1 ? "" : "s"}: {yearsText(u.years)}</span>
                    </div>
                    {u.subs.length > 0 && <p className="text-xs text-gray-500">eBay submodels: {u.subs.join(", ")}</p>}
                    {u.ambiguous && <p className="text-xs text-amber-800">More than one of our models has this name, so choose which one it is.</p>}
                    {!u.make_exists && <p className="text-xs text-amber-800">We have no make called “{u.make}” either, so creating this also creates the make.</p>}
                    <div className="flex flex-wrap gap-2 text-xs">
                      {([["later", "Decide later"], ["map", "Use one of our models"], ["create", "Create it"], ["skip", "Never add this"]] as const).map(([m, label]) => (
                        <button
                          key={m}
                          type="button"
                          className={`rounded border px-2 py-1 ${d.mode === m ? "border-black bg-black text-white" : "border-gray-300 bg-white"}`}
                          onClick={() => setD(u, { mode: m })}
                        >
                          {label}
                        </button>
                      ))}
                    </div>
                    {d.mode === "map" && (
                      <div className="space-y-2">
                        {u.suggestions.length > 0 && (
                          <div className="flex flex-wrap items-center gap-2 text-xs">
                            <span className="text-gray-600">Closest of ours:</span>
                            {u.suggestions.map((s) => (
                              <button key={s.model_id} type="button" className={`rounded border px-2 py-1 ${d.model_id === s.model_id ? "border-black bg-gray-100" : "border-gray-300"}`}
                                onClick={() => setD(u, { model_id: s.model_id, label: `${s.make} ${s.name}` })}>
                                {s.make} {s.name}
                              </button>
                            ))}
                          </div>
                        )}
                        <ModelPicker onPick={(m) => setD(u, { model_id: m.id, label: `${m.make} ${m.name}` })} />
                        <p className={`text-xs ${d.model_id ? "text-green-700" : "text-red-700"}`}>{d.model_id ? `Will use: ${d.label}` : "Choose a model."}</p>
                      </div>
                    )}
                    {d.mode === "create" && (
                      <div className="flex flex-wrap items-end gap-3">
                        <label className="block text-xs font-medium">
                          <span className="mb-1 block">Make</span>
                          <input className={`${inputCls} w-44`} list="fit-makes" value={d.make} onChange={(e) => setD(u, { make: e.target.value })} />
                        </label>
                        <label className="block text-xs font-medium">
                          <span className="mb-1 block">Model</span>
                          <input className={`${inputCls} w-56`} value={d.model} onChange={(e) => setD(u, { model: e.target.value })} />
                        </label>
                        <label className="block text-xs font-medium">
                          <span className="mb-1 block">Vehicle type (new models)</span>
                          <select className={`${inputCls} w-44`} value={d.type_id} onChange={(e) => setD(u, { type_id: e.target.value })}>
                            <option value="">None</option>
                            {types.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
                          </select>
                        </label>
                      </div>
                    )}
                    {d.mode === "skip" && <p className="text-xs text-gray-600">These rows are left out now and every time the conversion sees this machine.</p>}
                  </div>
                );
              })}
              <datalist id="fit-makes">{makes.map((m) => <option key={m} value={m} />)}</datalist>
              <div className="flex flex-wrap items-center gap-3">
                <button type="button" className={btnCls} disabled={busy !== "" || settled.length === 0} onClick={() => void saveDecisions()}>
                  {busy === "settle" ? "Saving…" : `Save ${settled.length || ""} decision${settled.length === 1 ? "" : "s"}`}
                </button>
                {plan.unresolved.some((u) => u.suggestions[0] && dec[ukey(u)]?.mode === "later") && (
                  <button
                    type="button"
                    className="text-xs text-blue-700 hover:underline"
                    onClick={() => plan.unresolved.forEach((u) => { const s = u.suggestions[0]; if (s && dec[ukey(u)]?.mode === "later") setD(u, { mode: "map", model_id: s.model_id, label: `${s.make} ${s.name}` }); })}
                  >
                    Fill each with its closest model (still needs saving)
                  </button>
                )}
              </div>
            </div>
          )}

          {plan.models.length === 0 && plan.unresolved.length === 0 && <p className="text-gray-500">Nothing to add: no row matched a vehicle.</p>}

          {plan.models.length > 0 && (
            <div className="space-y-2">
              <div className="flex flex-wrap items-center gap-3">
                <h4 className="font-semibold">Years to add ({totalNew} new)</h4>
                {totalNew > 0 && (
                  <>
                    <button type="button" className="text-xs text-blue-700 hover:underline" onClick={() => setAll(true)}>Tick all</button>
                    <button type="button" className="text-xs text-blue-700 hover:underline" onClick={() => setAll(false)}>Untick all</button>
                  </>
                )}
              </div>
              <div className="max-h-[28rem] overflow-auto rounded border border-gray-200">
                <table className="w-full text-left text-xs">
                  <thead className="sticky top-0 bg-gray-50 text-gray-500">
                    <tr>
                      <th className="w-8 px-2 py-1" />
                      <th className="px-2 py-1">Our model</th>
                      <th className="px-2 py-1">New years (click a year to untick it)</th>
                      <th className="px-2 py-1">Already on the product</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100">
                    {plan.models.map((m) => {
                      const on = sel[m.model_id] ?? [];
                      return (
                        <tr key={m.model_id} className="align-top">
                          <td className="px-2 py-1">
                            {m.new_years.length > 0 && <input type="checkbox" aria-label={`All new years for ${m.make} ${m.model}`} checked={on.length === m.new_years.length} onChange={() => toggleModel(m)} />}
                          </td>
                          <td className="px-2 py-1">
                            {m.make} {m.model}
                            {m.ebay_names.some((n) => n.toLowerCase() !== `${m.make} ${m.model}`.toLowerCase()) && <span className="block text-gray-500">eBay: {m.ebay_names.join(", ")}</span>}
                          </td>
                          <td className="px-2 py-1">
                            {m.new_years.length === 0 ? (
                              <span className="text-gray-400">none, all already there</span>
                            ) : (
                              <span className="flex flex-wrap gap-1">
                                {m.new_years.map((y) => (
                                  <button key={y} type="button" className={`rounded border px-1.5 py-0.5 ${on.includes(y) ? "border-green-700 bg-green-50 font-semibold text-green-800" : "border-gray-300 bg-white text-gray-400 line-through"}`} onClick={() => toggleYear(m, y)}>
                                    {y}
                                  </button>
                                ))}
                              </span>
                            )}
                          </td>
                          <td className="px-2 py-1 text-gray-500">{yearsText(m.already_years)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              {plan.skipped.length > 0 && <p className="text-xs text-gray-500">Set to “never add”: {plan.skipped.join(", ")}.</p>}
              {totalNew > 0 && (
                <button type="button" className={btnCls} disabled={busy !== "" || picked === 0} onClick={() => void commit()}>
                  {busy === "commit" ? "Adding…" : `Add ${picked} ticked year${picked === 1 ? "" : "s"} to the product's fitment`}
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
