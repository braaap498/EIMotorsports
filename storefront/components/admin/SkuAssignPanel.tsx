"use client";

import { useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Notice, btnCls, btn2Cls, dangerCls, inputCls } from "./ui";

/* ONE-TIME panel: catalog-ordered SKUs with reserved numbers. Nothing changes until Apply is pressed and confirmed.
   When the run is finished, delete this file and its two lines in app/admin/products/page.tsx. */

type Plan = {
  scope: string;
  products: number;
  will_change: number;
  range: [number, number] | null;
  displaced: number;
  unchanged: number;
  reserved_numbers: number;
  last_number: string;
  ranges: Record<string, [string, string] | null>;
  catalog_counts: Record<string, number>;
  distributors: Record<string, string[]>;
  labels_to_reprint: number;
  warnings: string[];
  duplicates: { source: string; item_id: number; keeps: number | null; others: number[] }[];
  slots?: { number: string; source: string; part_sku: string | null; part_mfr: string | null; product_id: number | null; product_now: string | null }[];
  manual?: { source: string; item_id: number; number: string | null; products: number[] }[];
  manual_total?: number;
  duplicate_total: number;
  sample: { product_id: number; from: string | null; to: string }[];
};
type Status = {
  running: boolean;
  phase: string | null;
  done: number;
  total: number;
  message: string;
  error: string | null;
  last_run?: { id: number; status: string; phase: string | null; done: number; total: number; message: string | null };
};

const LABELS: Record<string, string> = { autodist: "Automatic", rmatv: "Rocky Mountain", niche: "Niche (block of 1000)", sinera: "Sinera (block of 1000)", other: "All other products" };

