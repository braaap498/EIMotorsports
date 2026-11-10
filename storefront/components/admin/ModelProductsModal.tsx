"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import ClearableInput from "./ClearableInput";
import { Notice, Pager, btnCls, btn2Cls, inputCls } from "./ui";

type Model = { id: number; name: string; make: string; year_min: number | null; year_max: number | null; products: number };
type Item = {
  id: number;
  sku: string | null;
  custom_label_sku: string | null;
  ebay_item_id: string | null;
  title: string | null;
  years: number[];
  other_models: number;
};
type Resp = { total: number; items: Item[] };
type Found = { items: Model[] };

const LIMIT = 50;

/** [2008, 2009, 2010, 2013] -> "2008–2010, 2013" */
function yearRanges(years: number[]) {
  const ys = [...years].sort((a, b) => a - b);
  const out: string[] = [];
  for (let i = 0; i < ys.length; ) {
    let j = i;
    while (j + 1 < ys.length && ys[j + 1] === ys[j] + 1) j++;
    out.push(j > i ? `${ys[i]}–${ys[j]}` : String(ys[i]));
    i = j + 1;
  }
  return out.join(", ") || "—";
}

function span(m: { year_min: number | null; year_max: number | null }) {
  if (m.year_min == null) return "no years";
  return m.year_min === m.year_max ? String(m.year_min) : `${m.year_min}–${m.year_max}`;
}

/* Fitment > Models > Products. Shows every product that fits one model (with the years it fits there) and moves chosen
   products to another model, year for year. Used to consolidate a duplicate model: move its products across, then delete
   the empty duplicate from Edit. */
