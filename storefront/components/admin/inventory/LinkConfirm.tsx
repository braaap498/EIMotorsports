"use client";

import { Field, btnCls, btn2Cls, inputCls } from "../ui";

export const BARCODE_KINDS: [string, string][] = [
  ["box", "Box / package"],
  ["part", "Part itself"],
  ["upc", "Manufacturer UPC"],
  ["shelf", "Shelf label"],
  ["other", "Other"],
];

/**
 * The one confirm step for linking a scanned barcode to a part. Used on the Barcodes tab
 * and when an unknown code is scanned while receiving or shipping, so it looks and acts the same everywhere.
 */
export default function LinkConfirm({
  code,
  sku,
  title,
  kind,
  onKind,
  onLink,
  onCancel,
  busy,
  note,
  cancelLabel = "Cancel",
}: {
  code: string;
  sku: string;
  title?: string;
  kind: string;
  onKind: (k: string) => void;
  onLink: () => void;
  onCancel: () => void;
  busy?: boolean;
  note?: string;
  cancelLabel?: string;
}) {
  return (
    <div className="space-y-2 rounded-lg border-2 border-amber-300 bg-amber-50 p-3">
      <p className="text-sm">
        Link <span className="rounded bg-white px-1.5 py-0.5 font-mono">{code}</span> to <b>{sku}</b>
        {title ? ` — ${title}` : ""}?
      </p>
      {note && <p className="text-xs text-gray-600">{note}</p>}
      <div className="flex flex-wrap items-end gap-2">
        <Field label="This barcode is on the…">
          <select className={inputCls} value={kind} onChange={(e) => onKind(e.target.value)}>
            {BARCODE_KINDS.map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </select>
        </Field>
        <button className={btnCls} disabled={busy} onClick={onLink}>
          {busy ? "Linking…" : "Link barcode"}
        </button>
        <button className={btn2Cls} disabled={busy} onClick={onCancel}>
          {cancelLabel}
        </button>
      </div>
    </div>
  );
}
