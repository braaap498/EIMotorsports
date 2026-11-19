"use client";

import { useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Notice, btnCls, btn2Cls, inputCls } from "../../../components/admin/ui";

type Rec = { product_id: string; name: string; before: string; after: string };
type ScanResp = { store: string; total_pages: number | null; scanned: number; next_page: number | null; ready: Rec[]; no_match: Rec[] };
type ApplyResult = { product_id: string; before: string; after: string; status: string; detail: string };
type StoreChoice = { store: string };

const BATCH = 20;

export default function EbayInventoryLabelsPage() {
  const [stores, setStores] = useState<StoreChoice[]>([]);
  const [store, setStore] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const [progress, setProgress] = useState("");
  const [scanned, setScanned] = useState<number | null>(null);
  const [ready, setReady] = useState<Rec[]>([]);
  const [noMatch, setNoMatch] = useState<Rec[]>([]);
  const [done, setDone] = useState<ApplyResult[]>([]);
  const stop = useRef(false);

  useEffect(() => {
    api<{ stores: StoreChoice[] }>("/ebay-draft/stores")
      .then((r) => {
        setStores(r.stores);
        if (r.stores.length) setStore(r.stores[0].store);
      })
      .catch(() => {});
  }, []);

  async function scan() {
    setBusy(true);
    stop.current = false;
    setMsg(null);
    setDone([]);
    setReady([]);
    setNoMatch([]);
    setScanned(0);
    let page: number | null = 1;
    let count = 0;
    const r1: Rec[] = [];
    const r2: Rec[] = [];
    try {
      while (page !== null && !stop.current) {
        setProgress(`Reading eBay inventory, page ${page}…`);
        const r: ScanResp = await api<ScanResp>("/ebay-edit/sm-scan", { method: "POST", body: { store, page, pages: 6 } });
        count += r.scanned;
        r1.push(...r.ready);
        r2.push(...r.no_match);
        setScanned(count);
        setReady([...r1]);
        setNoMatch([...r2]);
        page = r.next_page;
      }
      setMsg({ kind: "success", text: stop.current ? "Stopped. What was found so far is listed." : `Done. Looked at ${count.toLocaleString()} inventory records.` });
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setProgress("");
      setBusy(false);
    }
  }

  async function change(limit: number | null) {
    const items = limit === null ? ready : ready.slice(0, limit);
    if (items.length === 0) return;
    if (!confirm(`Change the custom label on ${items.length} eBay inventory record${items.length === 1 ? "" : "s"} in ${store}?`)) return;
    setBusy(true);
    stop.current = false;
    setMsg(null);
    try {
      const all: ApplyResult[] = [];
      for (let i = 0; i < items.length && !stop.current; i += BATCH) {
        setProgress(`Changing ${Math.min(i + BATCH, items.length)} of ${items.length}…`);
        const chunk = items.slice(i, i + BATCH).map((x) => ({ product_id: x.product_id, before: x.before, after: x.after }));
        const r = await api<{ results: ApplyResult[] }>("/ebay-edit/sm-apply", { method: "POST", body: { store, items: chunk } });
        all.push(...r.results);
        setDone([...all]);
        if (r.results.some((x) => x.status === "error")) {
          setMsg({ kind: "error", text: "eBay refused one, so the rest was not sent. See the list below." });
          break;
        }
      }
      const okIds = new Set(all.filter((x) => x.status === "updated").map((x) => x.product_id));
      setReady((cur) => cur.filter((x) => !okIds.has(x.product_id)));
      if (all.length && all.every((x) => x.status === "updated")) setMsg({ kind: "success", text: `Changed ${all.length}.` });
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setProgress("");
      setBusy(false);
    }
  }

  const failed = done.filter((d) => d.status !== "updated");

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold">eBay inventory labels</h1>
      <p className="max-w-3xl text-sm text-gray-600">
        Seller Hub &gt; Listings &gt; Inventory keeps its own custom label for each product, and it did not change when the label changed from commas to xx.
        This reads that inventory from eBay, finds the records that still have a comma, and gives each the same label with xx, but only when one of your
        products has that label. Reading changes nothing. Nothing is changed until you press a Change button.
      </p>
      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}
      <div className="flex flex-wrap items-center gap-3 rounded border border-gray-200 bg-white p-4 text-sm">
        <label className="flex items-center gap-2">
          Store
          <select className={inputCls} style={{ width: "auto" }} value={store} onChange={(e) => setStore(e.target.value)} disabled={busy}>
            {stores.map((s) => <option key={s.store} value={s.store}>{s.store}</option>)}
          </select>
        </label>
        <button type="button" className={btnCls} disabled={busy || !store} onClick={() => void scan()}>Find records with old labels</button>
        {busy && <button type="button" className={btn2Cls} onClick={() => { stop.current = true; }}>Stop</button>}
        {progress && <span className="text-gray-600">{progress}</span>}
      </div>

      {scanned !== null && (
        <div className="space-y-3 rounded border border-gray-200 bg-white p-4 text-sm">
          <p>
            Looked at <b>{scanned.toLocaleString()}</b> records. <b>{ready.length}</b> can be changed
            {noMatch.length > 0 ? `; ${noMatch.length} have a comma but no product has the matching xx label, so they are left alone` : ""}.
          </p>
          {ready.length > 0 && (
            <div className="flex flex-wrap items-center gap-2">
              <button type="button" className={btnCls} disabled={busy} onClick={() => void change(1)}>Change one (test)</button>
              <button type="button" className={btnCls} disabled={busy} onClick={() => void change(null)}>Change all {ready.length}</button>
            </div>
          )}
          {ready.length > 0 && (
            <ul className="max-h-80 divide-y divide-gray-100 overflow-y-auto text-xs">
              {ready.slice(0, 200).map((r) => (
                <li key={r.product_id} className="py-1">
                  <span className="break-all font-mono">{r.before}</span> → <span className="break-all font-mono">{r.after}</span>
                  <span className="ml-2 text-gray-500">{r.name}</span>
                </li>
              ))}
              {ready.length > 200 && <li className="py-1 text-gray-500">…and {ready.length - 200} more.</li>}
            </ul>
          )}
          {noMatch.length > 0 && (
            <details>
              <summary className="cursor-pointer text-xs text-gray-600">Left alone ({noMatch.length})</summary>
              <ul className="mt-1 max-h-48 overflow-y-auto text-xs text-gray-600">
                {noMatch.slice(0, 200).map((r) => <li key={r.product_id}><span className="font-mono">{r.before}</span> <span className="text-gray-500">{r.name}</span></li>)}
              </ul>
            </details>
          )}
        </div>
      )}

      {done.length > 0 && (
        <div className="space-y-1 rounded border border-gray-200 bg-white p-4 text-xs">
          <p className="text-sm">Changed {done.filter((d) => d.status === "updated").length} of {done.length} tried.</p>
          {failed.map((d) => (
            <p key={d.product_id} className="text-red-700"><span className="font-mono">{d.before}</span> – {d.status}: {d.detail}</p>
          ))}
        </div>
      )}
    </div>
  );
}