export default function ModelProductsModal({ model, onClose, onChanged }: { model: Model; onClose: () => void; onChanged: () => void }) {
  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<Resp>({ total: 0, items: [] });
  const [loading, setLoading] = useState(true);
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [allMatching, setAllMatching] = useState(false);
  const [targetQ, setTargetQ] = useState("");
  const [targets, setTargets] = useState<Model[]>([]);
  const [target, setTarget] = useState<Model | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const p = new URLSearchParams({ offset: String(offset), limit: String(LIMIT) });
      if (q) p.set("q", q);
      setData(await api<Resp>(`/models/${model.id}/products?${p.toString()}`));
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setLoading(false);
    }
  }, [model.id, offset, q]);
  useEffect(() => {
    void load();
  }, [load]);

  // wait for a pause in typing before searching
  useEffect(() => {
    if (qInput.trim() === q) return;
    const t = setTimeout(() => {
      setQ(qInput.trim());
      setOffset(0);
      setPicked(new Set());
      setAllMatching(false);
    }, 300);
    return () => clearTimeout(t);
  }, [qInput, q]);

  // find the model to move to
  useEffect(() => {
    if (!targetQ.trim()) {
      setTargets([]);
      return;
    }
    const t = setTimeout(() => {
      api<Found>(`/models?q=${encodeURIComponent(targetQ.trim())}&limit=10`)
        .then((r) => setTargets(r.items.filter((m) => m.id !== model.id)))
        .catch(() => setTargets([]));
    }, 300);
    return () => clearTimeout(t);
  }, [targetQ, model.id]);

  const pageAllPicked = data.items.length > 0 && data.items.every((x) => picked.has(x.id));
  const count = allMatching ? data.total : picked.size;

  async function move() {
    if (!target || count === 0) return;
    const what = allMatching ? `ALL ${data.total} product${data.total === 1 ? "" : "s"}${q ? ` matching “${q}”` : ""}` : `${count} product${count === 1 ? "" : "s"}`;
    if (
      !window.confirm(
        `Move ${what} from ${model.make} ${model.name} to ${target.make} ${target.name}?\n\nEach product keeps the same years. Any year the target does not have yet is added to it.`
      )
    )
      return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await api<{ moved_products: number; years_added: number; remaining: number }>(`/models/${model.id}/products/move`, {
        method: "POST",
        body: { to_model_id: target.id, product_ids: allMatching ? [] : [...picked], all_matching: allMatching, q: q || null },
      });
      setPicked(new Set());
      setAllMatching(false);
      setMsg({
        kind: "success",
        text:
          `Moved ${r.moved_products} product${r.moved_products === 1 ? "" : "s"} to ${target.make} ${target.name}` +
          (r.years_added ? ` (${r.years_added} year${r.years_added === 1 ? "" : "s"} added to it)` : "") +
          `. ${r.remaining === 0 ? `${model.make} ${model.name} has no products left, so it can be deleted from Edit.` : `${r.remaining} left on ${model.make} ${model.name}.`}`,
      });
      await load();
      onChanged();
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/40 p-4">
      <div className="my-8 w-full max-w-5xl rounded-lg bg-white p-5 shadow-xl">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-semibold">
            Products on {model.make} {model.name} <span className="text-sm font-normal text-gray-500">({span(model)})</span>
          </h2>
          <button type="button" onClick={onClose} className="text-2xl leading-none text-gray-500 hover:text-black" aria-label="Close">
            ×
          </button>
        </div>

        <div className="space-y-3">
          {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}

          <div className="space-y-2 rounded border border-gray-200 bg-gray-50 p-3">
            <div className="text-sm font-medium">Move the ticked products to…</div>
            {target ? (
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <span className="rounded bg-white px-3 py-2 font-medium ring-1 ring-gray-300">
                  {target.make} {target.name} <span className="font-normal text-gray-500">({span(target)})</span>
                </span>
                <button type="button" className={btn2Cls} disabled={busy} onClick={() => setTarget(null)}>
                  Change
                </button>
              </div>
            ) : (
              <div className="max-w-md space-y-1">
                <ClearableInput
                  className={inputCls}
                  placeholder="Search the model to move them to"
                  value={targetQ}
                  onChange={(e) => setTargetQ(e.target.value)}
                  onClear={() => {
                    setTargetQ("");
                    setTargets([]);
                  }}
                />
                {targets.map((m) => (
                  <button
                    key={m.id}
                    type="button"
                    className="block w-full rounded border border-gray-200 bg-white px-3 py-2 text-left text-sm hover:bg-gray-100"
                    onClick={() => {
                      setTarget(m);
                      setTargetQ("");
                      setTargets([]);
                    }}
                  >
                    {m.make} {m.name} <span className="text-gray-500">· {span(m)} · {m.products} products</span>
                  </button>
                ))}
              </div>
            )}
            <button type="button" className={btnCls} disabled={busy || !target || count === 0} onClick={() => void move()}>
              {busy ? "Moving…" : count === 0 ? "Move" : `Move ${count} product${count === 1 ? "" : "s"}`}
            </button>
          </div>

          <ClearableInput
            className="w-full max-w-md rounded border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900"
            placeholder="Search these products by title, item number, SKU or custom label"
            value={qInput}
            onChange={(e) => setQInput(e.target.value)}
            onClear={() => {
              setQInput("");
              setQ("");
              setOffset(0);
              setPicked(new Set());
              setAllMatching(false);
            }}
          />

          {pageAllPicked && !allMatching && data.total > data.items.length && (
            <div className="rounded bg-blue-50 px-3 py-2 text-sm text-blue-900">
              All {data.items.length} on this page are ticked.{" "}
              <button type="button" className="font-medium underline" onClick={() => setAllMatching(true)}>
                Tick all {data.total}
                {q ? " that match" : ""}
              </button>
            </div>
          )}
          {allMatching && (
            <div className="rounded bg-blue-50 px-3 py-2 text-sm text-blue-900">
              All {data.total}
              {q ? " matching" : ""} products are ticked.{" "}
              <button type="button" className="font-medium underline" onClick={() => { setAllMatching(false); setPicked(new Set()); }}>
                Clear
              </button>
            </div>
          )}

          <Pager total={data.total} limit={LIMIT} offset={offset} onChange={(o) => { setOffset(o); }} />

          <div className="overflow-x-auto rounded border border-gray-200 bg-white">
            <table className="w-full min-w-[44rem] table-fixed text-left text-sm">
              <colgroup>
                <col className="w-[2.5rem]" />
                <col />
                <col className="w-[14rem]" />
                <col className="w-[9rem]" />
              </colgroup>
              <thead className="bg-gray-50 text-xs uppercase text-gray-500">
                <tr>
                  <th className="px-3 py-2">
                    <input
                      type="checkbox"
                      aria-label="Tick all on this page"
                      checked={pageAllPicked}
                      onChange={(e) => {
                        setAllMatching(false);
                        setPicked((cur) => {
                          const n = new Set(cur);
                          data.items.forEach((x) => (e.target.checked ? n.add(x.id) : n.delete(x.id)));
                          return n;
                        });
                      }}
                    />
                  </th>
                  <th className="whitespace-nowrap px-3 py-2">Product</th>
                  <th className="whitespace-nowrap px-3 py-2">Years on this model</th>
                  <th className="whitespace-nowrap px-3 py-2">Other models</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {data.items.map((x) => (
                  <tr key={x.id} className={picked.has(x.id) || allMatching ? "bg-blue-50" : ""}>
                    <td className="px-3 py-2">
                      <input
                        type="checkbox"
                        aria-label={`Tick ${x.title ?? x.id}`}
                        checked={picked.has(x.id) || allMatching}
                        disabled={allMatching}
                        onChange={(e) =>
                          setPicked((cur) => {
                            const n = new Set(cur);
                            if (e.target.checked) n.add(x.id);
                            else n.delete(x.id);
                            return n;
                          })
                        }
                      />
                    </td>
                    <td className="px-3 py-2">
                      <div className="truncate" title={x.title ?? ""}>
                        <a className="hover:underline" href={`/admin/products/${x.id}`} target="_blank" rel="noopener noreferrer">
                          {x.title ?? "(no title)"}
                        </a>
                      </div>
                      <div className="truncate text-xs text-gray-500">
                        {[x.ebay_item_id, x.sku, x.custom_label_sku].filter(Boolean).join(" · ")}
                      </div>
                    </td>
                    <td className="whitespace-nowrap px-3 py-2">{yearRanges(x.years)}</td>
                    <td className="whitespace-nowrap px-3 py-2 text-gray-600">{x.other_models > 0 ? `${x.other_models} more` : "—"}</td>
                  </tr>
                ))}
                {data.items.length === 0 && (
                  <tr>
                    <td colSpan={4} className="px-3 py-6 text-center text-gray-500">
                      {loading ? "Loading…" : q ? "No products match." : "No products fit this model."}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <Pager total={data.total} limit={LIMIT} offset={offset} onChange={(o) => { setOffset(o); }} />

          <div className="flex justify-start border-t border-gray-200 pt-4">
            <button type="button" className={btn2Cls} onClick={onClose}>
              Close
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
