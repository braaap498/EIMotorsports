"use client";

import { useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { btn2Cls, inputCls } from "./ui";

/* Pieces per sale for ONE eBay listing: how many units of the product one sale of this listing uses (a 3-pack of a one-part
   product = 3). It applies to every order of the listing, drop shipped or not: the listing shows units available / this number,
   and each order sets aside (and later picks) quantity x this number. A Save button appears as soon as the number is changed. */
export default function PiecesBox({
  listingId,
  value,
  onSaved,
  onError,
  disabled = false,
}: {
  listingId: number;
  value: number;
  onSaved: (pieces: number, queued: number) => void;
  onError?: (message: string) => void;
  disabled?: boolean;
}) {
  const [text, setText] = useState(String(value));
  const [saving, setSaving] = useState(false);
  useEffect(() => setText(String(value)), [value]);

  const n = Math.floor(Number(text));
  const valid = text.trim() !== "" && Number.isFinite(n) && n >= 1 && n <= 999;
  const changed = valid && n !== value;

  async function save() {
    if (!valid) {
      onError?.("Pieces per sale must be a whole number from 1 to 999.");
      return;
    }
    if (!changed) return;
    setSaving(true);
    try {
      const r = await api<{ pieces: number; queued?: number }>(`/availability/listings/${listingId}/pieces`, { method: "POST", body: { pieces: n } });
      onSaved(r.pieces, r.queued ?? 0);
    } catch (e) {
      onError?.(errMsg(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <span className="inline-flex items-center gap-1">
      <input
        type="number"
        min={1}
        max={999}
        step={1}
        value={text}
        disabled={disabled || saving}
        aria-label="Pieces per sale"
        className={`${inputCls} w-16 ${text.trim() !== "" && !valid ? "border-red-500" : ""}`}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") { e.preventDefault(); void save(); }
          if (e.key === "Escape") setText(String(value));
        }}
      />
      {(changed || saving) && (
        <button type="button" className={`${btn2Cls} px-2 py-1 text-xs`} disabled={saving || !valid} onClick={() => void save()}>
          {saving ? "Saving…" : "Save"}
        </button>
      )}
    </span>
  );
}
