"use client";

import { useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Badge, Field, Notice, btnCls, btn2Cls, inputCls } from "../ui";
import ProductPicker from "./ProductPicker";
import ScanBox from "./ScanBox";
import { beep, type Prod } from "./shared";
import {
  SIZES,
  metrics,
  buildPrintHtml,
  buildZpl,
  download,
  renderBarcodePng,
  type LabelSpec,
  type Paper,
  type Rotate,
  type Symbology,
} from "./labels";

type BarcodeRow = { id: number; code: string; kind: string; label: string | null; is_auto: boolean; created_by_email: string | null };
type Loaded = { product: Prod; barcodes: BarcodeRow[] };
type Flash = { kind: "success" | "error" | "info"; text: string };

const KIND_LABEL: Record<string, string> = {
  shelf: "Shelf label",
  box: "Box / package",
  part: "Part itself",
  upc: "Manufacturer UPC",
  other: "Other",
};

/** Link barcodes to a product, set its bin location, and print its shelf label. */
export default function BarcodePanel({ canEdit, active }: { canEdit: boolean; active: boolean }) {
  const [data, setData] = useState<Loaded | null>(null);
  const [flash, setFlash] = useState<Flash | null>(null);
  const [bin, setBin] = useState("");
  const [kind, setKind] = useState("box");
  const [sizeId, setSizeId] = useState("2x1");
  const [sym, setSym] = useState<Symbology>("datamatrix");
  const [copies, setCopies] = useState(1);
  const [paper, setPaper] = useState<Paper>("label");
  const [rotate, setRotate] = useState<Rotate>(0);
  const [label, setLabel] = useState<{ spec: LabelSpec; png: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const scanRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (active && data) scanRef.current?.focus();
  }, [active, data]);

  async function load(id: number, keepLabel = false) {
    try {
      const d = await api<Loaded>(`/inventory/products/${id}/barcodes`);
      setData(d);
      setBin(d.product.bin_location ?? "");
      if (!keepLabel) setLabel(null);
    } catch (e) {
      setFlash({ kind: "error", text: errMsg(e) });
    }
  }

  async function link(code: string) {
    if (!data) return;
    setFlash(null);
    try {
      const r = await api<{ already?: boolean }>(`/inventory/products/${data.product.id}/barcodes`, {
        method: "POST",
        body: { code, kind },
      });
      beep(true);
      setFlash({
        kind: "success",
        text: r.already ? `${code} was already linked to this part.` : `Linked ${code} to ${data.product.sku}.`,
      });
      await load(data.product.id);
    } catch (e) {
      beep(false);
      setFlash({ kind: "error", text: errMsg(e) });
    }
    setTimeout(() => scanRef.current?.focus(), 0);
  }

  async function unlink(b: BarcodeRow) {
    if (!data) return;
    if (!window.confirm(`Unlink ${b.code}? Scanning it will stop finding this part.`)) return;
    try {
      await api(`/inventory/barcodes/${b.id}`, { method: "DELETE" });
      setFlash({ kind: "success", text: `Unlinked ${b.code}.` });
      await load(data.product.id);
    } catch (e) {
      setFlash({ kind: "error", text: errMsg(e) });
    }
  }

  async function saveBin() {
    if (!data) return;
    try {
      const r = await api<{ bin_location: string | null }>(`/inventory/products/${data.product.id}/bin`, {
        method: "PUT",
        body: { bin_location: bin.trim() || null },
      });
      setData({ ...data, product: { ...data.product, bin_location: r.bin_location } });
      setLabel(null);
      setFlash({ kind: "success", text: r.bin_location ? `Bin location saved: ${r.bin_location}` : "Bin location cleared." });
    } catch (e) {
      setFlash({ kind: "error", text: errMsg(e) });
    }
  }

  async function makeLabel() {
    if (!data) return;
    setBusy(true);
    setFlash(null);
    try {
      const r = await api<{ code: string; created: boolean }>(`/inventory/products/${data.product.id}/shelf-barcode`, {
        method: "POST",
      });
      const spec: LabelSpec = { code: r.code, sku: data.product.sku, title: data.product.title, bin: data.product.bin_location };
      const png = await renderBarcodePng(r.code, sym);
      setLabel({ spec, png });
      if (r.created) await load(data.product.id, true);
    } catch (e) {
      setFlash({
        kind: "error",
        text:
          errMsg(e).toLowerCase().includes("bwip")
            ? "The barcode drawing library isn’t installed yet (npm install bwip-js). You can still download the ZPL file."
            : errMsg(e),
      });
    } finally {
      setBusy(false);
    }
  }

  const size = SIZES.find((s) => s.id === sizeId) ?? SIZES[0];
  const M = metrics(size);

  function printLabel() {
    if (!label) return;
    const w = window.open("", "_blank", "width=700,height=500");
    if (!w) {
      setFlash({ kind: "error", text: "The print window was blocked. Allow pop-ups for this site and try again." });
      return;
    }
    w.document.open();
    w.document.write(buildPrintHtml(label.png, label.spec, size, sym, copies, paper, rotate));
    w.document.close();
  }

  async function zpl() {
    if (!data) return;
    setBusy(true);
    try {
      let spec: LabelSpec | undefined = label?.spec;
      if (!spec) {
        const r = await api<{ code: string }>(`/inventory/products/${data.product.id}/shelf-barcode`, { method: "POST" });
        spec = { code: r.code, sku: data.product.sku, title: data.product.title, bin: data.product.bin_location };
      }
      download(`label-${data.product.sku}.zpl`, buildZpl(spec, size, sym, copies));
      await load(data.product.id, true);
    } catch (e) {
      setFlash({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-5">
      <p className="text-sm text-gray-600">
        Find a part, then link every barcode that should mean it (the box label, the manufacturer’s UPC, a label on the part).
        Each barcode can belong to only one part. Print the part’s shelf label here too.
      </p>

      {!data ? (
        <ProductPicker browse onPick={(p) => load(p.id)} />
      ) : (
        <div className="space-y-5">
          <div className="flex flex-wrap items-start justify-between gap-3 rounded-lg border border-gray-200 bg-white p-4">
            <div className="flex items-center gap-3">
              {data.product.thumb ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={data.product.thumb} alt="" className="h-16 w-16 rounded border border-gray-200 object-contain" />
              ) : (
                <div className="h-16 w-16 rounded border border-gray-200 bg-gray-50" />
              )}
              <div className="text-sm">
                <div className="font-semibold">{data.product.title}</div>
                <div className="text-gray-500">
                  {data.product.sku} · on hand {data.product.quantity_on_hand}
                </div>
              </div>
            </div>
            <button
              className={btn2Cls}
              onClick={() => {
                setData(null);
                setFlash(null);
              }}
            >
              Pick a different part
            </button>
          </div>

          {flash && <Notice kind={flash.kind}>{flash.text}</Notice>}

          <section className="space-y-2">
            <h3 className="text-sm font-semibold uppercase tracking-wide text-gray-500">Bin / shelf location</h3>
            <div className="flex flex-wrap items-end gap-2">
              <Field label="Printed large on the shelf label (e.g. A-12-3)">
                <input
                  className={inputCls}
                  value={bin}
                  maxLength={40}
                  disabled={!canEdit}
                  onChange={(e) => setBin(e.target.value)}
                />
              </Field>
              {canEdit && (
                <button className={btn2Cls} onClick={saveBin}>
                  Save location
                </button>
              )}
            </div>
          </section>

          <section className="space-y-2">
            <h3 className="text-sm font-semibold uppercase tracking-wide text-gray-500">
              Linked barcodes ({data.barcodes.length})
            </h3>
            {data.barcodes.length === 0 ? (
              <p className="text-sm text-gray-500">None yet.</p>
            ) : (
              <ul className="divide-y divide-gray-100 rounded-lg border border-gray-200 bg-white">
                {data.barcodes.map((b) => (
                  <li key={b.id} className="flex items-center justify-between gap-3 px-4 py-2 text-sm">
                    <span className="min-w-0">
                      <span className="font-mono">{b.code}</span>
                      <span className="ml-2">
                        <Badge tone={b.kind === "shelf" ? "blue" : "gray"}>{KIND_LABEL[b.kind] ?? b.kind}</Badge>
                      </span>
                    </span>
                    {canEdit && (
                      <button className="text-red-700 underline" onClick={() => unlink(b)}>
                        Unlink
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            )}
            {canEdit && (
              <div className="space-y-2 rounded-lg border border-gray-200 bg-white p-4">
                <div className="text-sm font-medium">Link another barcode: scan it here</div>
                <div className="grid gap-2 md:grid-cols-[1fr_auto]">
                  <ScanBox ref={scanRef} onScan={link} placeholder="Scan the box / part / UPC label…" />
                  <select className={inputCls} value={kind} onChange={(e) => setKind(e.target.value)}>
                    <option value="box">Box / package</option>
                    <option value="part">Part itself</option>
                    <option value="upc">Manufacturer UPC</option>
                    <option value="other">Other</option>
                    <option value="shelf">Shelf label</option>
                  </select>
                </div>
              </div>
            )}
          </section>

          {canEdit && (
            <section className="space-y-3">
              <h3 className="text-sm font-semibold uppercase tracking-wide text-gray-500">Shelf label</h3>
              <div className="flex flex-wrap items-end gap-3">
                <Field label="Label size">
                  <select className={inputCls} value={sizeId} onChange={(e) => setSizeId(e.target.value)}>
                    {SIZES.map((s) => (
                      <option key={s.id} value={s.id}>
                        {s.label}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="Barcode type">
                  <select
                    className={inputCls}
                    value={sym}
                    onChange={(e) => {
                      setSym(e.target.value as Symbology);
                      setLabel(null);
                    }}
                  >
                    <option value="datamatrix">Data Matrix (2D, small)</option>
                    <option value="qrcode">QR code (2D)</option>
                    <option value="code128">Code 128 (1D lines)</option>
                  </select>
                </Field>
                <Field label="Copies">
                  <input
                    type="number"
                    min={1}
                    max={100}
                    className={`${inputCls} w-24`}
                    value={copies}
                    onChange={(e) => setCopies(Math.min(100, Math.max(1, parseInt(e.target.value || "1", 10) || 1)))}
                  />
                </Field>
                <Field label="Print on">
                  <select className={inputCls} value={paper} onChange={(e) => setPaper(e.target.value as Paper)}>
                    <option value="label">Label printer (one label per page)</option>
                    <option value="letter">Regular paper, 8.5 × 11 in (labels at actual size)</option>
                  </select>
                </Field>
                <Field label="Rotate label">
                  <select className={inputCls} value={rotate} onChange={(e) => setRotate(parseInt(e.target.value, 10) as Rotate)}>
                    <option value={0}>None</option>
                    <option value={90}>90° clockwise</option>
                    <option value={270}>90° counter-clockwise</option>
                  </select>
                </Field>
                <button className={btnCls} disabled={busy} onClick={makeLabel}>
                  {busy ? "Working…" : "Make / show shelf label"}
                </button>
              </div>

              {label && (
                <div className="space-y-3">
                  <div
                    className={`flex gap-2 overflow-hidden border-2 border-dashed border-gray-400 bg-white p-2 ${
                      sym === "code128" ? "flex-col" : "items-center"
                    }`}
                    style={{ width: `${size.wIn * 96}px`, height: `${size.hIn * 96}px` }}
                  >
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={label.png}
                      alt="barcode"
                      style={
                        sym === "code128"
                          ? { width: "100%", height: `${size.hIn * 96 * 0.34}px`, objectFit: "fill", flex: "none" }
                          : { height: `${size.hIn * 96 * M.img}px`, width: `${size.hIn * 96 * M.img}px`, imageRendering: "pixelated", flex: "none" }
                      }
                    />
                    <div className="min-w-0 text-black">
                      <div className="font-extrabold leading-none" style={{ fontSize: `${size.hIn * 96 * M.bin}px` }}>
                        {label.spec.bin ?? ""}
                      </div>
                      <div className="mt-1 leading-tight" style={{ fontSize: `${size.hIn * 96 * M.title}px` }}>
                        {label.spec.title}
                      </div>
                      <div style={{ fontSize: `${size.hIn * 96 * M.small}px` }}>
                        {label.spec.sku} · {label.spec.code}
                      </div>
                    </div>
                  </div>
                  <p className="text-xs text-gray-500">
                    Rough preview. The printed label uses the exact label size
                    {sym === "code128" ? " with the barcode across the top" : ""}.
                  </p>
                  <div className="flex flex-wrap gap-2">
                    <button className={btnCls} onClick={printLabel}>
                      Print label
                    </button>
                    <button className={btn2Cls} disabled={busy} onClick={zpl}>
                      Download ZPL file
                    </button>
                  </div>
                </div>
              )}
              {!label && (
                <div>
                  <button className="text-sm underline" disabled={busy} onClick={zpl}>
                    Skip the preview and download the ZPL file
                  </button>
                </div>
              )}
              <p className="text-xs text-gray-500">
                “Print label” opens a page sized to the label: choose your Zebra printer and make sure its paper size matches. With “Regular paper” it lays the labels out at their real size on an 8.5 × 11 in sheet instead (dashed lines show the edges), so you can test the design on any printer before the Zebra arrives; print it at 100% / actual size.
                A ZPL file prints straight to a Zebra printer, for example from PowerShell:{" "}
                <span className="font-mono">copy /b label-SKU.zpl \\localhost\ZebraShareName</span> (the printer must be shared
                under that name).
              </p>
            </section>
          )}
        </div>
      )}
    </div>
  );
}
