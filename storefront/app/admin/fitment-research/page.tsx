"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { getSupabase } from "../../../lib/supabase";
import { Badge, Notice, btnCls, btn2Cls, inputCls } from "../../../components/admin/ui";

type Row = { id: number; product_id: number; origin: string; verdict: string | null; name: string; sku: string; items: number };
type Item = {
  id: number; make_text: string | null; model_text: string | null; make_id: number | null; model_id: number | null;
  year_from: number | null; year_to: number | null; years_note: string; source_kind: string; source_ref: string | null;
  evidence: string | null; confidence: "high" | "medium" | "low";
};
type Detail = { proposal: { id: number; verdict: string; notes: string } | null; items: Item[] };
type Pdf = { id: number; name: string; brand: string; status: string; pages: number; pages_done: number; error: string | null };

type Found = { id: number; title: string; sku: string; mpn: string | null; fitment_rows: number };
type VType = { id: number; name: string; slug: string };
type PModel = { model: string; years: number[]; model_exists: boolean; new_years: number; similar: string[]; type_id: number | null };
type PMake = { make: string; make_exists: boolean; models: PModel[] };
type Preview = {
  makes: PMake[]; warnings: string[];
  totals: { makes: number; new_makes: number; models: number; new_models: number; model_years: number };
};

const yearsText = (ys: number[]) => {
  const out: string[] = [];
  for (let i = 0; i < ys.length; ) {
    let j = i;
    while (j + 1 < ys.length && ys[j + 1] === ys[j] + 1) j++;
    out.push(j === i ? String(ys[i]) : `${ys[i]}–${ys[j]}`);
    i = j + 1;
  }
  return out.join(", ");
};

const tone = (c: string) => (c === "high" ? "green" : c === "medium" ? "amber" : "gray");

