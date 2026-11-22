"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { useAccess } from "./AdminAccess";
import { Badge, Notice, Pager, btnCls, btn2Cls, dangerCls, inputCls } from "./ui";
import type { Dist } from "./Distributors";

/* The parts of a distributor that has no catalog here: the same list the product screen picks from (Edit product > Distributor
   parts > Saved parts). Add a part by hand, edit it, or make a product from it, like the Automatic Distributors page does for its
   catalog. A part that already has a product shows that product; a product made here follows the saved part (its cost,
   description and manufacturer # come from here) and is NOT published until it has a price. */

type Part = {
  id: number;
  part_number: string;
  mfr_number: string | null;
  description: string | null;
  unit_cost: number | null;
  product_id: number | null;
  product_title: string | null;
  products: number;
};
type Flash = { kind: "success" | "error" | "info"; text: string } | null;

const PAGE = 50;
const blank = { part_number: "", mfr_number: "", same: false, description: "", unit_cost: "" };
const money = (v: number | null) => (v == null ? "—" : `$${Number(v).toFixed(2)}`);

export default function ManualParts({ dist }: { dist: Dist }) {
  const { can } = useAccess();
  const canMake = can("products.create");
  const [items, setItems] = useState<Part[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [q, setQ] = useState("");
  const [only, setOnly] = useState<"all" | "no_product" | "has_product">("all");
  const [loading, setLoading] = useState(true);
  const [flash, setFlash] = useState<Flash>(null);
  const [form, setForm] = useState(blank);
  const [adding, setAdding] = useState(false);
  const [edit, setEdit] = useState<{ id: number; part: string; mfr: string; desc: string; cost: string } | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [tick, setTick] = useState(0);

  const load = useCallback(() => {
    setLoading(true);
    api<{ items: Part[]; total: number }>(
      `/kits/manual-catalog?distributor_id=${dist.id}&q=${encodeURIComponent(q.trim())}&only=${only}&limit=${PAGE}&offset=${offset}`
    )
      .then((r) => {
        setItems(r.items);
        setTotal(r.total);
      })
      .catch((e) => setFlash({ kind: "error", text: errMsg(e) }))
      .finally(() => setLoading(false));
  }, [dist.id, q, only, offset]);

  // reload when the vendor, the search, the filter or the page changes (the search waits a moment while typing)
  useEffect(() => {
    const t = setTimeout(load, 250);
    return () => clearTimeout(t);
  }, [load, tick]);
  useEffect(() => setOffset(0), [dist.id, q, only]);

  async function add() {
    if (!form.part_number.trim()) return setFlash({ kind: "error", text: "Type the vendor's part number." });
    const cost = form.unit_cost.trim() === "" ? null : Number(form.unit_cost);
    if (cost !== null && Number.isNaN(cost)) return setFlash({ kind: "error", text: "Cost must be a number." });
    setAdding(true);
    setFlash(null);
    try {
      await api("/kits/manual-catalog", {
        method: "POST",
        body: {
          distributor_id: dist.id,
          part_number: form.part_number.trim(),
          mfr_number: (form.same ? form.part_number.trim() : form.mfr_number.trim()) || null,
          description: form.description.trim() || null,
          unit_cost: cost,
        },
      });
      setForm(blank);
      setFlash({ kind: "success", text: "Part saved." });
      setTick((n) => n + 1);
    } catch (e) {
      setFlash({ kind: "error", text: errMsg(e) });
    } finally {
      setAdding(false);
    }
  }

  async function saveEdit() {
    if (!edit) return;
    const cost = edit.cost.trim() === "" ? null : Number(edit.cost);
    if (cost !== null && Number.isNaN(cost)) return setFlash({ kind: "error", text: "Cost must be a number." });
    setBusyId(edit.id);
    try {
      await api(`/kits/manual-catalog/${edit.id}`, {
        method: "PUT",
        body: { mfr_number: edit.mfr.trim() || null, description: edit.desc.trim() || null, unit_cost: cost },
      });
      setEdit(null);
      setFlash({ kind: "success", text: "Part updated. Products that use it follow the change." });
      setTick((n) => n + 1);
    } catch (e) {
      setFlash({ kind: "error", text: errMsg(e) });
    } finally {
      setBusyId(null);
    }
  }

  async function remove(p: Part) {
    if (!window.confirm(`Remove ${p.part_number} from ${dist.name}'s saved parts?`)) return;
    setBusyId(p.id);
    try {
      await api(`/kits/manual-catalog/${p.id}`, { method: "DELETE" });
      setFlash({ kind: "success", text: `${p.part_number} removed.` });
      setTick((n) => n + 1);
    } catch (e) {
      setFlash({ kind: "error", text: errMsg(e) });
    } finally {
      setBusyId(null);
    }
  }

  async function makeProduct(p: Part) {
    if (!window.confirm(`Make a product from ${p.part_number}?\n\nIt is created unpublished, with this part as its distributor part and the custom label set to the vendor part # when that is free.`)) return;
    setBusyId(p.id);
    setFlash(null);
    try {
      const r = await api<{ product_id: number; title: string; custom_label: string | null }>(`/kits/manual-catalog/${p.id}/make-product`, { method: "POST" });
      setFlash({
        kind: "info",
        text: `Made product #${r.product_id}: ${r.title}${r.custom_label ? ` (custom label ${r.custom_label})` : ". Its custom label was left blank: that part # is already a label or cannot be one"}. It is NOT published; set a price, photos and category on the product.`,
      });
      setTick((n) => n + 1);
    } catch (e) {
      setFlash({ kind: "error", text: errMsg(e) });
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-gray-200 bg-white p-4 space-y-3">
        <div>
          <h2 className="text-base font-semibold">{dist.name} parts</h2>
          <p className="text-xs text-gray-500">
            {dist.name} has no catalog loaded here, so its parts are typed once and saved in this list. Products pick from it under Distributor parts, and you can make a product from a part here.
          </p>
        </div>

        <div className="flex flex-wrap items-end gap-2">
          <label className="text-xs">
            Vendor part #
            <input className={inputCls} value={form.part_number} onChange={(e) => setForm({ ...form, part_number: e.target.value })} />
          </label>
          <label className="text-xs">
            Manufacturer #
            <input
              className={inputCls}
              value={form.same ? form.part_number : form.mfr_number}
              disabled={form.same}
              onChange={(e) => setForm({ ...form, mfr_number: e.target.value })}
            />
          </label>
          <label className="flex items-center gap-1 pb-2 text-xs" title="Use the vendor part # as the manufacturer # too">
            <input type="checkbox" checked={form.same} onChange={(e) => setForm({ ...form, same: e.target.checked })} />
            Same as vendor part #
          </label>
          <label className="min-w-[12rem] flex-1 text-xs">
            Description
            <input className={inputCls} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
          </label>
          <label className="text-xs">
            Cost
            <input type="number" step="0.01" className={`${inputCls} w-24`} value={form.unit_cost} onChange={(e) => setForm({ ...form, unit_cost: e.target.value })} />
          </label>
          <button type="button" className={btnCls} disabled={adding} onClick={() => void add()}>
            {adding ? "Saving…" : "Add part"}
          </button>
        </div>
      </div>

      {flash && <Notice kind={flash.kind}>{flash.text}</Notice>}

      <div className="rounded-lg border border-gray-200 bg-white p-4 space-y-3">
        <div className="flex flex-wrap items-end gap-3">
          <label className="min-w-[14rem] flex-1 text-xs">
            Search
            <input className={inputCls} value={q} placeholder="vendor part #, manufacturer # or description" onChange={(e) => setQ(e.target.value)} />
          </label>
          <label className="text-xs">
            Show
            <select className={inputCls} value={only} onChange={(e) => setOnly(e.target.value as typeof only)}>
              <option value="all">All parts</option>
              <option value="no_product">No product yet</option>
              <option value="has_product">Has a product</option>
            </select>
          </label>
          <span className="pb-2 text-xs text-gray-500">{loading ? "Loading…" : `${total.toLocaleString()} part${total === 1 ? "" : "s"}`}</span>
        </div>

        {edit && (
          <div className="flex flex-wrap items-end gap-2 rounded bg-gray-50 p-2">
            <span className="text-xs text-gray-600">
              Editing <span className="font-mono font-semibold">{edit.part}</span>. Every product that uses it follows.
            </span>
            <label className="text-xs">
              Manufacturer #
              <input className={inputCls} value={edit.mfr} onChange={(e) => setEdit({ ...edit, mfr: e.target.value })} />
            </label>
            <label className="min-w-[10rem] flex-1 text-xs">
              Description
              <input className={inputCls} value={edit.desc} onChange={(e) => setEdit({ ...edit, desc: e.target.value })} />
            </label>
            <label className="text-xs">
              Cost
              <input type="number" step="0.01" className={`${inputCls} w-24`} value={edit.cost} onChange={(e) => setEdit({ ...edit, cost: e.target.value })} />
            </label>
            <button type="button" className={btnCls} disabled={busyId === edit.id} onClick={() => void saveEdit()}>
              Save
            </button>
            <button type="button" className={btn2Cls} onClick={() => setEdit(null)}>
              Cancel
            </button>
          </div>
        )}

        {items.length === 0 && !loading ? (
          <p className="text-sm text-gray-500">
            {q.trim() || only !== "all" ? "No saved parts match." : `No parts saved for ${dist.name} yet. Add one above, or type parts on a product under Distributor parts and they are saved here.`}
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="bg-gray-50 text-xs uppercase text-gray-500">
                <tr>
                  <th className="px-2 py-2">Vendor part #</th>
                  <th className="px-2 py-2">Manufacturer #</th>
                  <th className="px-2 py-2">Description</th>
                  <th className="px-2 py-2">Cost</th>
                  <th className="px-2 py-2">Product</th>
                  <th className="px-2 py-2"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {items.map((p) => (
                  <tr key={p.id} className="align-top">
                    <td className="whitespace-nowrap px-2 py-2 font-mono">{p.part_number}</td>
                    <td className="whitespace-nowrap px-2 py-2 font-mono">{p.mfr_number ?? "—"}</td>
                    <td className="px-2 py-2">{p.description ?? "—"}</td>
                    <td className="whitespace-nowrap px-2 py-2">{money(p.unit_cost)}</td>
                    <td className="px-2 py-2">
                      {p.product_id ? (
                        <>
                          <Link href={`/admin/products/${p.product_id}`} className="text-blue-700 hover:underline">
                            #{p.product_id} {p.product_title}
                          </Link>
                          {p.products > 1 && <span className="ml-2"><Badge tone="gray">+{p.products - 1} more</Badge></span>}
                        </>
                      ) : (
                        <span className="text-gray-500">No product yet</span>
                      )}
                    </td>
                    <td className="whitespace-nowrap px-2 py-2 text-right">
                      {canMake && !p.product_id && (
                        <button type="button" className={btn2Cls} disabled={busyId === p.id} onClick={() => void makeProduct(p)}>
                          Make product
                        </button>
                      )}
                      <button
                        type="button"
                        className="ml-2 px-1 text-xs text-gray-600 underline"
                        onClick={() => setEdit({ id: p.id, part: p.part_number, mfr: p.mfr_number ?? "", desc: p.description ?? "", cost: p.unit_cost != null ? String(p.unit_cost) : "" })}
                      >
                        Edit
                      </button>
                      {!p.product_id && (
                        <button type="button" className={`${dangerCls} ml-2`} disabled={busyId === p.id} onClick={() => void remove(p)}>
                          Remove
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <Pager total={total} limit={PAGE} offset={offset} onChange={(o) => setOffset(o)} />
      </div>
    </div>
  );
}
