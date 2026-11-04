"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import ClickRow from "./ClickRow";
import { useAccess } from "./AdminAccess";
import { Badge, Notice, btnCls, btn2Cls, dangerCls, inputCls } from "./ui";

/* The distributors you order from. They live in the same table the purchase orders use
   (/purchase-orders/distributors), so a distributor added here shows up in the Create PO list. */

export type Dist = { id: number; name: string; address: string | null; email: string | null; phone: string | null; notes: string | null };

const inp = inputCls.replace("text-sm", "text-base");

// A distributor that has its own tools page (catalog feed, imports). Matched by name so no database change is needed;
// owners and admins only, because the Automatic Distributors page shows dealer cost.
export const AUTODIST_PAGE = "/admin/distributors/autodist";
export const isAutodist = (name: string) => /autodist|automatic\s+distributors/i.test(name);

/** Where clicking a distributor goes. */
export function distributorPage(d: Dist, isAdmin: boolean): string {
  return isAdmin && isAutodist(d.name) ? AUTODIST_PAGE : `/admin/distributors/${d.id}`;
}

type Flash = { kind: "success" | "error" | "info"; text: string } | null;

export function useDistributors() {
  const [dists, setDists] = useState<Dist[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [flash, setFlash] = useState<Flash>(null);
  const reload = useCallback(() => {
    api<{ items: Dist[] }>("/purchase-orders/distributors")
      .then((r) => setDists(r.items))
      .catch((e) => setFlash({ kind: "error", text: errMsg(e) }))
      .finally(() => setLoaded(true));
  }, []);
  useEffect(reload, [reload]);
  return { dists, loaded, flash, setFlash, reload };
}

type DForm = { name: string; address: string; email: string; phone: string; notes: string };
const blank: DForm = { name: "", address: "", email: "", phone: "", notes: "" };

/** Add (dist = null) or edit one distributor. */
export function DistributorForm({
  dist,
  presetName,
  onSaved,
  onCancel,
}: {
  dist: Dist | null;
  presetName?: string;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const [form, setForm] = useState<DForm>(
    dist
      ? { name: dist.name, address: dist.address ?? "", email: dist.email ?? "", phone: dist.phone ?? "", notes: dist.notes ?? "" }
      : { ...blank, name: presetName ?? "" }
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const set = (k: keyof DForm, v: string) => setForm((f) => ({ ...f, [k]: v }));

  async function save() {
    if (!form.name.trim()) return setError("A distributor needs a name.");
    setBusy(true);
    setError("");
    try {
      await api(dist ? `/purchase-orders/distributors/${dist.id}` : "/purchase-orders/distributors", {
        method: dist ? "PUT" : "POST",
        body: form,
      });
      onSaved();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3 rounded-lg border border-gray-200 bg-white p-4 text-base">
      <div className="font-semibold">{dist ? "Edit distributor" : "New distributor"}</div>
      {error && <Notice kind="error">{error}</Notice>}
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="block font-medium text-gray-700">
          <span className="mb-1 block">Name</span>
          <input className={inp} value={form.name} maxLength={100} onChange={(e) => set("name", e.target.value)} />
        </label>
        <label className="block font-medium text-gray-700">
          <span className="mb-1 block">Email</span>
          <input className={inp} value={form.email} onChange={(e) => set("email", e.target.value)} />
        </label>
        <label className="block font-medium text-gray-700 sm:row-span-2">
          <span className="mb-1 block">Address (prints in the Vendor box)</span>
          <textarea className={inp} rows={4} value={form.address} onChange={(e) => set("address", e.target.value)} />
        </label>
        <label className="block font-medium text-gray-700">
          <span className="mb-1 block">Phone</span>
          <input className={inp} value={form.phone} onChange={(e) => set("phone", e.target.value)} />
        </label>
        <label className="block font-medium text-gray-700">
          <span className="mb-1 block">Notes (not printed)</span>
          <input className={inp} value={form.notes} onChange={(e) => set("notes", e.target.value)} />
        </label>
      </div>
      <div className="flex gap-2">
        <button className={btnCls} disabled={busy} onClick={save}>
          {busy ? "Saving…" : "Save"}
        </button>
        <button className={btn2Cls} disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}

export async function deleteDistributor(d: Dist): Promise<boolean> {
  if (!window.confirm(`Delete ${d.name}? Purchase orders already made keep their own copy of the vendor.`)) return false;
  await api(`/purchase-orders/distributors/${d.id}`, { method: "DELETE" });
  return true;
}

/** The Distributors page: every distributor you order from. Click one to open its page. */
export default function DistributorList() {
  const { isAdmin } = useAccess();
  const { dists, loaded, flash, setFlash, reload } = useDistributors();
  const [editing, setEditing] = useState<Dist | "new" | null>(null);
  const [preset, setPreset] = useState("");

  async function remove(d: Dist) {
    try {
      if (await deleteDistributor(d)) {
        reload();
        setFlash({ kind: "success", text: `${d.name} deleted.` });
      }
    } catch (e) {
      setFlash({ kind: "error", text: errMsg(e) });
    }
  }

  function start(target: Dist | "new", name = "") {
    setPreset(name);
    setEditing(target);
    setFlash(null);
  }

  // Automatic Distributors always has its page for owners and admins, even before it is added as a distributor.
  const autodistListed = dists.some((d) => isAutodist(d.name));

  return (
    <div className="max-w-5xl space-y-4">
      <div>
        <h1 className="text-2xl font-semibold">Distributors</h1>
        <p className="mt-1 text-base text-gray-600">
          The distributors you order from. Click one to open its page. The address prints in the Vendor box on the PO, one line per line you type.
        </p>
      </div>

      {flash && <Notice kind={flash.kind}>{flash.text}</Notice>}

      <button className={btnCls} onClick={() => start("new")}>
        Add a distributor
      </button>

      {editing !== null && (
        <DistributorForm
          key={editing === "new" ? `new-${preset}` : editing.id}
          dist={editing === "new" ? null : editing}
          presetName={preset}
          onCancel={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            reload();
            setFlash({ kind: "success", text: "Distributor saved." });
          }}
        />
      )}

      <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white">
        <table className="w-full text-left text-base">
          <thead className="bg-gray-50 text-sm text-gray-600">
            <tr>
              <th className="px-4 py-2">Distributor</th>
              <th className="px-4 py-2">Contact</th>
              <th className="px-4 py-2" />
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {loaded && dists.length === 0 && !isAdmin && (
              <tr>
                <td colSpan={3} className="p-6 text-center text-gray-500">
                  No distributors yet.
                </td>
              </tr>
            )}
            {isAdmin && loaded && !autodistListed && (
              <ClickRow href={AUTODIST_PAGE}>
                <td className="px-4 py-3 align-top">
                  <div className="flex items-center gap-2">
                    <Link href={AUTODIST_PAGE} className="font-semibold underline">
                      Automatic Distributors
                    </Link>
                    <Badge tone="blue">Catalog feed</Badge>
                  </div>
                  <div className="text-sm text-gray-500">Not on your purchase-order list yet.</div>
                </td>
                <td className="px-4 py-3 align-top text-gray-500">—</td>
                <td className="px-4 py-3 text-right align-top">
                  <button className={btn2Cls} onClick={() => start("new", "Automatic Distributors")}>
                    Add to PO list
                  </button>
                </td>
              </ClickRow>
            )}
            {dists.map((d) => {
              const href = distributorPage(d, isAdmin);
              return (
                <ClickRow key={d.id} href={href}>
                  <td className="px-4 py-3 align-top">
                    <div className="flex flex-wrap items-center gap-2">
                      <Link href={href} className="font-semibold underline">
                        {d.name}
                      </Link>
                      {isAdmin && isAutodist(d.name) && <Badge tone="blue">Catalog feed</Badge>}
                    </div>
                    {d.notes && <div className="text-sm text-gray-500">{d.notes}</div>}
                  </td>
                  <td className="px-4 py-3 align-top text-gray-600">
                    <div className="whitespace-pre-line">{d.address}</div>
                    <div className="text-gray-500">{[d.email, d.phone].filter(Boolean).join(" · ")}</div>
                  </td>
                  <td className="px-4 py-3 text-right align-top">
                    <div className="flex justify-end gap-2">
                      <button className={btn2Cls} onClick={() => start(d)}>
                        Edit
                      </button>
                      <button className={dangerCls} onClick={() => remove(d)}>
                        Delete
                      </button>
                    </div>
                  </td>
                </ClickRow>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
