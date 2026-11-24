"use client";

import { useCallback, useEffect, useState } from "react";
import { api, apiDownload, errMsg } from "../../lib/adminApi";
import { Badge, Field, Notice, btnCls, btn2Cls, inputCls } from "./ui";
import { useAmazonAccounts } from "./amazonShared";

type Draft = {
  id: number; account: string; marketplace: string; product_id: number | null; mode: string; sku: string; asin: string | null; product_type: string | null;
  title: string | null; bullets: string | null; description: string | null; price: number | null; quantity: number; status: string;
  validated_ok: boolean; last_issues: string | null;
};
type Issue = { severity?: string; code?: string; message?: string; attributeNames?: string[] };

/* Amazon > New listing: make a draft from a website product (an offer on an existing ASIN, or a new catalog item), or sell similar from
   a listing (Listings screen). Validate is a read-only preview: read its issues, fix them, then Publish (needs Live pushes ON). */
export default function AmazonNew() {
  const { accounts, account, setAccount } = useAmazonAccounts();
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const [issues, setIssues] = useState<Record<number, Issue[]>>({});
  const [f, setF] = useState({ product_id: "", sku: "", mode: "offer", asin: "", product_type: "", marketplace: "US" });
  const [edit, setEdit] = useState<Record<number, Partial<Draft>>>({});

  const load = useCallback(async () => {
    try {
      const p = new URLSearchParams();
      if (account) p.set("account", account);
      setDrafts((await api<{ drafts: Draft[] }>(`/amazon-new/drafts?${p}`)).drafts);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    }
  }, [account]);
  useEffect(() => {
    void load();
  }, [load]);

  async function act(fn: () => Promise<unknown>, ok: string) {
    setMsg(null);
    try {
      await fn();
      setMsg({ kind: "success", text: ok });
      await load();
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    }
  }

  async function validate(d: Draft) {
    setMsg(null);
    try {
      const r = await api<{ ok: boolean; issues: Issue[] }>(`/amazon-new/drafts/${d.id}/validate`, { method: "POST" });
      setIssues((x) => ({ ...x, [d.id]: r.issues }));
      setMsg({ kind: r.ok ? "success" : "error", text: r.ok ? "Amazon's preview found no errors." : "Amazon's preview found problems. Read the issues under the draft." });
      await load();
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    }
  }

  async function fitment(pid: number) {
    try {
      const blob = await apiDownload(`/amazon-new/fitment/${pid}.csv`);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `amazon-fitment-${pid}.csv`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    }
  }

  const e = (d: Draft) => ({ ...d, ...(edit[d.id] ?? {}) });
  const setE = (id: number, patch: Partial<Draft>) => setEdit((x) => ({ ...x, [id]: { ...(x[id] ?? {}), ...patch } }));

  return (
    <div className="space-y-4">
      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}
      <div className="space-y-2 rounded border border-gray-200 bg-white p-4">
        <h2 className="font-semibold">New draft from a product</h2>
        <div className="flex flex-wrap items-end gap-2">
          <Field label="Account" className="w-48">
            <select className={inputCls} value={account} onChange={(x) => setAccount(x.target.value)}>
              <option value="">Pick…</option>
              {accounts.map((a) => <option key={a.account} value={a.account}>{a.display_name || a.account}</option>)}
            </select>
          </Field>
          <Field label="Marketplace" className="w-24"><input className={inputCls} value={f.marketplace} onChange={(x) => setF({ ...f, marketplace: x.target.value.toUpperCase() })} /></Field>
          <Field label="Product id" className="w-28"><input className={inputCls} value={f.product_id} onChange={(x) => setF({ ...f, product_id: x.target.value })} /></Field>
          <Field label="New SKU (optional)" className="w-48"><input className={inputCls} value={f.sku} onChange={(x) => setF({ ...f, sku: x.target.value })} /></Field>
          <Field label="Kind" className="w-56">
            <select className={inputCls} value={f.mode} onChange={(x) => setF({ ...f, mode: x.target.value })}>
              <option value="offer">Offer on an existing ASIN</option>
              <option value="new">New catalog item</option>
            </select>
          </Field>
          {f.mode === "offer" ? (
            <Field label="ASIN" className="w-40"><input className={inputCls} value={f.asin} onChange={(x) => setF({ ...f, asin: x.target.value.toUpperCase() })} /></Field>
          ) : (
            <Field label="Amazon product type" className="w-56"><input className={inputCls} value={f.product_type} onChange={(x) => setF({ ...f, product_type: x.target.value.toUpperCase() })} placeholder="e.g. AUTO_PART" /></Field>
          )}
          <button
            className={btnCls}
            disabled={!account || !/^\d+$/.test(f.product_id) || (f.mode === "offer" && f.asin.length < 10)}
            onClick={() =>
              void act(
                () => api("/amazon-new/drafts", { method: "POST", body: { account, marketplace: f.marketplace, product_id: Number(f.product_id), sku: f.sku || null, mode: f.mode, asin: f.asin || null, product_type: f.product_type || null } }),
                "Draft made."
              )
            }
          >
            Make draft
          </button>
        </div>
        <p className="text-xs text-gray-500">To sell similar, use the Listings screen&rsquo;s listing and copy it under a new SKU (the draft appears here).</p>
      </div>

      {drafts.map((d0) => {
        const d = e(d0);
        const iss = issues[d.id] ?? (d.last_issues ? (JSON.parse(d.last_issues) as Issue[]) : []);
        return (
          <div key={d.id} className="space-y-2 rounded border border-gray-200 bg-white p-4">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-semibold">{d.sku}</span>
              <Badge tone={d.status === "published" ? "green" : "amber"}>{d.status}</Badge>
              <Badge tone={d.validated_ok ? "green" : "gray"}>{d.validated_ok ? "Validated" : "Not validated"}</Badge>
              <span className="text-xs text-gray-500">{d.account} · {d.marketplace} · {d.mode === "offer" ? `offer on ${d.asin ?? "?"}` : `new item ${d.product_type ?? ""}`}{d.product_id ? ` · product #${d.product_id}` : " · no product linked"}</span>
            </div>
            <div className="grid gap-2 sm:grid-cols-2">
              <Field label="Title" className="sm:col-span-2"><input className={inputCls} value={d.title ?? ""} onChange={(x) => setE(d.id, { title: x.target.value })} /></Field>
              <Field label="Price"><input className={inputCls} type="number" value={d.price ?? ""} onChange={(x) => setE(d.id, { price: x.target.value === "" ? null : Number(x.target.value) })} /></Field>
              <Field label="Quantity"><input className={inputCls} type="number" value={d.quantity} onChange={(x) => setE(d.id, { quantity: Number(x.target.value) })} /></Field>
              {d.mode === "new" && (
                <>
                  <Field label="Bullets (one per line, up to 5)"><textarea className={inputCls} rows={4} value={d.bullets ?? ""} onChange={(x) => setE(d.id, { bullets: x.target.value })} /></Field>
                  <Field label="Description"><textarea className={inputCls} rows={4} value={d.description ?? ""} onChange={(x) => setE(d.id, { description: x.target.value })} /></Field>
                </>
              )}
            </div>
            {iss.length > 0 && (
              <ul className="space-y-1 rounded border border-amber-200 bg-amber-50 p-2 text-xs text-amber-900">
                {iss.map((i, n) => <li key={n}><strong>{i.severity ?? "ISSUE"}</strong> {i.code ?? ""} {i.message ?? ""} {i.attributeNames?.length ? `(${i.attributeNames.join(", ")})` : ""}</li>)}
              </ul>
            )}
            <div className="flex flex-wrap gap-2">
              <button className={btn2Cls} disabled={!edit[d.id]} onClick={() => void act(async () => { await api(`/amazon-new/drafts/${d.id}`, { method: "PUT", body: { title: d.title, price: d.price, quantity: d.quantity, bullets: d.bullets, description: d.description } }); setEdit((x) => { const y = { ...x }; delete y[d.id]; return y; }); }, "Saved. Validate again before publishing.")}>Save changes</button>
              <button className={btn2Cls} disabled={d.status === "published" || !!edit[d.id]} onClick={() => void validate(d0)}>Validate (preview only)</button>
              <button className={btnCls} disabled={!d.validated_ok || d.status === "published" || !!edit[d.id]} onClick={() => { if (window.confirm(`Publish ${d.sku} to Amazon? This sends it for real.`)) void act(() => api(`/amazon-new/drafts/${d.id}/publish`, { method: "POST" }), "Sent to Amazon."); }}>Publish</button>
              {d.product_id && <button className={btn2Cls} onClick={() => void fitment(d.product_id as number)}>Fitment CSV</button>}
            </div>
          </div>
        );
      })}
      {drafts.length === 0 && <p className="text-sm text-gray-500">No drafts yet.</p>}
    </div>
  );
}
