"use client";

import { useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Notice, btn2Cls, btnCls, inputCls } from "../ui";
import { useAccess } from "../AdminAccess";
import ScanBox from "./ScanBox";
import UnknownCode from "./UnknownCode";
import { beep, lookupCode, type Prod } from "./shared";

type StockRow = { id: number; sku: string; title: string; quantity: number; bin_location: string | null };
type Flash = { kind: "success" | "error" | "info"; text: string };
type Current = { product: Prod; here: number; inWarehouse: boolean };

/**
 * Scan a part's barcode, see how many are counted in this warehouse, type the real number (or press +1 / -1)
 * and press Enter. It saves through the same call the Stock table uses, so History records who changed it and
 * the eBay / Amazon quantities follow. Then the scan box is ready for the next part.
 */
export default function ScanAdjust({ warehouseId, name, onChanged }: { warehouseId: number; name: string; onChanged: () => void }) {
  const { can } = useAccess();
  const scanRef = useRef<HTMLInputElement>(null);
  const countRef = useRef<HTMLInputElement>(null);
  const [cur, setCur] = useState<Current | null>(null);
  const [count, setCount] = useState("");
  const [unknown, setUnknown] = useState<string | null>(null);
  const [flash, setFlash] = useState<Flash | null>(null);
  const [busy, setBusy] = useState(false);

  const focusScan = () => setTimeout(() => scanRef.current?.focus(), 0);
  useEffect(() => {
    scanRef.current?.focus();
  }, []);
  useEffect(() => {
    if (cur) setTimeout(() => countRef.current?.select(), 0);
  }, [cur]);

  /** How many of this part are counted in THIS warehouse (the product's own number is the total of all of them). */
  async function open(p: Prod) {
    try {
      const p2 = new URLSearchParams({ limit: "50", offset: "0", view: "all", status: "all", q: p.sku });
      const r = await api<{ items: StockRow[] }>(`/inventory/warehouses/${warehouseId}/stock?${p2}`);
      const row = r.items.find((x) => x.id === p.id);
      setCur({ product: p, here: row ? row.quantity : 0, inWarehouse: !!row });
      setCount(String(row ? row.quantity : 0));
      setUnknown(null);
      setFlash(null);
      beep(true);
    } catch (e) {
      beep(false);
      setFlash({ kind: "error", text: errMsg(e) });
      focusScan();
    }
  }

  async function handleScan(code: string) {
    setFlash(null);
    setCur(null);
    try {
      const r = await lookupCode(code);
      if (!r.found || !r.product) {
        beep(false);
        setUnknown(code);
        return;
      }
      await open(r.product);
    } catch (e) {
      beep(false);
      setFlash({ kind: "error", text: errMsg(e) });
      focusScan();
    }
  }

  async function save() {
    if (!cur) return;
    const n = Number(count);
    if (count.trim() === "" || !Number.isInteger(n) || n < 0) {
      setFlash({ kind: "error", text: "Type a whole number, 0 or more." });
      return;
    }
    if (n === cur.here && cur.inWarehouse) {
      setFlash({ kind: "info", text: `${cur.product.sku}: already ${n}, nothing changed.` });
      setCur(null);
      focusScan();
      return;
    }
    setBusy(true);
    setFlash(null);
    try {
      await api(`/inventory/warehouses/${warehouseId}/stock/${cur.product.id}`, { method: "PUT", body: { quantity: n } });
      beep(true);
      setFlash({ kind: "success", text: `${cur.product.sku}: ${cur.here} → ${n} in ${name}` });
      setCur(null);
      onChanged();
      focusScan();
    } catch (e) {
      beep(false);
      setFlash({ kind: "error", text: errMsg(e) }); // stay on the count box so it can be fixed and retried
    } finally {
      setBusy(false);
    }
  }

  const bump = (d: number) => {
    const n = Number(count);
    setCount(String(Math.max(0, (Number.isInteger(n) ? n : 0) + d)));
  };

  return (
    <div className="space-y-3 rounded border border-gray-200 bg-white p-3">
      <p className="text-sm text-gray-600">
        Scan a part to set its count in <b>{name}</b>. Type the real number and press Enter to save.
      </p>
      <ScanBox ref={scanRef} onScan={(c) => void handleScan(c)} placeholder="Scan the part's barcode…" disabled={busy} />

      {flash && <Notice kind={flash.kind}>{flash.text}</Notice>}

      {unknown && (
        <UnknownCode
          code={unknown}
          canLink={can("inventory.barcodes")}
          onLinked={(p) => void open(p)}
          onCancel={() => {
            setUnknown(null);
            focusScan();
          }}
        />
      )}

      {cur && (
        <div className="space-y-3 rounded border border-gray-300 p-3">
          <div className="text-sm">
            <div className="font-medium">{cur.product.sku}</div>
            <div className="text-gray-600">{cur.product.title}</div>
            {cur.product.bin_location && <div className="text-gray-500">Bin/Shelf: {cur.product.bin_location}</div>}
            <div className="mt-1">
              Counted in {name}: <b>{cur.here}</b>
              {!cur.inWarehouse && <span className="text-gray-500"> (not in this warehouse yet, saving adds it)</span>}
            </div>
          </div>
          <form
            className="flex flex-wrap items-end gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              void save();
            }}
          >
            <label className="text-sm font-medium text-gray-700">
              <span className="mb-1 block">Real count</span>
              <input
                ref={countRef}
                className={`${inputCls} w-28`}
                type="number"
                min={0}
                inputMode="numeric"
                value={count}
                onChange={(e) => setCount(e.target.value)}
                disabled={busy}
              />
            </label>
            <button type="button" className={btn2Cls} onClick={() => bump(-1)} disabled={busy}>
              −1
            </button>
            <button type="button" className={btn2Cls} onClick={() => bump(1)} disabled={busy}>
              +1
            </button>
            <button type="submit" className={btnCls} disabled={busy}>
              {busy ? "Saving…" : "Save"}
            </button>
            <button
              type="button"
              className={btn2Cls}
              onClick={() => {
                setCur(null);
                focusScan();
              }}
              disabled={busy}
            >
              Cancel
            </button>
          </form>
        </div>
      )}
    </div>
  );
}
