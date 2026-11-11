"use client";

import { useEffect, useState } from "react";
import { api } from "../../../lib/adminApi";
import { inputCls } from "../ui";
import { useAccess } from "../AdminAccess";
import WarehouseStock from "./WarehouseStock";

type Wh = { id: number; name: string; is_default: boolean };

/** Stock for the chosen warehouse. Every warehouse has its own (and All warehouses shows the totals) All / Low stock / Out of stock lists. */
export default function StockPanel({ active }: { active: boolean }) {
  const { can } = useAccess();
  const [whs, setWhs] = useState<Wh[]>([]);
  const [whId, setWhId] = useState<number | "all" | null>(null);

  useEffect(() => {
    if (!active) return;
    api<{ items: Wh[] }>("/inventory/warehouses")
      .then((r) => {
        setWhs(r.items);
        // keep the warehouse you were on; otherwise start on the main one
        setWhId((cur) => (cur === "all" || (cur !== null && r.items.some((w) => w.id === cur)) ? cur : (r.items.find((w) => w.is_default) ?? r.items[0])?.id ?? null));
      })
      .catch(() => {});
  }, [active]);

  const current = whs.find((w) => w.id === whId);
  const isAll = whId === "all";

  return (
    <div className="space-y-4">
      {whs.length > 0 && (
        <select aria-label="Warehouse" className={`${inputCls} !w-64`} value={whId ?? ""} onChange={(e) => setWhId(e.target.value === "all" ? "all" : Number(e.target.value))}>
          <option value="all">All warehouses</option>
          {whs.map((w) => (
            <option key={w.id} value={w.id}>
              {w.name}
            </option>
          ))}
        </select>
      )}
      {/* keyed by warehouse so each one starts on its own All tab with a clean search */}
      {isAll && <WarehouseStock key="all" warehouseId="all" name="All warehouses" isMain={false} canEdit={false} active={active} />}
      {current && !isAll && <WarehouseStock key={current.id} warehouseId={current.id} name={current.name} isMain={current.is_default} canEdit={can("inventory.adjust")} active={active} />}
    </div>
  );
}
