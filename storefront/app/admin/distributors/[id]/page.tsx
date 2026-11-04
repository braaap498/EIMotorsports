"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
import { errMsg } from "../../../../lib/adminApi";
import { useAccess } from "../../../../components/admin/AdminAccess";
import { Notice, btn2Cls, dangerCls } from "../../../../components/admin/ui";
import { DistributorForm, deleteDistributor, isAutodist, AUTODIST_PAGE, useDistributors } from "../../../../components/admin/Distributors";

// One distributor's page: its details, edit and delete. (Automatic Distributors has its own page with the catalog.)
export default function DistributorPage() {
  const params = useParams<{ id: string }>();
  const id = Number(params.id);
  const { can, isAdmin } = useAccess();
  const router = useRouter();
  const { dists, loaded, flash, setFlash, reload } = useDistributors();
  const [editing, setEditing] = useState(false);

  if (!can("inventory.purchase")) {
    return <Notice kind="error">Your account doesn’t have access to distributors.</Notice>;
  }
  if (!Number.isInteger(id)) return <p className="text-sm text-red-600">Invalid distributor id.</p>;
  if (!loaded) return <p className="text-sm text-gray-500">Loading…</p>;

  const d = dists.find((x) => x.id === id);
  if (!d) {
    return (
      <div className="space-y-3">
        {flash && <Notice kind={flash.kind}>{flash.text}</Notice>}
        <Notice kind="error">That distributor no longer exists.</Notice>
        <Link href="/admin/distributors" className="underline">
          ← All distributors
        </Link>
      </div>
    );
  }

  async function remove() {
    if (!d) return;
    try {
      if (await deleteDistributor(d)) router.push("/admin/distributors");
    } catch (e) {
      setFlash({ kind: "error", text: errMsg(e) });
    }
  }

  return (
    <div className="max-w-3xl space-y-4">
      <Link href="/admin/distributors" className="text-sm text-gray-600 underline">
        ← All distributors
      </Link>
      <h1 className="text-2xl font-semibold">{d.name}</h1>
      {flash && <Notice kind={flash.kind}>{flash.text}</Notice>}

      {isAdmin && isAutodist(d.name) && (
        <Notice kind="info">
          This distributor has a catalog page.{" "}
          <Link href={AUTODIST_PAGE} className="underline">
            Open the Automatic Distributors catalog
          </Link>
        </Notice>
      )}

      {editing ? (
        <DistributorForm
          dist={d}
          onCancel={() => setEditing(false)}
          onSaved={() => {
            setEditing(false);
            reload();
            setFlash({ kind: "success", text: "Distributor saved." });
          }}
        />
      ) : (
        <div className="space-y-3 rounded-lg border border-gray-200 bg-white p-4 text-base">
          <dl className="grid gap-3 sm:grid-cols-2">
            <div>
              <dt className="text-sm font-medium text-gray-500">Email</dt>
              <dd>{d.email || "—"}</dd>
            </div>
            <div>
              <dt className="text-sm font-medium text-gray-500">Phone</dt>
              <dd>{d.phone || "—"}</dd>
            </div>
            <div>
              <dt className="text-sm font-medium text-gray-500">Address (prints in the Vendor box)</dt>
              <dd className="whitespace-pre-line">{d.address || "—"}</dd>
            </div>
            <div>
              <dt className="text-sm font-medium text-gray-500">Notes (not printed)</dt>
              <dd>{d.notes || "—"}</dd>
            </div>
          </dl>
          <div className="flex gap-2">
            <button className={btn2Cls} onClick={() => setEditing(true)}>
              Edit
            </button>
            <button className={dangerCls} onClick={remove}>
              Delete
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
