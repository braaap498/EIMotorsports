"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

// The cart lives in this browser (localStorage), so it works for guests and
// signed-in customers alike. Prices are never stored here: they are looked up
// again from the store each time the cart is shown.
export type CartLine = { product_id: number; slug: string; qty: number };

const KEY = "ei-cart";
export const MAX_LINE_QTY = 99;

type Ctx = {
  lines: CartLine[];
  count: number; // total pieces
  loaded: boolean;
  // Adds qty of a product, never going past `max` (how many are in stock).
  // Returns how many were actually added.
  add: (line: { product_id: number; slug: string }, qty: number, max?: number) => number;
  setQty: (productId: number, qty: number) => void;
  remove: (productId: number) => void;
  clear: () => void;
};

const CartCtx = createContext<Ctx | null>(null);

export function useCart(): Ctx {
  const c = useContext(CartCtx);
  if (!c) throw new Error("useCart must be used inside <CartProvider>");
  return c;
}

function read(): CartLine[] {
  try {
    const raw = localStorage.getItem(KEY);
    const data = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(data)) return [];
    return data
      .filter((l) => l && Number.isInteger(l.product_id) && typeof l.slug === "string" && Number.isInteger(l.qty) && l.qty > 0)
      .map((l) => ({ product_id: l.product_id, slug: l.slug, qty: Math.min(l.qty, MAX_LINE_QTY) }));
  } catch {
    return [];
  }
}

export function CartProvider({ children }: { children: ReactNode }) {
  const [lines, setLines] = useState<CartLine[]>([]);
  const [loaded, setLoaded] = useState(false);

  // Read the saved cart after the page loads, and follow changes made in other tabs.
  useEffect(() => {
    setLines(read());
    setLoaded(true);
    const onStorage = (e: StorageEvent) => {
      if (e.key === KEY) setLines(read());
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const save = useCallback((next: CartLine[]) => {
    try {
      localStorage.setItem(KEY, JSON.stringify(next));
    } catch {
      /* storage unavailable: the cart still works until the page is closed */
    }
    setLines(next);
  }, []);

  const add = useCallback<Ctx["add"]>(
    (line, qty, max) => {
      const current = read(); // re-read so two tabs never overwrite each other
      const existing = current.find((l) => l.product_id === line.product_id);
      const have = existing?.qty ?? 0;
      const limit = Math.min(max ?? MAX_LINE_QTY, MAX_LINE_QTY);
      const next = Math.min(have + qty, limit);
      const added = Math.max(0, next - have);
      if (added === 0) return 0;
      save(
        existing
          ? current.map((l) => (l.product_id === line.product_id ? { ...l, qty: next } : l))
          : [...current, { product_id: line.product_id, slug: line.slug, qty: next }]
      );
      return added;
    },
    [save]
  );

  const setQty = useCallback(
    (productId: number, qty: number) => {
      const q = Math.max(1, Math.min(Math.floor(qty) || 1, MAX_LINE_QTY));
      save(read().map((l) => (l.product_id === productId ? { ...l, qty: q } : l)));
    },
    [save]
  );

  const remove = useCallback((productId: number) => save(read().filter((l) => l.product_id !== productId)), [save]);
  const clear = useCallback(() => save([]), [save]);

  const value = useMemo<Ctx>(
    () => ({ lines, count: lines.reduce((n, l) => n + l.qty, 0), loaded, add, setQty, remove, clear }),
    [lines, loaded, add, setQty, remove, clear]
  );

  return <CartCtx.Provider value={value}>{children}</CartCtx.Provider>;
}
