"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { api, errMsg } from "../../../lib/adminApi";
import { Badge, Field, Notice, btnCls, btn2Cls, dangerCls, inputCls } from "../../../components/admin/ui";

type Cat = { id: number; parent_id: number | null; name: string; slug: string | null; sort_order: number; products: number; children: number };

export default function CategoriesPage() {
  const [cats, setCats] = useState<Cat[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);

  // add
  const [newName, setNewName] = useState("");
  const [newParent, setNewParent] = useState("");

  // edit
  const [editId, setEditId] = useState<number | null>(null);
  const [editName, setEditName] = useState("");
  const [editParent, setEditParent] = useState("");
  const [editOrder, setEditOrder] = useState("0");

  const load = useCallback(async () => {
    try {
      const r = await api<{ items: Cat[] }>("/category-manage");
      setCats(r.items);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const byId = useMemo(() => new Map(cats.map((c) => [c.id, c])), [cats]);

  const path = useCallback(
    (id: number): string => {
      const out: string[] = [];
      let c = byId.get(id);
      let guard = 0;
      while (c && guard++ < 10) {
        out.unshift(c.name);
        c = c.parent_id ? byId.get(c.parent_id) : undefined;
      }
      return out.join(" > ");
    },
    [byId],
  );

  // Tree order: each category right under its parent, siblings by sort order.
  const ordered = useMemo(() => {
    const kids = new Map<number | null, Cat[]>();
    for (const c of cats) {
      const k = c.parent_id && byId.has(c.parent_id) ? c.parent_id : null;
      kids.set(k, [...(kids.get(k) || []), c]);
    }
    const out: { cat: Cat; depth: number }[] = [];
    const walk = (parent: number | null, depth: number) => {
      for (const c of (kids.get(parent) || []).sort((a, b) => a.sort_order - b.sort_order || a.name.localeCompare(b.name))) {
        out.push({ cat: c, depth });
        walk(c.id, depth + 1);
      }
    };
    walk(null, 0);
    return out;
  }, [cats, byId]);

  const shown = ordered.filter(({ cat }) => !q.trim() || path(cat.id).toLowerCase().includes(q.trim().toLowerCase()));

  // Parents offered when moving: not the category itself or anything under it.
  function parentChoices(id: number) {
    const bad = new Set<number>([id]);
    let grew = true;
    while (grew) {
      grew = false;
      for (const c of cats) {
        if (c.parent_id && bad.has(c.parent_id) && !bad.has(c.id)) {
          bad.add(c.id);
          grew = true;
        }
      }
    }
    return cats
      .filter((c) => !bad.has(c.id))
      .map((c) => ({ id: c.id, label: path(c.id) }))
      .sort((a, b) => a.label.localeCompare(b.label));
  }

  const allChoices = useMemo(() => cats.map((c) => ({ id: c.id, label: path(c.id) })).sort((a, b) => a.label.localeCompare(b.label)), [cats, path]);

  async function add() {
    const name = newName.trim();
    if (!name) return;
    setBusy(true);
    setMsg(null);
    try {
      await api("/category-manage", { method: "POST", body: { name, parent_id: newParent ? Number(newParent) : null } });
      setMsg({ kind: "success", text: `"${name}" added.` });
      setNewName("");
      await load();
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy(false);
    }
  }

  function startEdit(c: Cat) {
    setEditId(c.id);
    setEditName(c.name);
    setEditParent(c.parent_id ? String(c.parent_id) : "");
    setEditOrder(String(c.sort_order));
    setMsg(null);
  }

  async function save(c: Cat) {
    const name = editName.trim();
    if (!name) return;
    const parent = editParent ? Number(editParent) : null;
    const moved = parent !== c.parent_id;
    if (moved && c.products > 0 && !confirm(`Move "${c.name}" ${parent ? `under "${path(parent)}"` : "to the top level"}?\n\nIts ${c.products} product${c.products === 1 ? "" : "s"} move with it, and it will show in a new place on the storefront menu.`)) return;
    setBusy(true);
    setMsg(null);
    try {
      await api(`/category-manage/${c.id}`, {
        method: "PATCH",
        body: { name, move: moved, parent_id: moved ? parent : null, sort_order: Number.isFinite(Number(editOrder)) ? Math.trunc(Number(editOrder)) : c.sort_order },
      });
      setMsg({ kind: "success", text: `Saved "${name}".` });
      setEditId(null);
      await load();
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy(false);
    }
  }

  async function remove(c: Cat) {
    if (!confirm(`Delete the category "${path(c.id)}"? This cannot be undone.`)) return;
    setBusy(true);
    setMsg(null);
    try {
      await api(`/category-manage/${c.id}`, { method: "DELETE" });
      setMsg({ kind: "success", text: `Deleted "${c.name}".` });
      setEditId(null);
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
        <h1 className="text-2xl font-semibold">Categories</h1>
        <div className="flex gap-4 text-sm">
          <Link href="/admin/part-types" className="underline">
            Edit part types
          </Link>
          <Link href="/admin/products" className="underline">
            Back to products
          </Link>
        </div>
      </div>
      <p className="text-sm text-gray-600">
        The category tree that products are filed under and the storefront menu is built from. Renaming or moving a category keeps its products and its
        web address. A category can only be deleted when it is empty.
      </p>
      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}

      <form
        className="space-y-3 rounded-lg border border-gray-200 bg-white p-4"
        onSubmit={(e) => {
          e.preventDefault();
          void add();
        }}
      >
        <h2 className="font-semibold">Add a category</h2>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Name">
            <input className={inputCls} value={newName} maxLength={80} onChange={(e) => setNewName(e.target.value)} />
          </Field>
          <Field label="Under">
            <select className={inputCls} value={newParent} onChange={(e) => setNewParent(e.target.value)}>
              <option value="">Top level</option>
              {allChoices.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.label}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <button className={btnCls} disabled={busy || !newName.trim()}>
          Add category
        </button>
      </form>

      <div className="flex flex-wrap items-end gap-3">
        <Field label="Search">
          <input className={inputCls} value={q} onChange={(e) => setQ(e.target.value)} placeholder="Category name" />
        </Field>
        <span className="pb-2 text-sm text-gray-500">{loaded ? `${shown.length} of ${cats.length}` : "Loading…"}</span>
      </div>

      <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white">
        <table className="w-full text-left text-sm">
          <thead className="bg-gray-50 text-xs uppercase text-gray-500">
            <tr>
              <th className="px-3 py-2">Category</th>
              <th className="px-3 py-2">Products</th>
              <th className="px-3 py-2">Sub-categories</th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {shown.map(({ cat: c, depth }) => (
              <tr key={c.id} className="align-top">
                <td className="px-3 py-2" style={{ paddingLeft: `${0.75 + (q.trim() ? 0 : depth) * 1.25}rem` }}>
                  {editId === c.id ? (
                    <div className="space-y-2">
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
                              void save(c);
                            } else if (e.key === "Escape") setEditId(null);
                          }}
                        />
                      </div>
                      <div className="flex flex-wrap items-center gap-2 text-xs">
                        <label className="flex items-center gap-1">
                          Under
                          <select className={`${inputCls} !w-auto text-xs`} value={editParent} onChange={(e) => setEditParent(e.target.value)}>
                            <option value="">Top level</option>
                            {parentChoices(c.id).map((p) => (
                              <option key={p.id} value={p.id}>
                                {p.label}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label className="flex items-center gap-1">
                          Order
                          <input className={`${inputCls} !w-20 text-xs`} type="number" value={editOrder} onChange={(e) => setEditOrder(e.target.value)} />
                        </label>
                      </div>
                      <div className="flex flex-wrap items-center gap-2">
                        <button type="button" className={`${btnCls} !px-3 !py-1`} disabled={busy || !editName.trim()} onClick={() => void save(c)}>
                          Save
                        </button>
                        <button type="button" className="text-xs underline" onClick={() => setEditId(null)}>
                          Cancel
                        </button>
                        {c.products === 0 && c.children === 0 && (
                          <button type="button" className={`${dangerCls} !px-3 !py-1 text-xs`} disabled={busy} onClick={() => void remove(c)}>
                            Delete
                          </button>
                        )}
                      </div>
                    </div>
                  ) : (
                    <span className={depth === 0 ? "font-semibold" : "font-medium"}>{q.trim() ? path(c.id) : c.name}</span>
                  )}
                </td>
                <td className="px-3 py-2">{c.products}</td>
                <td className="px-3 py-2">{c.children > 0 ? <Badge>{c.children}</Badge> : <span className="text-gray-400">none</span>}</td>
                <td className="whitespace-nowrap px-3 py-2 text-right">
                  {editId !== c.id && (
                    <button type="button" className={btn2Cls} disabled={busy} onClick={() => startEdit(c)}>
                      Edit
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {loaded && shown.length === 0 && (
              <tr>
                <td colSpan={4} className="px-3 py-6 text-center text-gray-500">
                  No categories match.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
