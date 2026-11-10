"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Badge, Field, Modal, Notice, btn2Cls, btnCls, inputCls } from "../ui";

export type Warehouse = {
  id: number;
  name: string;
  is_default: boolean;
  active: boolean;
  parts: number;
  units: number;
  address: string | null;
  city: string | null;
  state: string | null;
  zip: string | null;
  manager: string | null;
  phone: string | null;
  email: string | null;
  notes: string | null;
};
type Form = Omit<Warehouse, "id" | "is_default" | "active" | "parts" | "units">;
const blank: Form = { name: "", address: "", city: "", state: "", zip: "", manager: "", phone: "", email: "", notes: "" };
type Resp = { name_max: number; items: Warehouse[] };

/* Inventory > Warehouses. There is one warehouse to start ("E&I Warehouse") and every part on hand is in it.
   Its name can be changed here; the new name shows in the Online orders "Fulfillment source" column right away.
   More warehouses, and stock kept per warehouse, come in a later step. */
export default function WarehousePanel({ active, canRename }: { active: boolean; canRename: boolean }) {
  const [items, setItems] = useState<Warehouse[]>([]);
  const [nameMax, setNameMax] = useState(20);
  const [editing, setEditing] = useState<{ id: number | null; form: Form } | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await api<Resp>("/inventory/warehouses");
      setItems(r.items);
      if (r.name_max) setNameMax(r.name_max);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    }
  }, []);

  useEffect(() => {
    if (active) void load();
  }, [active, load]);

  async function save() {
    if (!editing) return;
    setBusy(true);
    setMsg(null);
    try {
      if (editing.id === null) {
        await api("/inventory/warehouses", { method: "POST", body: editing.form });
      } else {
        await api(`/inventory/warehouses/${editing.id}/details`, { method: "PUT", body: editing.form });
      }
      setMsg({ kind: "success", text: editing.id === null ? "Warehouse added." : "Warehouse saved." });
      setEditing(null);
      await load();
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy(false);
    }
  }

  const setF = (k: keyof Form, v: string) => setEditing((e) => (e ? { ...e, form: { ...e.form, [k]: v } } : e));
  const addressLine = (w: Warehouse) =>
    [w.address, [w.city, w.state].filter(Boolean).join(", "), w.zip].filter((x) => x && String(x).trim()).join(" ");

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-3xl text-xs text-gray-500">
          The warehouses that hold our stock. Everything already on hand is in the Main one. Choose a warehouse in the Stock
          tab to see its parts and add more. Every warehouse counts toward what the website and eBay can sell; picking takes
          from Main first and uses the others when Main runs out. Orders that ship from the Main warehouse show its name
          under <strong>Fulfillment source</strong>.
        </p>
        {canRename && (
          <button type="button" className={btnCls} onClick={() => { setMsg(null); setEditing({ id: null, form: { ...blank } }); }}>
            Add warehouse
          </button>
        )}
      </div>
      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}

      <div className="rounded border border-gray-200 bg-white">
        <table className="w-full min-w-[56rem] text-left text-sm">
          <thead className="bg-gray-50 text-xs uppercase text-gray-500">
            <tr>
              <th className="whitespace-nowrap px-3 py-2">Warehouse</th>
              <th className="px-3 py-2">Address</th>
              <th className="px-3 py-2">Manager / contact</th>
              <th className="whitespace-nowrap px-3 py-2">Parts</th>
              <th className="whitespace-nowrap px-3 py-2">Units</th>
              <th className="px-3 py-2"></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {items.map((w) => (
              <tr key={w.id} className="align-top">
                <td className="px-3 py-2">
                  <span className="whitespace-nowrap font-medium">
                    {w.name}
                    {w.is_default && (
                      <span className="ml-2">
                        <Badge tone="gray">Main</Badge>
                      </span>
                    )}
                  </span>
                  {w.notes && <div className="mt-1 max-w-xs text-xs text-gray-500">{w.notes}</div>}
                </td>
                <td className="px-3 py-2 text-gray-700">{addressLine(w) || <span className="text-gray-400">—</span>}</td>
                <td className="px-3 py-2 text-gray-700">
                  {w.manager || w.phone || w.email ? (
                    <>
                      {w.manager && <div className="font-medium">{w.manager}</div>}
                      {w.phone && <div>{w.phone}</div>}
                      {w.email && <div className="text-xs text-gray-500">{w.email}</div>}
                    </>
                  ) : (
                    <span className="text-gray-400">—</span>
                  )}
                </td>
                <td className="whitespace-nowrap px-3 py-2">{w.parts.toLocaleString()}</td>
                <td className="whitespace-nowrap px-3 py-2">{w.units.toLocaleString()}</td>
                <td className="whitespace-nowrap px-3 py-2">
                  {canRename && (
                    <button
                      type="button"
                      className={btn2Cls}
                      onClick={() => {
                        setMsg(null);
                        setEditing({
                          id: w.id,
                          form: {
                            name: w.name, address: w.address ?? "", city: w.city ?? "", state: w.state ?? "", zip: w.zip ?? "",
                            manager: w.manager ?? "", phone: w.phone ?? "", email: w.email ?? "", notes: w.notes ?? "",
                          },
                        });
                      }}
                    >
                      Edit
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {items.length === 0 && (
              <tr>
                <td colSpan={6} className="px-3 py-6 text-center text-gray-500">
                  Loading…
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {editing && (
        <Modal title={editing.id === null ? "Add warehouse" : "Edit warehouse"} onClose={() => setEditing(null)}>
          <div className="space-y-3">
            <Field label={`Name (up to ${nameMax} characters)`}>
              <input className={inputCls} value={editing.form.name} maxLength={nameMax} autoFocus onChange={(e) => setF("name", e.target.value)} />
            </Field>
            <Field label="Street address">
              <input className={inputCls} value={editing.form.address ?? ""} onChange={(e) => setF("address", e.target.value)} />
            </Field>
            <div className="grid gap-3 sm:grid-cols-3">
              <Field label="City"><input className={inputCls} value={editing.form.city ?? ""} onChange={(e) => setF("city", e.target.value)} /></Field>
              <Field label="State"><input className={inputCls} value={editing.form.state ?? ""} onChange={(e) => setF("state", e.target.value)} /></Field>
              <Field label="ZIP"><input className={inputCls} value={editing.form.zip ?? ""} onChange={(e) => setF("zip", e.target.value)} /></Field>
            </div>
            <div className="grid gap-3 sm:grid-cols-3">
              <Field label="Manager"><input className={inputCls} value={editing.form.manager ?? ""} onChange={(e) => setF("manager", e.target.value)} /></Field>
              <Field label="Phone"><input className={inputCls} value={editing.form.phone ?? ""} onChange={(e) => setF("phone", e.target.value)} /></Field>
              <Field label="Email"><input className={inputCls} value={editing.form.email ?? ""} onChange={(e) => setF("email", e.target.value)} /></Field>
            </div>
            <Field label="Notes (hours, dock instructions, anything useful)">
              <textarea className={inputCls} rows={3} value={editing.form.notes ?? ""} onChange={(e) => setF("notes", e.target.value)} />
            </Field>
            <div className="flex gap-2">
              <button type="button" className={btnCls} disabled={busy || !editing.form.name.trim()} onClick={() => void save()}>
                {editing.id === null ? "Add warehouse" : "Save"}
              </button>
              <button type="button" className={btn2Cls} disabled={busy} onClick={() => setEditing(null)}>
                Cancel
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}
