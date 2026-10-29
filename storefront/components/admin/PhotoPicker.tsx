"use client";

import ClearableInput from "./ClearableInput";
import { useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Modal, Notice, btn2Cls, inputCls } from "./ui";
import type { Photo } from "./PhotosPanel";

type FoundProduct = { id: number; sku: string; title: string; photos: { image_key: string; thumb: string | null }[] };
type Attached = { photos: Photo[]; added: number; skipped: number };

// Pick pictures that are already in storage (from another product) and put them on this one.
export default function PhotoPicker({
  productId,
  currentKeys,
  onAdded,
  onClose,
}: {
  productId: number;
  currentKeys: string[]; // pictures already on this product
  onAdded: (photos: Photo[], added: number, skipped: number) => void;
  onClose: () => void;
}) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<FoundProduct[] | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const [searching, setSearching] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // Search shortly after the person stops typing.
  useEffect(() => {
    const term = q.trim();
    if (term.length < 2) {
      setResults(null);
      return;
    }
    let cancelled = false;
    setSearching(true);
    const timer = setTimeout(() => {
      api<{ products: FoundProduct[] }>(`/photo-library?q=${encodeURIComponent(term)}&exclude=${productId}`)
        .then((r) => {
          if (!cancelled) {
            setResults(r.products);
            setError("");
          }
        })
        .catch((e) => {
          if (!cancelled) setError(errMsg(e));
        })
        .finally(() => {
          if (!cancelled) setSearching(false);
        });
    }, 400);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [q, productId]);

  const toggle = (key: string) => setPicked((p) => (p.includes(key) ? p.filter((k) => k !== key) : [...p, key]));

  async function add() {
    if (picked.length === 0) return;
    setBusy(true);
    setError("");
    try {
      const r = await api<Attached>(`/products/${productId}/photos/attach`, { method: "POST", body: { image_keys: picked } });
      onAdded(r.photos, r.added, r.skipped);
    } catch (e) {
      setError(errMsg(e));
      setBusy(false);
    }
  }

  return (
    <Modal title="Add from existing photos" onClose={onClose}>
      <div className="space-y-4">
        <label className="block text-sm font-medium text-gray-700">
          <span className="mb-1 block">Find a product that already has the picture</span>
          <ClearableInput
            className={inputCls}
            autoFocus
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="SKU, title words, or part of a file name"
          />
        </label>

        {error && <Notice kind="error">{error}</Notice>}
        {searching && <p className="text-sm text-gray-500">Searching…</p>}
        {!searching && results && results.length === 0 && <p className="text-sm text-gray-500">No other product with stored photos matches that.</p>}
        {!results && !searching && <p className="text-sm text-gray-500">Type at least two characters. Click pictures to select them.</p>}

        {results && results.length > 0 && (
          <div className="max-h-[50vh] space-y-4 overflow-y-auto pr-1">
            {results.map((p) => (
              <div key={p.id} className="space-y-1.5">
                <p className="text-sm">
                  <span className="font-semibold">{p.sku}</span> <span className="text-gray-600">{p.title}</span>
                </p>
                <ul className="flex flex-wrap gap-2">
                  {p.photos.map((ph) => {
                    const already = currentKeys.includes(ph.image_key);
                    const on = picked.includes(ph.image_key);
                    return (
                      <li key={ph.image_key}>
                        <button
                          type="button"
                          disabled={already || busy}
                          onClick={() => toggle(ph.image_key)}
                          title={already ? "Already on this product" : ph.image_key.split("/").pop()}
                          className={`relative block overflow-hidden rounded border-2 ${on ? "border-blue-600" : "border-gray-200"} ${already ? "opacity-40" : ""}`}
                        >
                          {ph.thumb ? (
                            // eslint-disable-next-line @next/next/no-img-element
                            <img src={ph.thumb} alt="" className="h-20 w-20 object-cover" />
                          ) : (
                            <span className="flex h-20 w-20 items-center justify-center bg-gray-100 text-xs text-gray-500">No picture</span>
                          )}
                          {on && <span className="absolute right-1 top-1 rounded bg-blue-600 px-1 text-xs font-bold text-white">✓</span>}
                          {already && <span className="absolute inset-x-0 bottom-0 bg-black/70 text-[10px] text-white">On this product</span>}
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </div>
            ))}
          </div>
        )}

        <div className="flex flex-wrap items-center justify-end gap-2 border-t border-gray-200 pt-3">
          {picked.length > 0 && <span className="mr-auto text-sm text-gray-600">{picked.length} selected</span>}
          <button type="button" className={btn2Cls} onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="rounded bg-black px-4 py-2 text-sm font-semibold text-white hover:bg-gray-800 disabled:opacity-40"
            disabled={busy || picked.length === 0}
            onClick={() => void add()}
          >
            {busy ? "Adding…" : picked.length > 1 ? `Add ${picked.length} photos` : "Add photo"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
