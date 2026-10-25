"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, errMsg } from "../../../lib/adminApi";
import { Badge, Notice, btnCls, btn2Cls, inputCls } from "../ui";
import { useAccess } from "../AdminAccess";
import ScanBox from "./ScanBox";
import UnknownCode from "./UnknownCode";
import { beep, lookupCode, newId, useLines } from "./shared";
import type { Prod } from "./shared";

type PoRow = {
  id: number;
  po_number: number;
  vendor_name: string;
  sent_at: string;
  line_count: number;
  open_lines: number;
  open_units: number;
};
type RLine = {
  id: number;
  product_id: number | null;
  item: string | null;
  description: string;
  quantity: number;
  received_at: string | null;
  received_qty: number | null;
  product: Prod | null;
};
type RIssue = {
  id: number;
  kind: "short" | "over" | "not_on_po";
  sku: string | null;
  description: string | null;
  expected_qty: number;
  received_qty: number;
  acknowledged_by: string | null;
  acknowledged_by_name: string | null;
  acknowledged_at: string;
};
type RPo = {
  id: number;
  po_number: number;
  vendor_name: string;
  status: "open" | "received" | "cancelled";
  sent_at: string | null;
  lines: RLine[];
  issues: RIssue[];
};
type Flash = { kind: "success" | "error" | "info"; text: string };

const KIND_LABEL = { short: "Short", over: "Over", not_on_po: "Not on PO" } as const;

/**
 * Receive tab, "against a PO" mode.
 *  1. A list of purchase orders that were sent and still have parts to receive. Click one.
 *  2. Scan the parts in. Each scan counts toward that part's line on the PO.
 *  3. Press "Finish this line". If the count is exactly what was ordered it is received at once; if it is short
 *     (or over) the person has to confirm and acknowledge it. Their name and the time are saved and the problem
 *     goes on the shortage / overage report. Then the next line is up.
 *  4. Received lines add to on hand (and so to available). When every line is closed the PO becomes "received".
 *  5. A part that is not on the PO can still be scanned. It goes in the "Not on this PO" box and is received,
 *     acknowledged and reported the same way.
 */
