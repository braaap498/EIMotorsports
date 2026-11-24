"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Badge, Field, Notice, btnCls, btn2Cls, inputCls } from "./ui";
import type { AmazonAccount } from "./amazonShared";

/* Amazon > Store API: add an account, save its refresh token and seller ID, test the connection, and the switches
   (Live pushes, order polling, quantity pushes, drop ship policy) plus the margin numbers the Buy Box screen uses. */
export default function AmazonAccounts() {
  const [rows, setRows] = useState<AmazonAccount[]>([]);
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const [newName, setNewName] = useState("");
  const [token, setToken] = useState<Record<string, string>>({});
  const [seller, setSeller] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState("");

  const load = useCallback(async () => {
    try {
      setRows((await api<{ accounts: AmazonAccount[] }>("/amazon-connect/accounts")).accounts);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  async function run(key: string, fn: () => Promise<unknown>, ok: string) {
    setBusy(key);
    setMsg(null);
    try {
      await fn();
      setMsg({ kind: "success", text: ok });
      await load();
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  const put = (account: string, patch: Record<string, unknown>) =>
    api("/amazon-connect/settings", { method: "PUT", body: { account, ...patch } });
  const enc = encodeURIComponent;

  return (
    <div className="space-y-4">
      <p className="text-xs text-gray-500">
        Add the account, paste its refresh token and seller ID, press Test. Keep <strong>Live pushes</strong> OFF until the listings are linked to
        products. Acknowledge Amazon&rsquo;s drop ship policy before ticking any listing as drop ship.
      </p>
      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}
      <div className="flex flex-wrap items-end gap-2">
        <Field label="New account name" className="w-64">
          <input className={inputCls} value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="e.g. EI-Motorsports" />
        </Field>
        <button
          className={btnCls}
          disabled={newName.trim().length < 2 || busy === "add"}
          onClick={() =>
            void run("add", async () => { await api("/amazon-connect/accounts", { method: "POST", body: { account: newName.trim() } }); setNewName(""); }, "Account added.")
          }
        >
          Add account
        </button>
      </div>

      {rows.map((a) => (
        <div key={a.account} className="space-y-3 rounded border border-gray-200 bg-white p-4">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-lg font-semibold">{a.display_name || a.account}</h2>
            <Badge tone={a.authorized ? "green" : "amber"}>{a.authorized ? "Key saved" : "No key yet"}</Badge>
            <Badge tone={a.live_pushes_enabled ? "green" : "gray"}>{a.live_pushes_enabled ? "Live pushes ON" : "Live pushes OFF"}</Badge>
            <span className="text-xs text-gray-500">Marketplaces: {a.marketplaces || "US"}</span>
          </div>

          <div className="flex flex-wrap items-end gap-2">
            <Field label="Refresh token" className="w-80">
              <input
                className={inputCls}
                type="password"
                value={token[a.account] ?? ""}
                onChange={(e) => setToken({ ...token, [a.account]: e.target.value })}
                placeholder={a.authorized ? "(saved; paste to replace)" : "Atzr|…"}
              />
            </Field>
            <Field label="Seller ID" className="w-48">
              <input className={inputCls} value={seller[a.account] ?? a.seller_id ?? ""} onChange={(e) => setSeller({ ...seller, [a.account]: e.target.value })} />
            </Field>
            <button
              className={btn2Cls}
              disabled={(token[a.account] ?? "").trim().length < 20 || busy === "tok" + a.account}
              onClick={() =>
                void run(
                  "tok" + a.account,
                  async () => {
                    await api(`/amazon-connect/accounts/${enc(a.account)}/token`, {
                      method: "POST",
                      body: { refresh_token: token[a.account].trim(), seller_id: (seller[a.account] ?? a.seller_id ?? "").trim() || null },
                    });
                    setToken({ ...token, [a.account]: "" });
                  },
                  "Key saved."
                )
              }
            >
              Save key
            </button>
            <button
              className={btn2Cls}
              disabled={!a.authorized || busy === "test" + a.account}
              onClick={() => void run("test" + a.account, () => api(`/amazon-connect/accounts/${enc(a.account)}/test`, { method: "POST" }), "Connection test passed.")}
            >
              {busy === "test" + a.account ? "Testing…" : "Test"}
            </button>
          </div>

          <div className="grid gap-2 text-sm sm:grid-cols-2">
            {(
              [
                ["live_pushes_enabled", "Live pushes (anything we send to Amazon)"],
                ["sales_polling_enabled", "Check for new orders"],
                ["sales_qty_push_enabled", "Send quantity changes"],
                ["stand_down_enabled", "Stand down when Amazon sells below our floor"],
                ["dropship_policy_ack", "I am the seller of record: drop ship policy acknowledged"],
              ] as const
            ).map(([k, label]) => (
              <label key={k} className="flex items-center gap-2">
                <input type="checkbox" checked={!!a[k]} disabled={busy === k + a.account} onChange={(e) => void run(k + a.account, () => put(a.account, { [k]: e.target.checked }), "Saved.")} />
                {label}
              </label>
            ))}
          </div>

          <div className="flex flex-wrap items-end gap-2">
            {(
              [
                ["sales_poll_minutes", "Order check every (min)"],
                ["referral_fee_pct", "Referral fee %"],
                ["target_margin_pct", "Target margin %"],
                ["ship_cost_est", "Shipping cost estimate $"],
              ] as const
            ).map(([k, label]) => (
              <Field key={k} label={label} className="w-40">
                <input
                  className={inputCls}
                  type="number"
                  defaultValue={a[k] ?? ""}
                  onBlur={(e) => {
                    const v = e.target.value.trim();
                    if (v !== "" && Number(v) !== a[k]) void run(k + a.account, () => put(a.account, { [k]: Number(v) }), "Saved.");
                  }}
                />
              </Field>
            ))}
            <Field label="Marketplaces (US,CA,MX,BR)" className="w-44">
              <input
                className={inputCls}
                defaultValue={a.marketplaces ?? "US"}
                onBlur={(e) => {
                  const v = e.target.value.trim().toUpperCase();
                  if (v && v !== a.marketplaces) void run("mp" + a.account, () => put(a.account, { marketplaces: v }), "Saved.");
                }}
              />
            </Field>
          </div>
        </div>
      ))}
      {rows.length === 0 && <p className="text-sm text-gray-500">No Amazon accounts yet.</p>}
    </div>
  );
}
