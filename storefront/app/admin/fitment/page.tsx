"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Notice, btnCls, btn2Cls, dangerCls, inputCls } from "../../../components/admin/ui";

type Item = { id: number; name: string; slug: string; models: number };

function NameList({
  title,
  resource,
  note,
}: {
  title: string;
  resource: "vehicle-types" | "makes";
  note?: string;
}) {
  const [items, setItems] = useState<Item[]>([]);
  const [filter, setFilter] = useState("");
  const [newName, setNewName] = useState("");
  const [editId, setEditId] = useState<number | null>(null);
  const [editName, setEditName] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api<Item[]>(`/${resource}`)
      .then(setItems)
      .catch((e) => setError(errMsg(e)));
  }, [resource]);

  useEffect(load, [load]);

  async function run(fn: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      await fn();
      load();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  const shown = items.filter((i) => i.name.toLowerCase().includes(filter.toLowerCase()));

  return (
    <section className="rounded-lg border border-gray-200 bg-white p-4">
      <h2 className="mb-1 text-lg font-semibold">{title}</h2>
      {note && <p className="mb-3 text-xs text-gray-500">{note}</p>}
      {error && <div className="mb-3"><Notice kind="error">{error}</Notice></div>}

      <form
        className="mb-3 flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (!newName.trim()) return;
          run(async () => {
            await api(`/${resource}`, { method: "POST", body: { name: newName.trim() } });
            setNewName("");
          });
        }}
      >
        <input className={inputCls} placeholder={`New ${resource === "makes" ? "make" : "type"} name`} value={newName} onChange={(e) => setNewName(e.target.value)} />
        <button className={btnCls} disabled={busy || !newName.trim()}>Add</button>
      </form>

      {items.length > 8 && (
        <input className={`${inputCls} mb-3`} placeholder="Filter…" value={filter} onChange={(e) => setFilter(e.target.value)} />
      )}

      <ul className="divide-y divide-gray-100">
        {shown.map((i) => (
          <li key={i.id} className="flex items-center gap-2 py-2 text-sm">
            {editId === i.id ? (
              <>
                <input className={inputCls} value={editName} onChange={(e) => setEditName(e.target.value)} autoFocus />
                <button
                  className={btnCls}
                  disabled={busy || !editName.trim()}
                  onClick={() =>
                    run(async () => {
                      await api(`/${resource}/${i.id}`, { method: "PATCH", body: { name: editName.trim() } });
                      setEditId(null);
                    })
                  }
                >
                  Save
                </button>
                <button className={btn2Cls} onClick={() => setEditId(null)}>Cancel</button>
              </>
            ) : (
              <>
                <span className="min-w-0 flex-1 truncate font-medium">{i.name}</span>
                <span className="hidden text-xs text-gray-400 sm:inline">{i.slug}</span>
                <span className="w-20 text-right text-xs text-gray-500">{i.models} models</span>
                <button className={btn2Cls} onClick={() => { setEditId(i.id); setEditName(i.name); }}>Rename</button>
                <button
                  className={dangerCls}
                  disabled={busy}
                  onClick={() => {
                    if (confirm(`Delete "${i.name}"?`)) run(() => api(`/${resource}/${i.id}`, { method: "DELETE" }));
                  }}
                >
                  Delete
                </button>
              </>
            )}
          </li>
        ))}
        {shown.length === 0 && <li className="py-3 text-sm text-gray-500">Nothing here yet.</li>}
      </ul>
    </section>
  );
}

export default function FitmentAdminPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Types &amp; makes</h1>
      <div className="grid gap-6 lg:grid-cols-2">
        <NameList
          title="Machine types"
          resource="vehicle-types"
          note="Renaming keeps the slug. The storefront filter list is still fixed in code, so a brand-new type will not appear in the selector until that list is updated."
        />
        <NameList
          title="Makes"
          resource="makes"
          note="A make can only be deleted once it has no models. Rename keeps its slug."
        />
      </div>
    </div>
  );
}
