"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Badge, Notice, btn2Cls, btnCls, inputCls } from "../ui";

type Warehouse = { id: number; name: string; is_default: boolean; active: boolean; parts: number; units: number };
type Resp = { name_max: number; items: Warehouse[] };

/* Inventory > Warehouses. There is one warehouse to start ("E&I Warehouse") and every part on hand is in it.
   Its name can be changed here; the new name shows in the Online orders "Fulfillment source" column right away.
   More warehouses, and stock kept per warehouse, come in a later step. */
export default function WarehousePanel({ active, canRename }: { active: boolean; canRename: boolean }) {
  const [items, setItems] = useState<Warehouse[]>([]);
  const [nameMax, setNameMax] = useState(20);
  const [edit, setEdit] = useState<{ id: number; name: string } | null>(null);
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
    if (!edit) return;
    setBusy(true);
    setMsg(null);
    try {
      await api(`/inventory/warehouses/${edit.id}`, { method: "PATCH", body: { name: edit.name } });
      setEdit(null);
      setMsg({ kind: "success", text: "Warehouse name changed." });
      await load();
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3">
      <p className="text-xs text-gray-500">
        The warehouses that hold our stock. Everything on hand is in the first one for now. You can change its name; orders
        that ship from it show the new name under <strong>Fulfillment source</strong> on the Online orders list.
      </p>
      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}

      <div className="rounded border border-gray-200 bg-white">
        <table className="w-full min-w-[40rem] table-fixed text-left text-sm">
          <colgroup>
            <col /> {/* Name */}
            <col className="w-[9rem]" /> {/* Parts on hand */}
            <col className="w-[9rem]" /> {/* Units on hand */}
            <col className="w-[16rem]" /> {/* Actions */}
          </colgroup>
          <thead className="bg-gray-50 text-xs uppercase text-gray-500">
            <tr>
              <th className="whitespace-nowrap px-3 py-2">Warehouse</th>
              <th className="whitespace-nowrap px-3 py-2">Parts on hand</th>
              <th className="whitespace-nowrap px-3 py-2">Units on hand</th>
              <th className="whitespace-nowrap px-3 py-2"></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {items.map((w) => (
              <tr key={w.id}>
                <td className="px-3 py-2">
                  {edit?.id === w.id ? (
                    <input
                      className={`${inputCls} !w-64`}
                      value={edit.name}
                      maxLength={nameMax}
                      autoFocus
                      onChange={(e) => setEdit({ id: w.id, name: e.target.value })}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") void save();
                        if (e.key === "Escape") setEdit(null);
                      }}
                      aria-label="Warehouse name"
                    />
                  ) : (
                    <span className="whitespace-nowrap font-medium">
                      {w.name}
                      {w.is_default && (
                        <span className="ml-2">
                          <Badge tone="gray">Main</Badge>
                        </span>
                      )}
                    </span>
                  )}
                </td>
                <td className="whitespace-nowrap px-3 py-2">{w.parts.toLocaleString()}</td>
                <td className="whitespace-nowrap px-3 py-2">{w.units.toLocaleString()}</td>
                <td className="whitespace-nowrap px-3 py-2">
                  {canRename &&
                    (edit?.id === w.id ? (
                      <span className="flex gap-2">
                        <button type="button" className={btnCls} disabled={busy || !edit.name.trim()} onClick={() => void save()}>
                          Save name
                        </button>
                        <button type="button" className={btn2Cls} disabled={busy} onClick={() => setEdit(null)}>
                          Cancel
                        </button>
                      </span>
                    ) : (
                      <button type="button" className={btn2Cls} onClick={() => { setMsg(null); setEdit({ id: w.id, name: w.name }); }}>
                        Rename
                      </button>
                    ))}
                </td>
              </tr>
            ))}
            {items.length === 0 && (
              <tr>
                <td colSpan={4} className="px-3 py-6 text-center text-gray-500">
                  Loading…
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