export default function ReceivePo({ active }: { active: boolean }) {
  const [rows, setRows] = useState<PoRow[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState("");
  const [listError, setListError] = useState("");
  const [po, setPo] = useState<RPo | null>(null);
  const [listKey, setListKey] = useState(0);

  useEffect(() => {
    const t = setTimeout(() => setQ(qInput.trim()), 300);
    return () => clearTimeout(t);
  }, [qInput]);

  useEffect(() => {
    if (!active || po) return;
    const p = new URLSearchParams();
    if (q) p.set("q", q);
    api<{ items: PoRow[] }>(`/inventory/receiving/pos?${p}`)
      .then((r) => {
        setRows(r.items);
        setListError("");
      })
      .catch((e) => setListError(errMsg(e)))
      .finally(() => setLoaded(true));
  }, [active, po, q, listKey]);

  const open = useCallback(async (id: number) => {
    try {
      setPo(await api<RPo>(`/inventory/receiving/pos/${id}`));
      setListError("");
    } catch (e) {
      setListError(errMsg(e));
    }
  }, []);

  if (po) {
    return (
      <PoScreen
        po={po}
        active={active}
        reload={async () => {
          const fresh = await api<RPo>(`/inventory/receiving/pos/${po.id}`);
          setPo(fresh);
          return fresh;
        }}
        onBack={() => {
          setPo(null);
          setListKey((k) => k + 1);
        }}
      />
    );
  }

  return (
    <div className="space-y-4">
      <p className="text-sm text-gray-600">
        Purchase orders that were sent to the distributor and still have parts to receive. Click one to scan its parts in.
        A PO only shows here once it is marked <strong>sent</strong> on the Purchase orders screen.
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <input
          className={`${inputCls} w-full max-w-xl`}
          value={qInput}
          onChange={(e) => setQInput(e.target.value)}
          placeholder="Search PO number, distributor, part number, SKU or description"
        />
        <button className={btn2Cls} onClick={() => setListKey((k) => k + 1)}>
          Refresh
        </button>
      </div>
      {listError && <Notice kind="error">{listError}</Notice>}
      <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white">
        <table className="w-full text-left text-sm">
          <thead className="bg-gray-50 text-xs uppercase text-gray-500">
            <tr>
              <th className="px-3 py-2">PO #</th>
              <th className="px-3 py-2">Distributor</th>
              <th className="px-3 py-2">Sent</th>
              <th className="px-3 py-2">Lines left</th>
              <th className="px-3 py-2">Units still expected</th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {rows.map((r) => (
              <tr key={r.id} className="cursor-pointer hover:bg-gray-50" onClick={() => open(r.id)}>
                <td className="px-3 py-2 font-semibold">{r.po_number}</td>
                <td className="px-3 py-2">{r.vendor_name}</td>
                <td className="whitespace-nowrap px-3 py-2">{new Date(r.sent_at).toLocaleDateString()}</td>
                <td className="whitespace-nowrap px-3 py-2">
                  {r.open_lines} of {r.line_count}
                  {r.open_lines < r.line_count && (
                    <span className="ml-2">
                      <Badge tone="amber">partly received</Badge>
                    </span>
                  )}
                </td>
                <td className="px-3 py-2">{r.open_units}</td>
                <td className="px-3 py-2 text-right">
                  <button
                    className={btnCls}
                    onClick={(e) => {
                      e.stopPropagation();
                      open(r.id);
                    }}
                  >
                    Receive
                  </button>
                </td>
              </tr>
            ))}
            {loaded && rows.length === 0 && (
              <tr>
                <td colSpan={6} className="px-3 py-6 text-center text-gray-500">
                  {q ? "No sent purchase orders match." : "No sent purchase orders are waiting to be received."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ one PO */
function PoScreen({
  po,
  active,
  reload,
  onBack,
}: {
  po: RPo;
  active: boolean;
  reload: () => Promise<RPo>;
  onBack: () => void;
}) {
  const { can } = useAccess();
  const scanRef = useRef<HTMLInputElement>(null);
  const extras = useLines();
  const batchIds = useRef<Record<number, string>>({});
  const [counts, setCounts] = useState<Record<number, number>>({});
  const [barcodes, setBarcodes] = useState<Record<number, number | null>>({});
  const [currentId, setCurrentId] = useState<number | null>(null);
  const [perScan, setPerScan] = useState(1);
  const [keepQty, setKeepQty] = useState(false);
  const [unknown, setUnknown] = useState<string | null>(null);
  const [flash, setFlash] = useState<Flash | null>(null);
  const [busy, setBusy] = useState(false);
  // The line waiting for its short / over count to be acknowledged.
  const [ackLine, setAckLine] = useState<number | null>(null);
  const [ackChecked, setAckChecked] = useState(false);
  const [ackNote, setAckNote] = useState("");
  // Extras (not on the PO)
  const [extrasBatch, setExtrasBatch] = useState(newId);
  const [extrasAck, setExtrasAck] = useState(false);
  const [extrasNote, setExtrasNote] = useState("");

  const focus = () => setTimeout(() => scanRef.current?.focus(), 0);
  useEffect(() => {
    if (active) scanRef.current?.focus();
  }, [active]);

  const pending = po.lines.filter((l) => !l.received_at);
  const done = po.lines.length - pending.length;
  const cancelled = po.status === "cancelled";
  const complete = po.status === "received";
  // Default to the first line still to do.
  const current = pending.find((l) => l.id === currentId) ?? pending[0] ?? null;
  const unsaved = Object.values(counts).some((n) => n > 0) || extras.lines.length > 0;

  function process(product: Prod, barcodeId: number | null) {
    const line = po.lines.find((l) => l.product_id === product.id && !l.received_at);
    if (line && !cancelled) {
      setCounts((c) => ({ ...c, [line.id]: (c[line.id] ?? 0) + perScan }));
      setBarcodes((b) => ({ ...b, [line.id]: b[line.id] ?? barcodeId }));
      setCurrentId(line.id);
      setAckLine((a) => (a === line.id ? null : a));
      beep(true);
      setFlash({ kind: "success", text: `+${perScan}  ${product.sku} — ${product.title}` });
    } else {
      const onPo = po.lines.some((l) => l.product_id === product.id);
      extras.add(product, perScan, barcodeId);
      beep(false);
      setFlash({
        kind: "info",
        text: onPo
          ? `${product.sku} — ${product.title} is already received on this PO. Added to “Not on this PO”; it will be reported as an overage.`
          : `${product.sku} — ${product.title} is not on this PO. Added to “Not on this PO”; it will be reported as an overage.`,
      });
    }
    if (!keepQty) setPerScan(1);
  }

  async function handleScan(code: string) {
    setFlash(null);
    try {
      const r = await lookupCode(code);
      if (!r.found || !r.product) {
        beep(false);
        setUnknown(code);
        return;
      }
      setUnknown(null);
      process(r.product, r.barcode?.id ?? null);
    } catch (e) {
      beep(false);
      setFlash({ kind: "error", text: errMsg(e) });
    }
    focus();
  }

  function setCount(lineId: number, n: number) {
    setCounts((c) => ({ ...c, [lineId]: Math.max(0, n) }));
    setAckLine((a) => (a === lineId ? null : a));
  }

  async function commitLine(line: RLine, acknowledged: boolean) {
    const n = counts[line.id] ?? 0;
    setBusy(true);
    setFlash(null);
    const batch = (batchIds.current[line.id] ??= newId());
    let ok = false;
    try {
      await api(`/inventory/receiving/pos/${po.id}/lines/${line.id}/receive`, {
        method: "POST",
        body: {
          batch_id: batch,
          quantity: n,
          acknowledged,
          barcode_id: barcodes[line.id] ?? null,
          note: ackNote.trim() || null,
        },
      });
      ok = true;
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && e.message.startsWith("Already saved")) {
        ok = true; // the first try went through; the answer was lost
      } else {
        setFlash({ kind: "error", text: errMsg(e) });
      }
    }
    if (ok) {
      delete batchIds.current[line.id];
      setCounts((c) => {
        const next = { ...c };
        delete next[line.id];
        return next;
      });
      setAckLine(null);
      setAckChecked(false);
      setAckNote("");
      try {
        const fresh = await reload();
        const stillOpen = fresh.lines.filter((l) => !l.received_at);
        // Move on to the next line after this one.
        const idx = fresh.lines.findIndex((l) => l.id === line.id);
        const next = stillOpen.find((l) => fresh.lines.findIndex((x) => x.id === l.id) > idx) ?? stillOpen[0] ?? null;
        setCurrentId(next ? next.id : null);
        setFlash({
          kind: "success",
          text:
            fresh.status === "received"
              ? `Received ${n} of ${line.quantity}. That was the last line: PO ${fresh.po_number} is now received in.`
              : `Received ${n} of ${line.quantity} for ${line.product?.sku ?? line.description}. Stock counts are updated.`,
        });
      } catch (e) {
        setFlash({ kind: "error", text: `Saved, but the PO could not be reloaded: ${errMsg(e)}` });
      }
    }
    setBusy(false);
    focus();
  }

  function finishLine(line: RLine) {
    const n = counts[line.id] ?? 0;
    if (n === line.quantity) {
      commitLine(line, false);
    } else {
      setAckChecked(false);
      setAckNote("");
      setAckLine(line.id);
    }
  }

  async function commitExtras() {
    setBusy(true);
    setFlash(null);
    let ok = false;
    try {
      await api(`/inventory/receiving/pos/${po.id}/extras`, {
        method: "POST",
        body: {
          batch_id: extrasBatch,
          acknowledged: extrasAck,
          note: extrasNote.trim() || null,
          lines: extras.lines.map((l) => ({ product_id: l.product.id, quantity: l.qty, barcode_id: l.barcodeId })),
        },
      });
      ok = true;
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && e.message.startsWith("Already saved")) ok = true;
      else setFlash({ kind: "error", text: errMsg(e) });
    }
    if (ok) {
      const units = extras.units;
      extras.clear();
      setExtrasBatch(newId());
      setExtrasAck(false);
      setExtrasNote("");
      setFlash({ kind: "success", text: `Received ${units} extra unit${units === 1 ? "" : "s"} that were not on the PO. They are on the shortage / overage report.` });
      try {
        await reload();
      } catch {
        /* the list refreshes when you go back */
      }
    }
    setBusy(false);
    focus();
  }

  function back() {
    if (unsaved && !window.confirm("You have scanned parts that are not saved yet. Leave anyway? They will be lost.")) return;
    onBack();
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-semibold">
            Receiving PO {po.po_number} <span className="font-normal text-gray-600">· {po.vendor_name}</span>
          </h2>
          <p className="text-sm text-gray-600">
            {complete
              ? "Every line is received."
              : `${done} of ${po.lines.length} lines received. Scan the parts, then press “Finish this line” to move to the next.`}
          </p>
        </div>
        <button className={btn2Cls} onClick={back}>
          ← Back to the list
        </button>
      </div>

      {cancelled && <Notice kind="error">This purchase order was cancelled. Nothing can be received against it.</Notice>}
      {complete && <Notice kind="success">PO {po.po_number} is received in. You can still add parts that came with it but weren’t on it, below.</Notice>}

      {!cancelled && (
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
      )}

      {flash && <Notice kind={flash.kind}>{flash.text}</Notice>}
      {unknown && (
        <UnknownCode
          code={unknown}
          canLink={can("inventory.barcodes")}
          onLinked={(p, b) => {
            process(p, b.id);
            setUnknown(null);
            focus();
          }}
          onCancel={() => {
            setUnknown(null);
            focus();
          }}
        />
      )}

      {/* ------------------------------------------------ the PO's lines */}
      <div className="space-y-2">
        {po.lines.map((l) => {
          const n = counts[l.id] ?? 0;
          const isCurrent = !l.received_at && current?.id === l.id;
          const diff = n - l.quantity;
          return (
            <div
              key={l.id}
              className={`rounded-lg border bg-white p-3 ${
                isCurrent ? "border-black ring-1 ring-black" : "border-gray-200"
              } ${l.received_at ? "opacity-70" : ""}`}
              onClick={() => !l.received_at && setCurrentId(l.id)}
            >
              <div className="flex flex-wrap items-center gap-3">
                {l.product?.thumb ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={l.product.thumb} alt="" className="h-14 w-14 rounded border border-gray-200 object-contain" />
                ) : (
                  <div className="h-14 w-14 rounded border border-gray-200 bg-gray-50" />
                )}
                <div className="min-w-0 flex-1">
                  <div className="font-medium">{l.product?.title ?? l.description}</div>
                  <div className="text-xs text-gray-500">
                    {[l.product?.sku, l.item].filter(Boolean).join(" · ")}
                    {l.product?.bin_location ? ` · Bin ${l.product.bin_location}` : ""}
                  </div>
                  {!l.product && <div className="text-xs text-red-700">This part no longer exists in the catalog.</div>}
                </div>
                <div className="text-center">
                  <div className="text-xs uppercase text-gray-500">Ordered</div>
                  <div className="text-lg font-semibold">{l.quantity}</div>
                </div>

                {l.received_at ? (
                  <div className="min-w-[9rem] text-right">
                    <Badge tone={l.received_qty !== null && l.received_qty !== l.quantity ? "amber" : "green"}>
                      received {l.received_qty ?? l.quantity} of {l.quantity}
                    </Badge>
                  </div>
                ) : (
                  <>
                    <div className="text-center">
                      <div className="text-xs uppercase text-gray-500">Counted</div>
                      <div className="flex items-center gap-1">
                        <button type="button" className={btn2Cls} disabled={busy || cancelled} onClick={() => setCount(l.id, n - 1)}>
                          −
                        </button>
                        <input
                          type="number"
                          min={0}
                          className={`${inputCls} w-20 text-center`}
                          value={n}
                          disabled={busy || cancelled}
                          onChange={(e) => setCount(l.id, parseInt(e.target.value || "0", 10) || 0)}
                        />
                        <button type="button" className={btn2Cls} disabled={busy || cancelled} onClick={() => setCount(l.id, n + 1)}>
                          +
                        </button>
                      </div>
                    </div>
                    <button
                      className={btnCls}
                      disabled={busy || cancelled || ackLine === l.id}
                      onClick={(e) => {
                        e.stopPropagation();
                        finishLine(l);
                      }}
                    >
                      Finish this line
                    </button>
                  </>
                )}
              </div>

              {!l.received_at && n > 0 && diff !== 0 && ackLine !== l.id && (
                <p className="mt-2 text-sm text-amber-700">
                  {diff < 0 ? `${-diff} short so far.` : `${diff} over.`} Finishing will ask you to confirm it.
                </p>
              )}

              {ackLine === l.id && (
                <div className="mt-3 space-y-2 rounded border border-amber-300 bg-amber-50 p-3 text-sm">
                  <p className="font-semibold text-amber-900">
                    {n === 0
                      ? `None of this part were counted, but ${l.quantity} were ordered.`
                      : diff < 0
                      ? `Short: ${n} counted, ${l.quantity} ordered (${-diff} missing).`
                      : `Over: ${n} counted, ${l.quantity} ordered (${diff} extra).`}
                  </p>
                  <p className="text-amber-900">
                    If you continue, {n} will be added to stock and this line is closed. It goes on the shortage / overage
                    report with your name and the time.
                  </p>
                  <label className="flex items-start gap-2 text-amber-900">
                    <input
                      type="checkbox"
                      className="mt-1"
                      checked={ackChecked}
                      onChange={(e) => setAckChecked(e.target.checked)}
                    />
                    <span>
                      I confirm the count of {n} is right and I acknowledge the {diff < 0 ? "shortage" : "overage"}.
                    </span>
                  </label>
                  <input
                    className={inputCls}
                    placeholder="Note (optional): damaged, back-ordered by the distributor…"
                    value={ackNote}
                    maxLength={300}
                    onChange={(e) => setAckNote(e.target.value)}
                  />
                  <div className="flex gap-2">
                    <button className={btnCls} disabled={!ackChecked || busy} onClick={() => commitLine(l, true)}>
                      {busy ? "Saving…" : `Confirm and receive ${n}`}
                    </button>
                    <button className={btn2Cls} disabled={busy} onClick={() => setAckLine(null)}>
                      Keep counting
                    </button>
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* ------------------------------------------------ parts that were not on the PO */}
      {!cancelled && extras.lines.length > 0 && (
        <div className="space-y-3 rounded-lg border border-blue-300 bg-blue-50 p-3">
          <h3 className="font-semibold text-blue-900">Not on this PO ({extras.units} unit{extras.units === 1 ? "" : "s"})</h3>
          <div className="overflow-x-auto rounded border border-blue-200 bg-white">
            <table className="w-full text-left text-sm">
              <tbody className="divide-y divide-gray-100">
                {extras.lines.map((l) => (
                  <tr key={l.product.id}>
                    <td className="px-3 py-2">
                      <div className="font-medium">{l.product.title}</div>
                      <div className="text-xs text-gray-500">{l.product.sku}</div>
                    </td>
                    <td className="px-3 py-2">
                      <div className="flex items-center gap-1">
                        <button type="button" className={btn2Cls} onClick={() => extras.setQty(l.product.id, Math.max(1, l.qty - 1))}>
                          −
                        </button>
                        <input
                          type="number"
                          min={1}
                          className={`${inputCls} w-20 text-center`}
                          value={Number.isFinite(l.qty) ? l.qty : ""}
                          onChange={(e) => extras.setQty(l.product.id, parseInt(e.target.value || "0", 10) || 0)}
                        />
                        <button type="button" className={btn2Cls} onClick={() => extras.setQty(l.product.id, l.qty + 1)}>
                          +
                        </button>
                      </div>
                    </td>
                    <td className="px-3 py-2 text-right">
                      <button type="button" className="text-sm text-red-700 underline" onClick={() => extras.remove(l.product.id)}>
                        Remove
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <label className="flex items-start gap-2 text-sm text-blue-900">
            <input type="checkbox" className="mt-1" checked={extrasAck} onChange={(e) => setExtrasAck(e.target.checked)} />
            <span>
              These parts arrived but were not ordered on this PO (or were more than ordered). I acknowledge it; my name and the
              time are recorded on the shortage / overage report.
            </span>
          </label>
          <input
            className={inputCls}
            placeholder="Note (optional)"
            value={extrasNote}
            maxLength={300}
            onChange={(e) => setExtrasNote(e.target.value)}
          />
          <div className="flex flex-wrap gap-2">
            <button
              className={btnCls}
              disabled={!extrasAck || busy || extras.lines.some((l) => !Number.isInteger(l.qty) || l.qty < 1)}
              onClick={commitExtras}
            >
              {busy ? "Saving…" : `Receive ${extras.units} extra unit${extras.units === 1 ? "" : "s"} into stock`}
            </button>
            <button className={btn2Cls} disabled={busy} onClick={() => extras.clear()}>
              Clear
            </button>
          </div>
        </div>
      )}

      {po.issues.length > 0 && (
        <div className="space-y-1 rounded-lg border border-gray-200 bg-white p-3 text-sm">
          <h3 className="font-semibold">Recorded on this PO</h3>
          <ul className="space-y-1">
            {po.issues.map((i) => (
              <li key={i.id} className="flex flex-wrap items-center gap-2">
                <Badge tone={i.kind === "short" ? "amber" : i.kind === "over" ? "blue" : "gray"}>{KIND_LABEL[i.kind]}</Badge>
                <span>{i.description ?? i.sku}</span>
                <span className="text-gray-500">
                  ordered {i.expected_qty}, received {i.received_qty} · acknowledged by{" "}
                  {i.acknowledged_by_name ?? i.acknowledged_by} {new Date(i.acknowledged_at).toLocaleString()}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
