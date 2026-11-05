"use client";

import { useEffect, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Badge, Notice, btnCls, btn2Cls, inputCls } from "../../../components/admin/ui";

/* eBay stores: add a store, connect (authorize) it, test the connection, and switch live pushes on or off.
   Moved here from the Live listings page; works the same. */

type Store = {
  store: string;
  display_name: string | null;
  seller_username: string | null;
  is_active: boolean;
  authorized: boolean;
  authorized_at: string | null;
  authorized_by: string | null;
  refresh_token_expires_at: string | null;
  live_pushes_enabled: boolean;
  linked_count: number;
};
type StoresResp = { main_store: string; stores: Store[] };


export default function EbayStoresPage() {
  const [stores, setStores] = useState<Store[]>([]);
  const [error, setError] = useState("");
  const [showAdd, setShowAdd] = useState(false);
  const [newStore, setNewStore] = useState("");
  const [newName, setNewName] = useState("");
  const [newSeller, setNewSeller] = useState("");
  const [addMsg, setAddMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState("");
  const [diag, setDiag] = useState<{
    store: string;
    scopes_granted: string[];
    scopes_missing: string[];
    checks: { name: string; ok: boolean; status: number | null; note: string }[];
  } | null>(null);
  const [connMsg, setConnMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);

  async function loadStores() {
    try {
      const r = await api<StoresResp>("/ebay-listings/stores");
      setStores(r.stores);
    } catch (e) {
      setError(errMsg(e));
    }
  }

  useEffect(() => {
    void loadStores();
  }, []);

  async function connect(s: string) {
    setBusy(s);
    setConnMsg(null);
    try {
      const r = await api<{ url: string }>("/ebay-connect/start", { method: "POST", body: { store: s } });
      window.location.href = r.url; // eBay's approval page; it sends the person back to /admin/ebay-connect
    } catch (e) {
      setConnMsg({ kind: "error", text: errMsg(e) });
      setBusy("");
    }
  }

  async function disconnect(s: string) {
    if (!window.confirm(`Disconnect ${s}? Its saved eBay key is deleted and it must be approved again to edit.`)) return;
    setBusy(s);
    setConnMsg(null);
    try {
      await api("/ebay-connect/disconnect", { method: "POST", body: { store: s } });
      setConnMsg({ kind: "success", text: `${s} was disconnected.` });
      await loadStores();
    } catch (e) {
      setConnMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  async function testConn(s: string) {
    setBusy(s);
    setConnMsg(null);
    try {
      const r = await api<{
        ok: boolean;
        ebay_username: string;
        expected_username: string;
        selling_limit: { quantity?: number; amount?: { value?: string } } | null;
        selling_limit_error: string | null;
      }>("/ebay-connect/test", { method: "POST", body: { store: s } });
      const fmt = (v: unknown) => {
        const n = Number(v);
        return v === undefined || v === null || v === "" || Number.isNaN(n)
          ? "?"
          : n.toLocaleString("en-US", { maximumFractionDigits: 2 });
      };
      const lim = r.selling_limit
        ? ` Selling limit: ${fmt(r.selling_limit.quantity)} items / $${fmt(r.selling_limit.amount?.value)}.`
        : " eBay reported no selling limit.";
      setConnMsg({
        kind: r.ok ? "success" : "error",
        text: r.ok
          ? `${s}: connection works, signed in as ${r.ebay_username}.${lim}${r.selling_limit_error ? ` (Limit check failed: ${r.selling_limit_error})` : ""}`
          : `${s}: the key belongs to ${r.ebay_username}, but ${r.expected_username} was expected.`,
      });
    } catch (e) {
      setConnMsg({ kind: "error", text: `${s}: ${errMsg(e)}` });
    } finally {
      setBusy("");
    }
  }

  async function setPushes(s: string, enabled: boolean) {
    const q = enabled
      ? `Turn live pushes ON for ${s}? After this, "Send to eBay" on the edit page changes real listings on that store.`
      : `Turn live pushes OFF for ${s}? Previews still work; nothing can be sent.`;
    if (!window.confirm(q)) return;
    setBusy(s);
    setConnMsg(null);
    try {
      await api("/ebay-edit/live-pushes", { method: "PUT", body: { store: s, enabled } });
      await loadStores();
    } catch (e) {
      setConnMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  async function checkAccess(s: string) {
    setBusy(s);
    setConnMsg(null);
    setDiag(null);
    try {
      setDiag(await api("/ebay-connect/check", { method: "POST", body: { store: s } }));
    } catch (e) {
      setConnMsg({ kind: "error", text: `${s}: ${errMsg(e)}` });
    } finally {
      setBusy("");
    }
  }

  async function addStore() {
    setAdding(true);
    setAddMsg(null);
    try {
      await api("/ebay-listings/stores", {
        method: "POST",
        body: { store: newStore.trim(), display_name: newName.trim() || null, seller_username: newSeller.trim() || null },
      });
      setAddMsg({ kind: "success", text: `${newStore.trim()} was added.` });
      setNewStore("");
      setNewName("");
      setNewSeller("");
      await loadStores();
    } catch (e) {
      setAddMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setAdding(false);
    }
  }

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-semibold">Stores</h1>
        <p className="mt-1 max-w-3xl text-sm text-gray-600">
          The eBay stores the site works with. Connect a store so the app can read and edit its listings, and turn live pushes ON
          only when you are ready for changes to go to eBay. The Listings tab shows these stores&rsquo; listings.
        </p>
      </div>

      {error && <Notice kind="error">{error}</Notice>}

      <div className="rounded border border-gray-200 bg-white p-4">
          <div className="space-y-3">
            <div>
              <button type="button" className={btn2Cls} onClick={() => setShowAdd((v) => !v)}>
                {showAdd ? "Close" : "Add store"}
              </button>
            </div>
            {connMsg && <Notice kind={connMsg.kind}>{connMsg.text}</Notice>}
            {diag && (
              <div className="space-y-1 rounded border border-gray-200 bg-gray-50 p-3 text-sm">
                <div className="font-medium">Access check for {diag.store} (read only)</div>
                {diag.checks.map((c) => (
                  <div key={c.name} className={c.ok ? "text-green-700" : "text-red-700"}>
                    {c.ok ? "✓" : "✗"} {c.name}: {c.note}
                    {c.status && !c.ok ? ` (HTTP ${c.status})` : ""}
                  </div>
                ))}
                <div className="text-gray-600">
                  {diag.scopes_missing.length === 0
                    ? "All requested permissions were granted."
                    : `Not granted: ${diag.scopes_missing.map((x) => x.split("/").pop()).join(", ")}`}
                </div>
              </div>
            )}
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs text-gray-500">
                  <tr>
                    <th className="py-1 pr-6 font-normal">Store</th>
                    <th className="py-1 pr-6 font-normal">eBay name</th>
                    <th className="py-1 pr-6 font-normal">Linked</th>
                    <th className="py-1 pr-6 font-normal">Access</th>
                    <th className="py-1 pr-6 font-normal">Pushes</th>
                    <th className="py-1 pr-6 font-normal">Approval ends</th>
                    <th className="py-1 font-normal">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {stores.map((s) => (
                    <tr key={s.store} className="align-middle">
                      <td className="py-2 pr-6">
                        <span className="font-medium">{s.display_name || s.store}</span>
                      </td>
                      <td className="py-2 pr-6 text-gray-500">{s.store}</td>
                      <td className="py-2 pr-6 text-gray-500">{s.linked_count} linked to products</td>
                      <td className="py-2 pr-6">
                        <Badge tone={s.authorized ? "green" : "gray"}>{s.authorized ? "Authorized" : "Not authorized yet"}</Badge>
                      </td>
                      <td className="py-2 pr-6">
                        <Badge tone={s.live_pushes_enabled ? "amber" : "gray"}>{s.live_pushes_enabled ? "Live pushes ON" : "Preview only"}</Badge>
                      </td>
                      <td className="py-2 pr-6 text-xs text-gray-500">
                        {s.authorized && s.refresh_token_expires_at ? new Date(s.refresh_token_expires_at).toLocaleDateString() : "—"}
                      </td>
                      <td className="py-2">
                        <span className="flex flex-wrap gap-2">
                          <button type="button" className={btn2Cls} disabled={busy === s.store} onClick={() => void connect(s.store)}>
                            {s.authorized ? "Reconnect" : "Connect"}
                          </button>
                          {s.authorized && (
                            <>
                              <button type="button" className={btn2Cls} disabled={busy === s.store} onClick={() => void testConn(s.store)}>
                                Test
                              </button>
                              <button type="button" className={btn2Cls} disabled={busy === s.store} onClick={() => void setPushes(s.store, !s.live_pushes_enabled)}>
                                {s.live_pushes_enabled ? "Turn pushes OFF" : "Turn pushes ON"}
                              </button>
                              <button type="button" className={btn2Cls} disabled={busy === s.store} onClick={() => void checkAccess(s.store)}>
                                Check access
                              </button>
                              <button type="button" className={btn2Cls} disabled={busy === s.store} onClick={() => void disconnect(s.store)}>
                                Disconnect
                              </button>
                            </>
                          )}
                        </span>
                      </td>
                    </tr>
                  ))}
                  {stores.length === 0 && (
                    <tr>
                      <td colSpan={7} className="py-2 text-gray-500">No stores yet.</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

        {showAdd && (

          <div className="mt-3 space-y-3 border-t border-gray-100 pt-3">
            <div className="grid gap-3 sm:grid-cols-3">
              <label className="block text-sm font-medium text-gray-700">
                <span className="mb-1 block">Store name</span>
                <input className={inputCls} value={newStore} onChange={(e) => setNewStore(e.target.value)} placeholder="genuine-oem-parts" />
              </label>
              <label className="block text-sm font-medium text-gray-700">
                <span className="mb-1 block">Display name (optional)</span>
                <input className={inputCls} value={newName} onChange={(e) => setNewName(e.target.value)} />
              </label>
              <label className="block text-sm font-medium text-gray-700">
                <span className="mb-1 block">eBay user name (only if different)</span>
                <input className={inputCls} value={newSeller} onChange={(e) => setNewSeller(e.target.value)} />
              </label>
            </div>
            <div className="flex items-center gap-3">
              <button type="button" className={btnCls} disabled={adding || !newStore.trim()} onClick={() => void addStore()}>
                {adding ? "Adding…" : "Add store"}
              </button>
              {addMsg && <span className={`text-sm ${addMsg.kind === "error" ? "text-red-700" : "text-green-700"}`}>{addMsg.text}</span>}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
