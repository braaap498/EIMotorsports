"use client";

import { useEffect, useRef, useState } from "react";
import { api, ApiError, errMsg } from "../../../lib/adminApi";
import { Notice, btnCls, btn2Cls, inputCls, Field } from "../ui";
import { useAccess } from "../AdminAccess";
import ScanBox from "./ScanBox";
import UnknownCode from "./UnknownCode";
import { beep, lookupCode, newId, useLines } from "./shared";

type Flash = { kind: "success" | "error" | "info"; text: string };

/**
 * Receive: scan parts in, then Commit to add them to stock.
 * Ship (eBay / other): scan parts going out on an order that isn't on the website, then Commit
 * to record it. Nothing is saved until Commit, and Commit is applied only once.
 */
export default function ScanSession({ mode, active }: { mode: "receive" | "ship-other"; active: boolean }) {
  const { can } = useAccess();
  const receive = mode === "receive";
  const scanRef = useRef<HTMLInputElement>(null);
  const { lines, add, setQty, remove, clear, units } = useLines();
  const [batchId, setBatchId] = useState(newId);
  const [perScan, setPerScan] = useState(1);
  const [keepQty, setKeepQty] = useState(false);
  const [reference, setReference] = useState("");
  const [note, setNote] = useState("");
  const [unknown, setUnknown] = useState<string | null>(null);
  const [flash, setFlash] = useState<Flash | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  const focus = () => setTimeout(() => scanRef.current?.focus(), 0);
  useEffect(() => {
    if (active) scanRef.current?.focus();
  }, [active]);

  async function handleScan(code: string) {
    setFlash(null);
    setConfirming(false);
    try {
      const r = await lookupCode(code);
      if (!r.found || !r.product) {
        beep(false);
        setUnknown(code);
        return;
      }
      setUnknown(null);
      add(r.product, perScan, r.barcode?.id ?? null);
      beep(true);
      setFlash({ kind: "success", text: `+${perScan}  ${r.product.sku} — ${r.product.title}` });
      if (!keepQty) setPerScan(1);
    } catch (e) {
      beep(false);
      setFlash({ kind: "error", text: errMsg(e) });
    }
    focus();
  }

  const badQty = lines.some((l) => !Number.isInteger(l.qty) || l.qty < 1);
  const needsRef = !receive && !reference.trim();

  async function commit() {
    setBusy(true);
    setFlash(null);
    try {
      await api(receive ? "/inventory/receive" : "/inventory/ship", {
        method: "POST",
        body: {
          batch_id: batchId,
          reference: reference.trim() || null,
          note: note.trim() || null,
          ...(receive ? {} : { order_number: null }),
          lines: lines.map((l) => ({ product_id: l.product.id, quantity: l.qty, barcode_id: l.barcodeId })),
        },
      });
      setFlash({
        kind: "success",
        text: receive
          ? `Received ${units} unit${units === 1 ? "" : "s"}. Stock counts are updated.`
          : `Recorded ${units} unit${units === 1 ? "" : "s"} shipped on ${reference.trim()}.`,
      });
      clear();
      setReference("");
      setNote("");
      setBatchId(newId());
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && e.message.startsWith("Already saved")) {
        // The first try got through but the answer was lost; don't leave a stale list on screen.
        setFlash({ kind: "success", text: "That batch was already saved the first time. Nothing was added twice." });
        clear();
        setBatchId(newId());
      } else {
        setFlash({ kind: "error", text: errMsg(e) });
      }
    } finally {
      setConfirming(false);
      setBusy(false);
      focus();
    }
  }

  return (
    <div className="space-y-4">
      <p className="text-sm text-gray-600">
        {receive
          ? "Scan the shelf label or the box label of each part that arrived. Scan the same part again to add one more, or type a quantity in the box on the right before you scan. Nothing changes in stock until you press Commit."
          : "For an eBay (or other non-website) sale: scan each part you pack. This records what left the shelf. It does not subtract stock again, because the sale already did."}
      </p>

      <div className="grid gap-3 md:grid-cols-[1fr_auto]">
        <ScanBox ref={scanRef} onScan={handleScan} />
        <div className="flex items-center gap-3">
          <label className="text-sm font-medium text-gray-700">
            <span className="block">Quantity for next scan</span>
            <input
              type="number"
              min={1}
              className={`${inputCls} w-28`}
              value={perScan}
              onChange={(e) => setPerScan(Math.max(1, parseInt(e.target.value || "1", 10) || 1))}
            />
          </label>
          <label className="mt-5 flex items-center gap-1.5 text-xs text-gray-600">
            <input type="checkbox" checked={keepQty} onChange={(e) => setKeepQty(e.target.checked)} />
            keep
          </label>
        </div>
      </div>

      {flash && <Notice kind={flash.kind}>{flash.text}</Notice>}
      {unknown && (
        <UnknownCode
          code={unknown}
          canLink={can("inventory.barcodes")}
          onLinked={(p, b) => {
            add(p, perScan, b.id);
            beep(true);
            setUnknown(null);
            setFlash({ kind: "success", text: `Linked, and +${perScan}  ${p.sku} — ${p.title}` });
            if (!keepQty) setPerScan(1);
            focus();
          }}
          onCancel={() => {
            setUnknown(null);
            focus();
          }}
        />
      )}

      <div className="grid gap-3 md:grid-cols-2">
        <Field label={receive ? "Packing slip / PO number (optional)" : "eBay order number (required)"}>
          <input className={inputCls} value={reference} onChange={(e) => setReference(e.target.value)} maxLength={100} />
        </Field>
        <Field label="Note (optional)">
          <input className={inputCls} value={note} onChange={(e) => setNote(e.target.value)} maxLength={500} />
        </Field>
      </div>

      {lines.length === 0 ? (
        <p className="rounded border border-dashed border-gray-300 p-6 text-center text-sm text-gray-500">
          Nothing scanned yet.
        </p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white">
          <table className="w-full text-left text-sm">
            <thead className="bg-gray-50 text-xs uppercase text-gray-500">
              <tr>
                <th className="px-3 py-2">Part</th>
                <th className="px-3 py-2">Bin</th>
                <th className="px-3 py-2">{receive ? "On hand now" : "On hand"}</th>
                <th className="px-3 py-2">{receive ? "Receiving" : "Shipping"}</th>
                {receive && <th className="px-3 py-2">After</th>}
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {lines.map((l) => (
                <tr key={l.product.id}>
                  <td className="px-3 py-2">
                    <div className="flex items-center gap-3">
                      {l.product.thumb ? (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img src={l.product.thumb} alt="" className="h-12 w-12 rounded border border-gray-200 object-contain" />
                      ) : (
                        <div className="h-12 w-12 rounded border border-gray-200 bg-gray-50" />
                      )}
                      <div className="min-w-0">
                        <div className="font-medium">{l.product.title}</div>
                        <div className="text-xs text-gray-500">{l.product.sku}</div>
                      </div>
                    </div>
                  </td>
                  <td className="px-3 py-2 font-semibold">{l.product.bin_location ?? "—"}</td>
                  <td className="px-3 py-2">{l.product.quantity_on_hand}</td>
                  <td className="px-3 py-2">
                    <div className="flex items-center gap-1">
                      <button type="button" className={btn2Cls} onClick={() => setQty(l.product.id, Math.max(1, l.qty - 1))}>
                        −
                      </button>
                      <input
                        type="number"
                        min={1}
                        className={`${inputCls} w-20 text-center`}
                        value={Number.isFinite(l.qty) ? l.qty : ""}
                        onChange={(e) => setQty(l.product.id, parseInt(e.target.value || "0", 10) || 0)}
                      />
                      <button type="button" className={btn2Cls} onClick={() => setQty(l.product.id, l.qty + 1)}>
                        +
                      </button>
                    </div>
                  </td>
                  {receive && <td className="px-3 py-2 font-semibold">{l.product.quantity_on_hand + (l.qty || 0)}</td>}
                  <td className="px-3 py-2 text-right">
                    <button type="button" className="text-sm text-red-700 underline" onClick={() => remove(l.product.id)}>
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-3">
        {!confirming ? (
          <button
            className={btnCls}
            disabled={lines.length === 0 || badQty || needsRef || busy}
            onClick={() => setConfirming(true)}
          >
            {receive
              ? `Commit: receive ${units} unit${units === 1 ? "" : "s"}`
              : `Commit: ${units} unit${units === 1 ? "" : "s"} shipped`}
          </button>
        ) : (
          <>
            <span className="text-sm font-medium">
              {receive
                ? `Add ${units} unit${units === 1 ? "" : "s"} across ${lines.length} part${lines.length === 1 ? "" : "s"} to stock?`
                : `Record ${units} unit${units === 1 ? "" : "s"} shipped on ${reference.trim()}?`}
            </span>
            <button className={btnCls} disabled={busy} onClick={commit}>
              {busy ? "Saving…" : "Yes, commit"}
            </button>
            <button className={btn2Cls} disabled={busy} onClick={() => setConfirming(false)}>
              Go back
            </button>
          </>
        )}
        {lines.length > 0 && !confirming && (
          <button
            className="text-sm underline"
            onClick={() => {
              clear();
              setFlash({ kind: "info", text: "Cleared. Nothing was saved." });
              focus();
            }}
          >
            Clear everything
          </button>
        )}
        {needsRef && lines.length > 0 && <span className="text-sm text-amber-700">Enter the order number to commit.</span>}
        {badQty && <span className="text-sm text-amber-700">Every quantity must be 1 or more.</span>}
      </div>
    </div>
  );
}