export default function FitmentResearchPage() {
  const [rows, setRows] = useState<Row[]>([]);
  const [pdfs, setPdfs] = useState<Pdf[]>([]);
  const [open, setOpen] = useState<Row | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [picked, setPicked] = useState<Record<number, boolean>>({});
  const [years, setYears] = useState<Record<number, [string, string]>>({});
  const [pid, setPid] = useState("");
  const [msg, setMsg] = useState<{ kind: "info" | "error" | "success"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  // paste copied catalog fitment onto one product
  const [pq, setPq] = useState("");
  const [found, setFound] = useState<Found[]>([]);
  const [target, setTarget] = useState<Found | null>(null);
  const [vtypes, setVtypes] = useState<VType[]>([]);
  const [vtype, setVtype] = useState("");
  const [pasted, setPasted] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [skipMakes, setSkipMakes] = useState<Record<string, boolean>>({});
  const [skipModels, setSkipModels] = useState<Record<string, boolean>>({});

  const load = useCallback(() => {
    api<Row[]>("/fitment-research/queue").then(setRows).catch((e) => setMsg({ kind: "error", text: errMsg(e) }));
    api<Pdf[]>("/fitment-research/pdfs").then(setPdfs).catch(() => {});
  }, []);
  useEffect(load, [load]);
  useEffect(() => {
    api<VType[]>("/vehicle-types").then((t) => {
      setVtypes(t);
      const m = t.find((x) => x.slug === "motorcycle");
      if (m) setVtype(String(m.id));
    }).catch(() => {});
  }, []);
  // keep PDF progress fresh while one is being read
  useEffect(() => {
    if (!pdfs.some((p) => p.status === "indexing")) return;
    const t = setInterval(() => api<Pdf[]>("/fitment-research/pdfs").then(setPdfs).catch(() => {}), 4000);
    return () => clearInterval(t);
  }, [pdfs]);

  const run = async (fn: () => Promise<string>) => {
    setBusy(true); setMsg(null);
    try { setMsg({ kind: "success", text: await fn() }); load(); } catch (e) { setMsg({ kind: "error", text: errMsg(e) }); }
    setBusy(false);
  };

  const review = async (r: Row) => {
    setOpen(r); setDetail(null);
    const d = await api<Detail>(`/fitment-research/products/${r.product_id}/proposal`);
    setDetail(d);
    setPicked(Object.fromEntries(d.items.map((i) => [i.id, i.confidence !== "low" && !!i.model_id])));
    setYears({});
  };

  const apply = (markUniversal = false) => open && detail?.proposal && run(async () => {
    const items = detail.items.filter((i) => picked[i.id]).map((i) => {
      const y = years[i.id];
      return { item_id: i.id, year_from: y?.[0] ? Number(y[0]) : undefined, year_to: y?.[1] ? Number(y[1]) : undefined };
    });
    const r = await api<{ added: number; skipped: number }>(`/fitment-research/proposals/${detail.proposal!.id}/apply`, { method: "POST", body: { items, mark_universal: markUniversal } });
    setOpen(null);
    return `Added ${r.added} machine-years${r.skipped ? `; ${r.skipped} skipped (no model or years)` : ""}.`;
  });

  const findProduct = () => run(async () => {
    const r = await api<Found[]>(`/fitment-research/paste/products?q=${encodeURIComponent(pq.trim())}`);
    setFound(r);
    if (r.length === 1) setTarget(r[0]);
    return r.length ? `${r.length} product${r.length === 1 ? "" : "s"} found.` : "No product matches that.";
  });

  const previewPaste = () => run(async () => {
    const r = await api<Preview>("/fitment-research/paste/preview", { method: "POST", body: { text: pasted } });
    setPreview(r); setSkipMakes({}); setSkipModels({});
    const t = r.totals;
    return `Read ${t.makes} makes, ${t.models} models, ${t.model_years} model-years. ${t.new_makes} new makes and ${t.new_models} new models would be created. Nothing is saved yet.`;
  });

  const applyPaste = () => target && run(async () => {
    const r = await api<Record<string, number>>(`/fitment-research/products/${target.id}/paste/apply`, {
      method: "POST",
      body: {
        text: pasted, vehicle_type_id: Number(vtype),
        skip_makes: Object.keys(skipMakes).filter((k) => skipMakes[k]),
        skip_models: Object.keys(skipModels).filter((k) => skipModels[k]),
      },
    });
    setPreview(null); setPasted("");
    return `Fitment added to ${target.title}: ${r.fitment_added} vehicles (${r.already_had} already had). Created ${r.makes_created} makes, ${r.models_created} models and ${r.years_created} years.`;
  });

  const uploadPdf = (f: File) => run(async () => {
    const { data } = await getSupabase().auth.getSession();
    const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL ?? ""}/admin/fitment-research/pdfs?name=${encodeURIComponent(f.name)}`, {
      method: "POST", headers: { Authorization: `Bearer ${data.session?.access_token}`, "Content-Type": "application/pdf" }, body: f,
    });
    if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail ?? `Upload failed (${res.status})`);
    return `${f.name} is being read. It can be used once it says ready.`;
  });

  return (
    <div className="space-y-6">
      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}

      <section className="space-y-3 rounded border border-gray-200 p-4">
        <h2 className="font-semibold">Find suggestions</h2>
        <div className="flex flex-wrap items-center gap-2">
          <button className={btnCls} disabled={busy} onClick={() => run(async () => {
            const r = await api<{ looked_at: number; suggestions_made: number }>("/fitment-research/names/run", { method: "POST", body: { limit: 500 } });
            return `Read ${r.looked_at} product names; ${r.suggestions_made} suggestions made.`;
          })}>Read machines from product names</button>
          <span className="text-sm text-gray-500">or research one product with AI:</span>
          <input className={inputCls + " w-28"} placeholder="Product id" value={pid} onChange={(e) => setPid(e.target.value)} />
          <button className={btn2Cls} disabled={busy || !pid} onClick={() => run(async () => {
            const r = await api<{ machines: number; verdict: string; used_catalog_pages: number }>(`/fitment-research/products/${pid}/ai`, { method: "POST" });
            return `AI found ${r.machines} machines (${r.verdict}); ${r.used_catalog_pages} catalog pages used.`;
          })}>{busy ? "Working…" : "Research"}</button>
        </div>
        <p className="text-xs text-gray-500">Nothing changes on a product until you approve a suggestion below.</p>
      </section>

      <section className="space-y-3 rounded border border-gray-200 p-4">
        <h2 className="font-semibold">Paste fitment from the catalog</h2>
        <p className="text-xs text-gray-500">
          On the distributor catalog page open the Fitment tab, click Expand All, copy everything and paste it here. Missing makes and models are
          added, years are added, and the part is linked to every pasted model-year (this also feeds its eBay compatible vehicles).
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <input className={inputCls + " w-64"} placeholder="Product id, SKU or part # (e.g. 56-132)" value={pq}
            onChange={(e) => { setPq(e.target.value); setTarget(null); }} onKeyDown={(e) => e.key === "Enter" && pq.trim() && findProduct()} />
          <button className={btn2Cls} disabled={busy || !pq.trim()} onClick={findProduct}>Find product</button>
        </div>
        {!target && found.length > 1 && (
          <ul className="divide-y rounded border border-gray-200 text-sm">
            {found.map((f) => (
              <li key={f.id} className="flex cursor-pointer items-center justify-between gap-3 px-3 py-2 hover:bg-gray-50" onClick={() => setTarget(f)}>
                <span className="truncate">{f.title} <span className="text-gray-400">{f.sku} {f.mpn ?? ""}</span></span>
                <Badge>{f.fitment_rows} vehicles</Badge>
              </li>
            ))}
          </ul>
        )}
        {target && (
          <div className="flex flex-wrap items-center gap-2 rounded bg-gray-50 px-3 py-2 text-sm">
            <b>{target.title}</b><span className="text-gray-400">{target.sku} {target.mpn ?? ""}</span>
            <Badge tone={target.fitment_rows ? "amber" : "gray"}>{target.fitment_rows} vehicles already</Badge>
          </div>
        )}
        <textarea className={inputCls + " h-40 w-full font-mono text-xs"} placeholder={"BMW\n2019\nF750GS\n2009\nG650GS\n…\nBuell\n2003\nXB9R Firebolt"}
          value={pasted} onChange={(e) => { setPasted(e.target.value); setPreview(null); }} />
        <div className="flex flex-wrap items-center gap-2">
          <label className="text-sm text-gray-600">New models go under type</label>
          <select className={inputCls + " w-44"} value={vtype} onChange={(e) => setVtype(e.target.value)}>
            <option value="">Choose…</option>
            {vtypes.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select>
          <button className={btnCls} disabled={busy || !pasted.trim()} onClick={previewPaste}>Preview</button>
        </div>
        {preview && (
          <div className="space-y-2">
            {preview.warnings.length > 0 && <Notice kind="info">{preview.warnings.join(" ")}</Notice>}
            {preview.makes.map((mk) => (
              <details key={mk.make} open className="rounded border border-gray-200">
                <summary className="flex cursor-pointer items-center gap-2 px-3 py-2 text-sm">
                  <input type="checkbox" checked={!skipMakes[mk.make]} onClick={(e) => e.stopPropagation()}
                    onChange={(e) => setSkipMakes({ ...skipMakes, [mk.make]: !e.target.checked })} />
                  <b>{mk.make}</b>
                  <Badge tone={mk.make_exists ? "gray" : "green"}>{mk.make_exists ? "make exists" : "new make"}</Badge>
                  <Badge>{mk.models.length} models</Badge>
                  <Badge tone="amber">{mk.models.filter((m) => !m.model_exists).length} new</Badge>
                  <Badge>{mk.models.reduce((a, m) => a + m.years.length, 0)} model-years to link</Badge>
                  <Badge tone="green">{mk.models.reduce((a, m) => a + m.new_years, 0)} new years</Badge>
                </summary>
                <ul className="divide-y border-t text-sm">
                  {mk.models.map((m) => {
                    const key = `${mk.make}|${m.model}`;
                    return (
                      <li key={key} className="flex items-start gap-2 px-3 py-1.5">
                        <input type="checkbox" className="mt-1" checked={!skipModels[key]} onChange={(e) => setSkipModels({ ...skipModels, [key]: !e.target.checked })} />
                        <div className="flex-1">
                          <span className="font-medium">{m.model}</span>{" "}
                          <Badge tone={m.model_exists ? "gray" : "green"}>{m.model_exists ? "exists" : "new"}</Badge>
                          {m.new_years > 0 && <span className="ml-1 text-xs text-gray-500">{m.new_years} new years</span>}
                          {m.similar.length > 0 && <span className="ml-1 text-xs text-amber-700">similar on site: {m.similar.join(", ")}</span>}
                          <div className="text-xs text-gray-500">{yearsText(m.years)}</div>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              </details>
            ))}
            <button className={btnCls} disabled={busy || !target || !vtype} onClick={applyPaste}>
              {target ? `Add this fitment to ${target.sku || target.title}` : "Find the product first"}
            </button>
          </div>
        )}
      </section>

      <section className="space-y-2 rounded border border-gray-200 p-4">
        <h2 className="font-semibold">Catalog PDFs</h2>
        <input type="file" accept="application/pdf" onChange={(e) => e.target.files?.[0] && uploadPdf(e.target.files[0])} />
        <ul className="divide-y text-sm">
          {pdfs.map((p) => (
            <li key={p.id} className="flex items-center justify-between py-1.5">
              <span>{p.name}</span>
              <span className="flex items-center gap-2">
                <Badge tone={p.status === "ready" ? "green" : p.status === "failed" ? "gray" : "amber"}>
                  {p.status === "indexing" ? `reading ${p.pages_done}/${p.pages || "?"}` : p.status}
                </Badge>
                <button className="text-red-600" onClick={() => run(async () => { await api(`/fitment-research/pdfs/${p.id}`, { method: "DELETE" }); return "Removed."; })}>Remove</button>
              </span>
            </li>
          ))}
          {pdfs.length === 0 && <li className="py-2 text-gray-500">No catalogs yet.</li>}
        </ul>
      </section>

      <section className="space-y-2">
        <h2 className="font-semibold">Waiting for review ({rows.length})</h2>
        <ul className="divide-y rounded border border-gray-200 text-sm">
          {rows.map((r) => (
            <li key={r.id} className="flex cursor-pointer items-center justify-between gap-3 px-3 py-2 hover:bg-gray-50" onClick={() => review(r)}>
              <span className="truncate">{r.name} <span className="text-gray-400">{r.sku}</span></span>
              <span className="flex shrink-0 gap-2"><Badge tone="blue">{r.origin}</Badge><Badge>{r.items} machines</Badge></span>
            </li>
          ))}
          {rows.length === 0 && <li className="px-3 py-3 text-gray-500">Nothing waiting.</li>}
        </ul>
      </section>

      {open && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={() => setOpen(null)}>
          <div className="max-h-[85vh] w-full max-w-3xl space-y-3 overflow-y-auto rounded bg-white p-5" onClick={(e) => e.stopPropagation()}>
            <h3 className="font-semibold">{open.name}</h3>
            {!detail ? <p className="text-sm text-gray-500">Loading…</p> : (
              <>
                {detail.proposal?.notes && <Notice>{detail.proposal.notes}</Notice>}
                {detail.items.length === 0 && <p className="text-sm text-gray-600">No machines found (verdict: {detail.proposal?.verdict}).</p>}
                {detail.items.map((i) => (
                  <label key={i.id} className="flex items-start gap-3 rounded border border-gray-200 p-2 text-sm">
                    <input type="checkbox" className="mt-1" checked={!!picked[i.id]} disabled={!i.model_id} onChange={(e) => setPicked({ ...picked, [i.id]: e.target.checked })} />
                    <div className="flex-1 space-y-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <b>{i.make_text} {i.model_text}</b>
                        <Badge tone={tone(i.confidence)}>{i.confidence}</Badge><Badge>{i.source_kind}</Badge>
                        {!i.model_id && <Badge tone="amber">model not in lists</Badge>}
                      </div>
                      <div className="flex items-center gap-1">
                        <input className={inputCls + " w-20"} placeholder={String(i.year_from ?? "from")} onChange={(e) => setYears({ ...years, [i.id]: [e.target.value, years[i.id]?.[1] ?? ""] })} />
                        –
                        <input className={inputCls + " w-20"} placeholder={i.year_to === 9999 ? "newest" : String(i.year_to ?? "to")} onChange={(e) => setYears({ ...years, [i.id]: [years[i.id]?.[0] ?? "", e.target.value] })} />
                        {i.years_note && <span className="text-xs text-amber-700">{i.years_note}</span>}
                      </div>
                      {i.evidence && <p className="text-xs text-gray-500">{i.evidence} {i.source_ref && <>({i.source_ref})</>}</p>}
                    </div>
                  </label>
                ))}
                <div className="flex flex-wrap gap-2">
                  <button className={btnCls} disabled={busy} onClick={() => apply(false)}>Add checked fitment</button>
                  {detail.proposal?.verdict === "universal" && <button className={btn2Cls} disabled={busy} onClick={() => apply(true)}>Mark universal</button>}
                  <button className={btn2Cls} disabled={busy} onClick={() => detail.proposal && run(async () => { await api(`/fitment-research/proposals/${detail.proposal!.id}/dismiss`, { method: "POST" }); setOpen(null); return "Dismissed."; })}>Dismiss</button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
