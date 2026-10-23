"use client";

import { useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Notice, btnCls, btn2Cls, inputCls, Field } from "../ui";
import ProductPicker, { type PickRow } from "./ProductPicker";
import type { BarcodeRef, Prod } from "./shared";

/**
 * A scanned code that isn't linked to anything yet (usually a new box label).
 * Pick the product once; from then on scanning that box finds it.
 */
export default function UnknownCode({
  code,
  canLink,
  onLinked,
  onCancel,
}: {
  code: string;
  canLink: boolean;
  onLinked: (p: Prod, b: BarcodeRef) => void;
  onCancel: () => void;
}) {
  const [picked, setPicked] = useState<PickRow | null>(null);
  const [kind, setKind] = useState("box");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function link() {
    if (!picked) return;
    setBusy(true);
    setError("");
    try {
      const b = await api<BarcodeRef>(`/inventory/products/${picked.id}/barcodes`, {
        method: "POST",
        body: { code, kind },
      });
      onLinked(
        {
          id: picked.id,
          sku: picked.sku,
          title: picked.title,
          quantity_on_hand: picked.quantity_on_hand ?? 0,
          bin_location: picked.bin_location,
        },
        b
      );
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3 rounded-lg border-2 border-amber-300 bg-amber-50 p-4">
      <p className="text-sm font-semibold text-amber-900">
        The barcode <span className="rounded bg-white px-1.5 py-0.5 font-mono">{code}</span> isn’t linked to a part yet.
      </p>
      {!canLink ? (
        <Notice kind="info">
          Your account can’t link new barcodes. Set this box aside and ask someone with the Barcodes permission.
        </Notice>
      ) : picked ? (
        <div className="space-y-3">
          <p className="text-sm">
            Link it to <b>{picked.sku}</b> — {picked.title}?
          </p>
          <div className="flex flex-wrap items-end gap-2">
            <Field label="This barcode is on the…">
              <select className={inputCls} value={kind} onChange={(e) => setKind(e.target.value)}>
                <option value="box">Box / package</option>
                <option value="part">Part itself</option>
                <option value="upc">Manufacturer UPC</option>
                <option value="shelf">Shelf label</option>
                <option value="other">Other</option>
              </select>
            </Field>
            <button className={btnCls} disabled={busy} onClick={link}>
              {busy ? "Linking…" : "Link and add"}
            </button>
            <button className={btn2Cls} onClick={() => setPicked(null)}>
              Pick a different part
            </button>
          </div>
        </div>
      ) : (
        <ProductPicker onPick={setPicked} />
      )}
      {error && <Notice kind="error">{error}</Notice>}
      <button className="text-sm underline" onClick={onCancel}>
        Skip this scan
      </button>
    </div>
  );
}
