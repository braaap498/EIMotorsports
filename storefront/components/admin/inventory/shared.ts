"use client";

import { useState } from "react";
import { api } from "../../../lib/adminApi";

export type Prod = {
  id: number;
  sku: string;
  title: string;
  quantity_on_hand: number;
  bin_location: string | null;
  thumb?: string | null;
};
export type BarcodeRef = { id: number; code: string; kind: string };
export type Lookup = {
  found: boolean;
  matched_by?: "barcode" | "sku";
  product?: Prod;
  barcode?: BarcodeRef | null;
};

/** What does this scanned code mean? */
export const lookupCode = (code: string) =>
  api<Lookup>(`/inventory/lookup?code=${encodeURIComponent(code)}`);

/** A fresh id for one "Commit" press. The server refuses a repeat, so a retry can't double-count. */
export function newId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}

/** Short high beep = good scan, low double buzz = problem. Works with the speakers on the PC. */
export function beep(ok: boolean) {
  try {
    const AC =
      window.AudioContext ||
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!AC) return;
    const ctx = new AC();
    const tone = (freq: number, start: number, len: number) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.frequency.value = freq;
      gain.gain.value = 0.12;
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(ctx.currentTime + start);
      osc.stop(ctx.currentTime + start + len);
    };
    if (ok) tone(1200, 0, 0.09);
    else {
      tone(220, 0, 0.18);
      tone(220, 0.24, 0.18);
    }
    setTimeout(() => ctx.close(), 800);
  } catch {
    /* no sound is fine */
  }
}

export type Line = { product: Prod; qty: number; barcodeId: number | null };

/** The pending list on the Receive / Ship screens. Nothing is saved until Commit. */
export function useLines() {
  const [lines, setLines] = useState<Line[]>([]);

  const add = (product: Prod, qty: number, barcodeId: number | null) =>
    setLines((prev) => {
      const i = prev.findIndex((l) => l.product.id === product.id);
      if (i < 0) return [{ product, qty, barcodeId }, ...prev];
      // Same part again: add to its line and move it to the top so the last scan is always visible.
      const next = [...prev];
      const [hit] = next.splice(i, 1);
      return [{ ...hit, qty: hit.qty + qty, barcodeId: hit.barcodeId ?? barcodeId }, ...next];
    });
  const setQty = (productId: number, qty: number) =>
    setLines((prev) => prev.map((l) => (l.product.id === productId ? { ...l, qty } : l)));
  const remove = (productId: number) => setLines((prev) => prev.filter((l) => l.product.id !== productId));
  const clear = () => setLines([]);
  const units = lines.reduce((n, l) => n + l.qty, 0);

  return { lines, add, setQty, remove, clear, units };
}
