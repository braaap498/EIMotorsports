"use client";

import { useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { btn2Cls, inputCls } from "./ui";

type Item = { id: number; name: string; fits: boolean };
type AddResult = { created: boolean; matched: boolean; item?: { id: number; name: string }; similar?: { id: number; name: string }[] };

/**
 * The Part type field: a dropdown of the standard list, not free text. It offers the types that belong to the product's
 * category first ("Show all types" adds the rest), and "Add a type…" puts a new one on the list (under this category).
 * The product still stores the type's name, so nothing that reads that field changes.
 */
export default function PartTypePicker({
  value,
  onChange,
  categoryId,
  categoryName,
  disabled,
}: {
  value: string;
  onChange: (v: string) => void;
  categoryId: number | null;
  categoryName?: string;
  disabled?: boolean;
}) {
  const [items, setItems] = useState<Item[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const [adding, setAdding] = useState(false);
  const [newName, setNewName] = useState("");
  const [similar, setSimilar] = useState<{ id: number; name: string }[]>([]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  // The type that was selected (by id), so that when it is renamed on the Rename types page (another tab) the selection follows it
  // to its new name. The product stores the type's NAME, so without this the product kept the old name and showed "not on the list".
  const selectedId = useRef<number | null>(null);
  const valueRef = useRef(value);
  valueRef.current = value;

  async function load() {
    try {
      const r = await api<{ items: Item[] }>(`/part-types${categoryId ? `?category_id=${categoryId}` : ""}`);
      setItems(r.items);
      const cur = valueRef.current.trim().toLowerCase();
      const still = r.items.find((i) => i.name.toLowerCase() === cur);
      if (still) {
        selectedId.current = still.id;
      } else if (cur && selectedId.current !== null) {
        const renamed = r.items.find((i) => i.id === selectedId.current);
        if (renamed) onChange(renamed.name); // it was renamed: select it under its new name
      }
    } catch (e) {
      setMsg(errMsg(e));
    } finally {
      setLoaded(true);
    }
  }
  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [categoryId]);

  // coming back from the Rename types tab: read the list again
  useEffect(() => {
    const again = () => {
      if (document.visibilityState === "visible") void load();
    };
    document.addEventListener("visibilitychange", again);
    window.addEventListener("focus", again);
    return () => {
      document.removeEventListener("visibilitychange", again);
      window.removeEventListener("focus", again);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [categoryId]);

  const current = value.trim();
  const currentItem = items.find((i) => i.name.toLowerCase() === current.toLowerCase());
  const onList = !!currentItem;
  const fitting = items.filter((i) => i.fits);
  const others = items.filter((i) => !i.fits && (showAll || i.id === currentItem?.id));
  const hiddenCount = items.filter((i) => !i.fits).length;
  const notForCategory = !!currentItem && !currentItem.fits && !!categoryId;

  async function add(force = false) {
    const name = newName.trim();
    if (!name) return;
    setBusy(true);
    setMsg("");
    try {
      const r = await api<AddResult>("/part-types", { method: "POST", body: { name, category_id: categoryId, force } });
      if (r.item) {
        await load();
        onChange(r.item.name);
        setAdding(false);
        setNewName("");
        setSimilar([]);
        if (r.matched) setMsg(`"${r.item.name}" was already on the list, so that one is used.`);
      } else if (r.similar) {
        setSimilar(r.similar);
      }
    } catch (e) {
      setMsg(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  async function listUnderCategory() {
    if (!currentItem || !categoryId) return;
    setBusy(true);
    setMsg("");
    try {
      await api(`/part-types/${currentItem.id}/link`, { method: "POST", body: { category_id: categoryId } });
      await load();
    } catch (e) {
      setMsg(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <select className={inputCls} disabled={disabled} value={onList ? currentItem!.name : current} onChange={(e) => onChange(e.target.value)}>
        <option value="">{loaded ? "(none)" : "Loading…"}</option>
        {current && !onList && loaded && <option value={current}>{current} (not on the list)</option>}
        {fitting.map((i) => (
          <option key={i.id} value={i.name}>
            {i.name}
          </option>
        ))}
        {others.length > 0 && (
          <optgroup label="Other categories">
            {others.map((i) => (
              <option key={i.id} value={i.name}>
                {i.name}
              </option>
            ))}
          </optgroup>
        )}
      </select>

      {!disabled && (
        <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs font-normal text-gray-600">
          {hiddenCount > 0 && (
            <label className="flex items-center gap-1">
              <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} />
              Show all types
            </label>
          )}
          {!adding && (
            <button type="button" className="underline" onClick={() => { setAdding(true); setMsg(""); }}>
              Add a type…
            </button>
          )}
          <a href="/admin/part-types" target="_blank" rel="noreferrer" className="underline">
            Rename types
          </a>
        </div>
      )}

      {adding && (
        <div className="mt-2 rounded border border-gray-200 bg-gray-50 p-2">
          <div className="flex flex-wrap items-center gap-2">
            <input
              className={`${inputCls} min-w-[12rem] flex-1`}
              value={newName}
              maxLength={80}
              placeholder="New part type"
              autoFocus
              onChange={(e) => { setNewName(e.target.value); setSimilar([]); }}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault(); // this sits inside the product form: Enter must not save the product
                  void add();
                }
              }}
            />
            <button type="button" className={`${btn2Cls} !px-3 !py-1 text-sm`} disabled={busy || !newName.trim()} onClick={() => void add()}>
              Add
            </button>
            <button type="button" className="text-xs underline" onClick={() => { setAdding(false); setNewName(""); setSimilar([]); }}>
              Cancel
            </button>
          </div>
          {categoryName && <p className="mt-1 text-xs font-normal text-gray-500">It will be listed under {categoryName}.</p>}
          {similar.length > 0 && (
            <div className="mt-2 text-xs font-normal text-gray-700">
              A similar type is already on the list. Use one of these, or add yours anyway:
              <div className="mt-1 flex flex-wrap gap-2">
                {similar.map((s) => (
                  <button
                    key={s.id}
                    type="button"
                    className="rounded border border-gray-300 bg-white px-2 py-1"
                    onClick={() => { onChange(s.name); setAdding(false); setNewName(""); setSimilar([]); }}
                  >
                    {s.name}
                  </button>
                ))}
                <button type="button" className="rounded border border-gray-400 bg-white px-2 py-1 font-semibold" disabled={busy} onClick={() => void add(true)}>
                  Add “{newName.trim()}” anyway
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {loaded && current && !onList && (
        <p className="mt-1 text-xs font-normal text-amber-800">This value is not on the Part type list. Pick one from the list, or add it.</p>
      )}
      {notForCategory && (
        <p className="mt-1 text-xs font-normal text-amber-800">
          This type isn&apos;t listed under {categoryName ?? "this category"}.{" "}
          {!disabled && (
            <button type="button" className="underline" disabled={busy} onClick={() => void listUnderCategory()}>
              List it under this category
            </button>
          )}
        </p>
      )}
      {msg && <p className="mt-1 text-xs font-normal text-gray-600">{msg}</p>}
    </div>
  );
}