export default function SkuAssignPanel() {
  const [scope, setScope] = useState<"all" | "active">("all");
  const [plan, setPlan] = useState<Plan | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [snap, setSnap] = useState(true);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [status, setStatus] = useState<Status | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  async function poll() {
    try {
      const s = await api<Status>("/cleanup/sku-catalog/status");
      setStatus(s);
      if (!s.running && timer.current) {
        clearInterval(timer.current);
        timer.current = null;
      }
    } catch {
      /* keep polling */
    }
  }
  function watch() {
    if (!timer.current) timer.current = setInterval(poll, 3000);
    poll();
  }
  useEffect(() => {
    poll().then(() => undefined);
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    if (status?.running) watch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status?.running]);

  // blank both = every product; both filled = only the products planned for those numbers (e.g. 1 to 100)
  const lo = parseInt(from, 10);
  const hi = parseInt(to, 10);
  const hasRange = from.trim() !== "" || to.trim() !== "";
  const rangeOk = !hasRange || (Number.isInteger(lo) && Number.isInteger(hi) && lo >= 1 && hi >= lo);
  const rangeQs = hasRange && rangeOk ? `&number_from=${lo}&number_to=${hi}` : "";

  async function preview() {
    if (!rangeOk) {
      setErr("Enter both ends of the number range (for example 1 and 100), or leave both blank for every product.");
      return;
    }
    setBusy(true);
    setErr("");
    setPlan(null);
    try {
      setPlan(await api<Plan>(`/cleanup/sku-catalog/plan?scope=${scope}${rangeQs}`));
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  }
  async function apply() {
    if (!plan || !window.confirm(`Renumber ${plan.will_change} products${plan.range ? ` (numbers ${plan.range[0]} to ${plan.range[1]})` : ""} now? Their SKUs and shelf barcodes change, and printed labels must be reprinted. Pause the catalog make-products jobs first.`)) return;
    setBusy(true);
    setErr("");
    try {
      await api("/cleanup/sku-catalog/apply", { method: "POST", body: { scope, confirm: "RENUMBER", update_snapshots: snap, ...(plan.range ? { number_from: plan.range[0], number_to: plan.range[1] } : {}) } });
      watch();
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  const running = !!status?.running;
  return (
    <section className="space-y-3 rounded-lg border-2 border-amber-400 bg-white p-4">
      <h2 className="text-lg font-semibold">Catalog-ordered SKUs</h2>
      <p className="text-sm text-gray-600">
        Numbers go to Automatic first, then Rocky Mountain, then a block of 1000 each for Niche and Sinera, then every other product. A catalog part
        with no product reserves its number until the product is made. Every product is renumbered, so its EIB- shelf label changes too.
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <select className={inputCls} value={scope} onChange={(e) => setScope(e.target.value as "all" | "active")} disabled={running}>
          <option value="all">All catalog rows</option>
          <option value="active">Active catalog rows only</option>
        </select>
        <label className="flex items-center gap-1 text-sm">
          Numbers
          <input className={inputCls + " w-24"} inputMode="numeric" placeholder="from" value={from} onChange={(e) => { setFrom(e.target.value); setPlan(null); }} disabled={running} />
          to
          <input className={inputCls + " w-24"} inputMode="numeric" placeholder="to" value={to} onChange={(e) => { setTo(e.target.value); setPlan(null); }} disabled={running} />
        </label>
        <button className={btn2Cls} onClick={preview} disabled={busy || running}>
          {busy ? "Working…" : "Preview SKU plan (changes nothing)"}
        </button>
      </div>
      {err && <Notice kind="error">{err}</Notice>}

      {plan && (
        <div className="space-y-3">
          <div className="grid gap-2 text-sm sm:grid-cols-2">
            {Object.entries(plan.ranges).map(([k, v]) => (
              <div key={k} className="rounded border border-gray-200 p-2">
                <div className="font-medium">{LABELS[k] ?? k}</div>
                <div className="text-gray-600">
                  {v ? `${v[0]} to ${v[1]}` : "no numbers"}
                  {plan.catalog_counts[k] != null ? ` · ${plan.catalog_counts[k]} catalog parts` : ""}
                </div>
              </div>
            ))}
          </div>
          <p className="text-sm">
            {plan.range ? `Numbers ${plan.range[0]} to ${plan.range[1]}: ` : ""}{plan.products} products: <b>{plan.will_change}</b> change, {plan.unchanged} stay. {plan.reserved_numbers} numbers stay reserved. Highest number {plan.last_number}.
            {" "}Shelf labels to reprint: <b>{plan.labels_to_reprint}</b>.
          </p>
          {plan.warnings.map((w) => (
            <Notice key={w} kind="info">{w}</Notice>
          ))}
          {plan.duplicate_total > 0 && (
            <details className="text-sm">
              <summary className="cursor-pointer">{plan.duplicate_total} catalog parts with more than one product (lowest id keeps the number)</summary>
              <ul className="mt-1 list-disc pl-5">
                {plan.duplicates.map((d) => (
                  <li key={d.source + d.item_id}>
                    {LABELS[d.source] ?? d.source} part {d.item_id}: keeps #{d.keeps}, others {d.others.map((o) => "#" + o).join(", ")}
                  </li>
                ))}
              </ul>
            </details>
          )}
          {(plan.manual_total ?? 0) > 0 && (
            <details className="text-sm">
              <summary className="cursor-pointer">{plan.manual_total} catalog parts that match a product only by manufacturer # (check by hand)</summary>
              <ul className="mt-1 list-disc pl-5">
                {(plan.manual ?? []).map((m) => (
                  <li key={m.source + m.item_id}>
                    {LABELS[m.source] ?? m.source} part {m.item_id} ({m.number ?? "no number"}): product {m.products.map((o) => "#" + o).join(", ")}
                  </li>
                ))}
              </ul>
            </details>
          )}
          {(plan.slots?.length ?? 0) > 0 && (
            <details className="text-sm" open>
              <summary className="cursor-pointer">Each number in this range: the catalog part and the product that gets it</summary>
              <ul className="mt-1 pl-5">
                {plan.slots!.map((x) => (
                  <li key={x.number}>
                    {x.number}: {LABELS[x.source] ?? x.source} part {x.part_sku ?? "(empty slot)"}
                    {x.part_mfr ? ` (mfr ${x.part_mfr})` : ""} → {x.product_id ? `product #${x.product_id} (now ${x.product_now ?? "no number"})` : "no product yet, number reserved"}
                  </li>
                ))}
              </ul>
            </details>
          )}
          <details className="text-sm">
            <summary className="cursor-pointer">First changes</summary>
            <ul className="mt-1 pl-5">
              {plan.sample.map((s) => (
                <li key={s.product_id}>
                  #{s.product_id}: {s.from} → {s.to}
                </li>
              ))}
            </ul>
          </details>
          <div className="space-y-2 rounded border border-red-300 p-3">
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={snap} onChange={(e) => setSnap(e.target.checked)} /> Also update the SKU copy saved with merged eBay listings
            </label>
            <div className="flex flex-wrap items-center gap-2">
              <button className={dangerCls} onClick={apply} disabled={busy || running}>
                {plan.range ? `Apply: numbers ${plan.range[0]} to ${plan.range[1]} only` : "Apply: renumber all products"}
              </button>
            </div>
            <p className="text-xs text-gray-500">Pause the catalog make-products jobs first. If a run is interrupted, press Preview and Apply again; it picks up where it stopped.</p>
          </div>
        </div>
      )}

      {status && (running || status.last_run) && (
        <div className="text-sm">
          {running ? (
            <Notice kind="info">
              {status.phase}: {status.done} of {status.total} · {status.message}
            </Notice>
          ) : status.error ? (
            <Notice kind="error">{status.error}</Notice>
          ) : status.last_run ? (
            <Notice kind={status.last_run.status === "done" ? "success" : "info"}>
              Last run #{status.last_run.id}: {status.last_run.status}. {status.last_run.message}
            </Notice>
          ) : null}
        </div>
      )}
    </section>
  );
}
