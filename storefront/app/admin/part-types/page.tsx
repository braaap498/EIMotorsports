"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { api, errMsg } from "../../../lib/adminApi";
import { Badge, Field, Notice, btnCls, btn2Cls, inputCls } from "../../../components/admin/ui";

type Row = { id: number; name: string; products: number; category_ids: number[] };
type Cat = { id: number; parent_id: number | null; name: string };
type AddResult = { created: boolean; matched: boolean; item?: { id: number; name: string }; similar?: { id: number; name: string }[] };
type RenameResult = { ok: boolean; item: { id: number; name: string }; products_changed: number };

export default function PartTypesPage() {
  const [rows, setRows] = useState<Row[]>([]);
  const [cats, setCats] = useState<Cat[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [q, setQ] = useState("");
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);

  // add
  const [newName, setNewName] = useState("");
  const [newCat, setNewCat] = useState("");
  const [similar, setSimilar] = useState<{ id: number; name: string }[]>([]);
  const [busy, setBusy] = useState(false);

  // rename
  const [editId, setEditId] = useState<number | null>(null);
  const [editName, setEditName] = useState("");

  const load = useCallback(async () => {
    try {
      const r = await api<{ items: Row[] }>("/part-types/manage");
      setRows(r.items);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    void load();
    api<Cat[]>("/categories").then(setCats).catch((e) => setMsg({ kind: "error", text: errMsg(e) }));
  }, [load]);

  // "Engine > Pistons" style names, so two categories with the same name can be told apart.
  const catPath = useMemo(() => {
    const byId = new Map(cats.map((c) => [c.id, c]));
    const path = (id: number): string => {
      const out: string[] = [];
      let c = byId.get(id);
      let guard = 0;
      while (c && guard++ < 10) {
        out.unshift(c.name);
        c = c.parent_id ? byId.get(c.parent_id) : undefined;
      }
      return out.join(" > ");
    };
    return path;
  }, [cats]);

  const sortedCats = useMemo(() => cats.map((c) => ({ id: c.id, label: catPath(c.id) })).sort((a, b) => a.label.localeCompare(b.label)), [cats, catPath]);

  const shown = rows.filter((r) => !q.trim() || r.name.toLowerCase().includes(q.trim().toLowerCase()));

  async function add(force = false) {
    const name = newName.trim();
    if (!name) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await api<AddResult>("/part-types", { method: "POST", body: { name, category_id: newCat ? Number(newCat) : null, force } });
      if (r.item) {
        setMsg({
          kind: "success",
          text: r.matched ? `"${r.item.name}" was already on the list, so nothing new was added.` : `"${r.item.name}" added.`,
        });
        setNewName("");
        setSimilar([]);
        await load();
      } else if (r.similar) {
        setSimilar(r.similar);
      }
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy(false);
    }
  }

  async function saveRename(row: Row) {
    const name = editName.trim();
    if (!name || name === row.name) {
      setEditId(null);
      return;
    }
    if (row.products > 0 && !confirm(`Rename "${row.name}" to "${name}"?\n\n${row.products} product${row.products === 1 ? "" : "s"} using it will show the new name.`)) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await api<RenameResult>(`/part-types/${row.id}`, { method: "PATCH", body: { name } });
      setMsg({ kind: "success", text: `Renamed to "${r.item.name}". ${r.products_changed} product${r.products_changed === 1 ? "" : "s"} updated.` });
      setEditId(null);
      await load();
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy(false);
    }
  }

  async function linkTo(row: Row, categoryId: string) {
    if (!categoryId) return;
    setBusy(true);
    setMsg(null);
    try {
      await api(`/part-types/${row.id}/link`, { method: "POST", body: { category_id: Number(categoryId) } });
      await load();
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-2xl font-semibold">Part types</h1>
        <div className="flex gap-4 text-sm">
          <Link href="/admin/categories" className="underline">
            Edit categories
          </Link>
          <Link href="/admin/products" className="underline">
            Back to products
          </Link>
        </div>
      </div>
      <p className="text-sm text-gray-600">
        The standard list used by the Part type dropdown on each product. Renaming a type changes it on every product that uses it. Types are
        offered for the categories they are listed under.
      </p>
      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}

      <form
        className="space-y-3 rounded-lg border border-gray-200 bg-white p-4"
        onSubmit={(e) => {
          e.preventDefault();
          void add();
        }}
      >
        <h2 className="font-semibold">Add a part type</h2>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Name">
            <input
              className={inputCls}
              value={newName}
              maxLength={80}
              onChange={(e) => {
                setNewName(e.target.value);
                setSimilar([]);
              }}
            />
          </Field>
          <Field label="List under category (optional)">
            <select className={inputCls} value={newCat} onChange={(e) => setNewCat(e.target.value)}>
              <option value="">Not listed under a category yet</option>
              {sortedCats.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.label}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <p className="text-xs text-gray-500">A type with no category shows under every category until you list it under one.</p>
        {similar.length > 0 && (
          <div className="text-xs text-gray-700">
            A similar type is already on the list: {similar.map((s) => s.name).join(", ")}.{" "}
            <button type="button" className="font-semibold underline" disabled={busy} onClick={() => void add(true)}>
              Add “{newName.trim()}” anyway
            </button>
          </div>
        )}
        <button className={btnCls} disabled={busy || !newName.trim()}>
          Add part type
        </button>
      </form>

      <div className="flex flex-wrap items-end gap-3">
        <Field label="Search">
          <input className={inputCls} value={q} onChange={(e) => setQ(e.target.value)} placeholder="Type name" />
        </Field>
        <span className="pb-2 text-sm text-gray-500">{loaded ? `${shown.length} of ${rows.length}` : "Loading…"}</span>
      </div>

      <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white">
        <table className="w-full text-left text-sm">
          <thead className="bg-gray-50 text-xs uppercase text-gray-500">
            <tr>
              <th className="px-3 py-2">Part type</th>
              <th className="px-3 py-2">Products</th>
              <th className="px-3 py-2">Listed under</th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {shown.map((r) => (
              <tr key={r.id} className="align-top">
                <td className="px-3 py-2">
                  {editId === r.id ? (
                    <div className="flex flex-wrap items-center gap-2">
                      <input
                        className={`${inputCls} min-w-[14rem]`}
                        value={editName}
                        maxLength={80}
                        autoFocus
                        onChange={(e) => setEditName(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") {
                            e.preventDefault();
                            void saveRename(r);
                          } else if (e.key === "Escape") setEditId(null);
                        }}
                      />
                      <button type="button" className={`${btnCls} !px-3 !py-1`} disabled={busy || !editName.trim()} onClick={() => void saveRename(r)}>
                        Save
                      </button>
                      <button type="button" className="text-xs underline" onClick={() => setEditId(null)}>
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <span className="font-medium">{r.name}</span>
                  )}
                </td>
                <td className="px-3 py-2">{r.products}</td>
                <td className="px-3 py-2">
                  {r.category_ids.length === 0 ? (
                    <Badge tone="amber">every category</Badge>
                  ) : (
                    <div className="flex flex-wrap gap-1">
                      {r.category_ids.map((id) => (
                        <Badge key={id}>{catPath(id) || `#${id}`}</Badge>
                      ))}
                    </div>
                  )}
                  <select className={`${inputCls} mt-1 !w-auto text-xs`} value="" disabled={busy} onChange={(e) => void linkTo(r, e.target.value)}>
                    <option value="">List under another category…</option>
                    {sortedCats
                      .filter((c) => !r.category_ids.includes(c.id))
                      .map((c) => (
                        <option key={c.id} value={c.id}>
                          {c.label}
                        </option>
                      ))}
                  </select>
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-right">
                  {editId !== r.id && (
                    <button
                      type="button"
                      className={btn2Cls}
                      disabled={busy}
                      onClick={() => {
                        setEditId(r.id);
                        setEditName(r.name);
                        setMsg(null);
                      }}
                    >
                      Rename
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {loaded && shown.length === 0 && (
              <tr>
                <td colSpan={4} className="px-3 py-6 text-center text-gray-500">
                  No part types match.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
