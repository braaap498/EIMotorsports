"use client";

import { useCallback, useEffect, useState } from "react";
import { api, apiDownload, errMsg } from "../../lib/adminApi";
import { Badge, Modal, Notice, Pager, btnCls, btn2Cls, dangerCls, inputCls } from "./ui";

type Part = {
  id: number;
  sku: string;
  title: string;
  part_number: string | null;
  cost: number | null;
  on_hand: number;
  reserved: number;
  available: number;
  reorder_point: number | null;
  on_order: boolean;
  open_pos: number[];
  suggested_qty: number;
};
type Dist = { id: number; name: string; address: string | null; email: string | null; phone: string | null; notes: string | null };
type PoLine = {
  id: number;
  product_id: number | null;
  sku: string | null;
  item: string | null;
  description: string;
  quantity: number;
  rate: number;
  amount: number;
  received_at: string | null;
  received_qty: number | null;
};
type PoDetail = {
  id: number;
  po_number: number;
  distributor_id: number | null;
  sent_at: string | null;
  created_by_name: string | null;
  status: "open" | "received" | "cancelled";
  cancelled_at: string | null;
  cancelled_by_name: string | null;
  vendor_name: string;
  vendor_address: string | null;
  ship_to: string | null;
  note: string | null;
  total: number;
  created_by: string | null;
  created_at: string;
  date: string;
  lines: PoLine[];
  missing_part_numbers?: string[];
  renumbered?: boolean;
};
type PoRow = {
  id: number;
  po_number: number;
  vendor_name: string;
  total_cents: number;
  created_by: string | null;
  created_by_name: string | null;
  sent_at: string | null;
  created_at: string;
  line_count: number;
  open_lines: number;
  status: "open" | "received" | "cancelled";
  issue_count: number;
};
type Issue = {
  id: number;
  kind: "short" | "over" | "not_on_po";
  sku: string | null;
  item: string | null;
  description: string | null;
  expected_qty: number;
  received_qty: number;
  difference: number;
  note: string | null;
  acknowledged_by: string | null;
  acknowledged_by_name: string | null;
  acknowledged_at: string;
};
type IssuePo = {
  id: number;
  po_number: number;
  vendor_name: string;
  status: "open" | "received" | "cancelled";
  created_at: string;
  issue_count: number;
  issues: Issue[];
};
type Sel = { part: Part; qty: string; rate: string };
type Tab = "reorder" | "history" | "issues" | "distributors";

const PARTS_LIMIT = 50;
const PO_LIMIT = 30;
const money = (n: number) => `$${n.toFixed(2)}`;
// One size bigger than the rest of the admin screens, so a purchase order is easy to read.
const inp = inputCls.replace("text-sm", "text-base");
const who = (name: string | null, email: string | null) => name ?? email ?? "—";
const KIND_LABEL: Record<Issue["kind"], string> = { short: "Short", over: "Over", not_on_po: "Over (not on PO)" };
const KIND_TONE: Record<Issue["kind"], "amber" | "blue" | "gray"> = { short: "amber", over: "blue", not_on_po: "blue" };

async function download(po: { id: number; po_number: number }, fmt: "pdf" | "xlsx") {
  const blob = await apiDownload(`/purchase-orders/${po.id}/export?format=${fmt}`);
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `PO-${po.po_number}.${fmt}`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

/**
 * Purchase orders.
 *  - Reorder: low and out-of-stock parts (or every part). Tick parts, press Create PO, pick a distributor.
 *  - History: every PO ever made (they are never deleted, only cancelled), searchable; view it or download it again as PDF or Excel.
 *  - Shortages / overages: every PO that had a receiving problem, searchable.
 *  - Distributors: add, edit and delete the distributors you order from.
 * Creating a PO marks its parts Backordered by default (un-tick on the Inventory > Stock tab).
 */
export default function PurchaseOrders() {
  const [tab, setTab] = useState<Tab>("reorder");
  const [dists, setDists] = useState<Dist[]>([]);
  const [flash, setFlash] = useState<{ kind: "success" | "error" | "info"; text: string } | null>(null);
  const [sel, setSel] = useState<Record<number, Sel>>({});
  const [historyKey, setHistoryKey] = useState(0); // bump to reload the history list after a new PO
  const [partsKey, setPartsKey] = useState(0);

  const loadDists = useCallback(() => {
    api<{ items: Dist[] }>("/purchase-orders/distributors")
      .then((r) => setDists(r.items))
      .catch((e) => setFlash({ kind: "error", text: errMsg(e) }));
  }, []);
  useEffect(loadDists, [loadDists]);

  const tabBtn = (key: Tab, label: string) => (
    <button
      key={key}
      onClick={() => setTab(key)}
      className={`whitespace-nowrap px-4 py-2 text-base font-medium ${
        tab === key ? "border-b-2 border-black text-black" : "text-gray-500 hover:text-black"
      }`}
    >
      {label}
    </button>
  );

  return (
    <div className="space-y-5">
      <h1 className="text-2xl font-semibold">Purchase orders</h1>
      <div className="flex gap-1 overflow-x-auto border-b border-gray-200">
        {tabBtn("reorder", `Reorder parts${Object.keys(sel).length ? ` (${Object.keys(sel).length} ticked)` : ""}`)}
        {tabBtn("history", "PO history")}
        {tabBtn("issues", "Shortages / overages")}
        {tabBtn("distributors", "Distributors")}
      </div>

      {flash && <Notice kind={flash.kind}>{flash.text}</Notice>}

      <div className={tab === "reorder" ? "" : "hidden"}>
        <Reorder
          dists={dists}
          sel={sel}
          setSel={setSel}
          partsKey={partsKey}
          goDistributors={() => setTab("distributors")}
          onCreated={() => {
            setSel({});
            setHistoryKey((k) => k + 1);
            setPartsKey((k) => k + 1);
          }}
          setFlash={setFlash}
        />
      </div>
      <div className={tab === "history" ? "" : "hidden"}>
        <History dists={dists} reloadKey={historyKey} active={tab === "history"} setFlash={setFlash} />
      </div>
      <div className={tab === "issues" ? "" : "hidden"}>
        <Issues reloadKey={historyKey} active={tab === "issues"} setFlash={setFlash} />
      </div>
      <div className={tab === "distributors" ? "" : "hidden"}>
        <Distributors dists={dists} reload={loadDists} setFlash={setFlash} />
      </div>
    </div>
  );
}

type FlashFn = (f: { kind: "success" | "error" | "info"; text: string } | null) => void;

/* ============================================================== Reorder */
function Reorder({
  dists,
  sel,
  setSel,
  partsKey,
  goDistributors,
  onCreated,
  setFlash,
}: {
  dists: Dist[];
  sel: Record<number, Sel>;
  setSel: React.Dispatch<React.SetStateAction<Record<number, Sel>>>;
  partsKey: number;
  goDistributors: () => void;
  onCreated: () => void;
  setFlash: FlashFn;
}) {
  const [showAll, setShowAll] = useState(false);
  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<{ total: number; items: Part[] }>({ total: 0, items: [] });
  const [loaded, setLoaded] = useState(false);
  const [creating, setCreating] = useState(false);
  const [made, setMade] = useState<PoDetail | null>(null);

  useEffect(() => {
    const t = setTimeout(() => {
      setQ(qInput.trim());
      setOffset(0);
    }, 300);
    return () => clearTimeout(t);
  }, [qInput]);

  useEffect(() => {
    const p = new URLSearchParams({ limit: String(PARTS_LIMIT), offset: String(offset) });
    if (q) p.set("q", q);
    if (showAll) p.set("show_all", "true");
    api<{ total: number; items: Part[] }>(`/purchase-orders/parts?${p}`)
      .then((d) => {
        setData(d);
        setLoaded(true);
      })
      .catch((e) => setFlash({ kind: "error", text: errMsg(e) }));
  }, [q, offset, showAll, partsKey, setFlash]);

  function toggle(p: Part) {
    setSel((s) => {
      const next = { ...s };
      if (next[p.id]) delete next[p.id];
      else next[p.id] = { part: p, qty: String(p.suggested_qty), rate: p.cost != null ? p.cost.toFixed(2) : "" };
      return next;
    });
  }

  const ticked = Object.values(sel);

  return (
    <div className="space-y-4">
      <p className="text-base text-gray-600">
        Parts that are low or out of stock. Parts with a restock level come first, biggest shortfall first. Tick the
        parts you want to reorder, then press Create PO. Tick “Show all parts” to add any part.
      </p>

      <div className="flex flex-wrap items-center gap-3">
        <input
          className={`${inp} w-full max-w-md`}
          value={qInput}
          onChange={(e) => setQInput(e.target.value)}
          placeholder="Search title, SKU or part number"
        />
        <label className="flex items-center gap-2 text-base text-gray-700">
          <input
            type="checkbox"
            checked={showAll}
            onChange={(e) => {
              setShowAll(e.target.checked);
              setOffset(0);
            }}
          />
          Show all parts
        </label>
        <button className={btnCls} disabled={ticked.length === 0} onClick={() => setCreating(true)}>
          Create PO{ticked.length ? ` (${ticked.length})` : ""}
        </button>
        {ticked.length > 0 && (
          <button className="text-base underline" onClick={() => setSel({})}>
            Clear ticks
          </button>
        )}
      </div>

      <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white">
        <table className="w-full text-left text-base">
          <thead className="bg-gray-50 text-sm uppercase text-gray-500">
            <tr>
              <th className="px-3 py-2"></th>
              <th className="px-3 py-2">Part</th>
              <th className="px-3 py-2">Part #</th>
              <th className="px-3 py-2">On hand</th>
              <th className="px-3 py-2">Reserved</th>
              <th className="px-3 py-2">Available</th>
              <th className="px-3 py-2">Restock level</th>
              <th className="px-3 py-2">Cost</th>
              <th className="px-3 py-2">On a PO</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {data.items.map((p) => (
              <tr key={p.id} className={sel[p.id] ? "bg-blue-50" : ""}>
                <td className="px-3 py-2">
                  <input type="checkbox" checked={!!sel[p.id]} onChange={() => toggle(p)} aria-label={`Select ${p.sku}`} />
                </td>
                <td className="px-3 py-2">
                  <div className="font-medium">{p.title}</div>
                  <div className="text-sm text-gray-500">{p.sku}</div>
                </td>
                <td className="whitespace-nowrap px-3 py-2">
                  {p.part_number ?? <span className="text-sm text-amber-700">none on file</span>}
                </td>
                <td className="px-3 py-2">{p.on_hand}</td>
                <td className="px-3 py-2">{p.reserved}</td>
                <td className="px-3 py-2 font-semibold">{p.available}</td>
                <td className="px-3 py-2">{p.reorder_point ?? "—"}</td>
                <td className="px-3 py-2">{p.cost != null ? money(p.cost) : "—"}</td>
                <td className="whitespace-nowrap px-3 py-2">
                  {p.open_pos.length > 0 ? <Badge tone="blue">PO {p.open_pos.join(", ")}</Badge> : "—"}
                </td>
              </tr>
            ))}
            {loaded && data.items.length === 0 && (
              <tr>
                <td colSpan={9} className="px-3 py-6 text-center text-gray-500">
                  {showAll || q ? "No parts match." : "Nothing is low or out of stock."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <Pager total={data.total} limit={PARTS_LIMIT} offset={offset} onChange={setOffset} />

      {creating && (
        <CreatePo
          dists={dists}
          sel={sel}
          setSel={setSel}
          goDistributors={() => {
            setCreating(false);
            goDistributors();
          }}
          onClose={() => setCreating(false)}
          onCreated={(po) => {
            setCreating(false);
            setMade(po);
            onCreated();
          }}
          setFlash={setFlash}
        />
      )}

      {made && (
        <Modal title={`PO ${made.po_number} created`} onClose={() => setMade(null)}>
          <div className="space-y-4 text-base">
            <p>
              Purchase order <b>{made.po_number}</b> to <b>{made.vendor_name}</b> for <b>{money(made.total)}</b> is saved in
              PO history. Its parts are now marked Backordered on the Stock tab (un-tick any you don’t want shown that way).
            </p>
            {made.missing_part_numbers && made.missing_part_numbers.length > 0 && (
              <Notice kind="error">
                No manufacturer part number is on file for: {made.missing_part_numbers.join("; ")}. The Item column is blank for
                those lines. Add the part number on the product page and download the PO again.
              </Notice>
            )}
            <p className="font-medium">Download it as:</p>
            <div className="flex flex-wrap gap-2">
              <button
                className={btnCls}
                onClick={() => download(made, "pdf").catch((e) => setFlash({ kind: "error", text: errMsg(e) }))}
              >
                PDF
              </button>
              <button
                className={btnCls}
                onClick={() => download(made, "xlsx").catch((e) => setFlash({ kind: "error", text: errMsg(e) }))}
              >
                Excel
              </button>
              <button className={btn2Cls} onClick={() => setMade(null)}>
                Done
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}

/* ============================================================== Create PO dialog */
function CreatePo({
  dists,
  sel,
  setSel,
  goDistributors,
  onClose,
  onCreated,
  setFlash,
}: {
  dists: Dist[];
  sel: Record<number, Sel>;
  setSel: React.Dispatch<React.SetStateAction<Record<number, Sel>>>;
  goDistributors: () => void;
  onClose: () => void;
  onCreated: (po: PoDetail) => void;
  setFlash: FlashFn;
}) {
  const [distId, setDistId] = useState<number | "">(dists.length === 1 ? dists[0].id : "");
  const [shipTo, setShipTo] = useState("");
  const [defaultShip, setDefaultShip] = useState("");
  const [note, setNote] = useState("");
  const [expected, setExpected] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const lines = Object.values(sel);

  // The usual Ship To address is filled in for you. "Clear" empties the box so a different one can be typed.
  useEffect(() => {
    api<{ ship_to: string }>("/purchase-orders/defaults")
      .then((d) => {
        setDefaultShip(d.ship_to);
        setShipTo(d.ship_to);
      })
      .catch(() => {});
  }, []);

  const edit = (id: number, patch: Partial<Sel>) => setSel((s) => (s[id] ? { ...s, [id]: { ...s[id], ...patch } } : s));
  const bad = lines.some((l) => !Number.isInteger(Number(l.qty)) || Number(l.qty) < 1 || (l.rate !== "" && !(Number(l.rate) >= 0)));
  const total = lines.reduce((n, l) => n + (Number(l.qty) || 0) * (Number(l.rate) || 0), 0);

  async function create() {
    if (distId === "") return setError("Pick a distributor first.");
    if (bad) return setError("Every line needs a whole-number quantity of at least 1 and a cost of 0 or more.");
    setBusy(true);
    setError("");
    try {
      const po = await api<PoDetail>("/purchase-orders", {
        method: "POST",
        body: {
          distributor_id: distId,
          ship_to: shipTo.trim(),
          note: note.trim() || null,
          expected_date: expected || null,
          lines: lines.map((l) => ({
            product_id: l.part.id,
            quantity: Number(l.qty),
            rate: l.rate === "" ? null : Number(l.rate),
          })),
        },
      });
      onCreated(po);
    } catch (e) {
      setError(errMsg(e));
      setFlash(null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title="Create purchase order" onClose={onClose}>
      <div className="space-y-4 text-base">
        {error && <Notice kind="error">{error}</Notice>}

        <div className="space-y-2">
          <div className="font-medium">1. Distributor</div>
          {dists.length === 0 ? (
            <p className="text-gray-600">
              You haven’t added a distributor yet.{" "}
              <button className="underline" onClick={goDistributors}>
                Add one on the Distributors tab
              </button>{" "}
              (your ticked parts are kept).
            </p>
          ) : (
            <div className="flex flex-wrap items-center gap-3">
              <select className={`${inp} max-w-sm`} value={distId} onChange={(e) => setDistId(e.target.value ? Number(e.target.value) : "")}>
                <option value="">Choose a distributor…</option>
                {dists.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name}
                  </option>
                ))}
              </select>
              <button className="underline" onClick={goDistributors}>
                Add / edit / delete distributors
              </button>
            </div>
          )}
        </div>

        <div className="space-y-2">
          <div className="font-medium">2. Parts, quantities and cost</div>
          <div className="overflow-x-auto rounded border border-gray-200">
            <table className="w-full text-left">
              <thead className="bg-gray-50 text-sm uppercase text-gray-500">
                <tr>
                  <th className="px-2 py-1">Item (part #)</th>
                  <th className="px-2 py-1">Description</th>
                  <th className="px-2 py-1">Qty</th>
                  <th className="px-2 py-1">Cost</th>
                  <th className="px-2 py-1"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {lines.map((l) => (
                  <tr key={l.part.id}>
                    <td className="whitespace-nowrap px-2 py-1">{l.part.part_number ?? <span className="text-sm text-amber-700">none on file</span>}</td>
                    <td className="px-2 py-1">{l.part.title}</td>
                    <td className="px-2 py-1">
                      <input type="number" min={1} className={`${inp} w-20`} value={l.qty} onChange={(e) => edit(l.part.id, { qty: e.target.value })} />
                    </td>
                    <td className="px-2 py-1">
                      <input type="number" min={0} step="0.01" className={`${inp} w-24`} value={l.rate} onChange={(e) => edit(l.part.id, { rate: e.target.value })} />
                    </td>
                    <td className="px-2 py-1">
                      <button
                        className="text-red-700 underline"
                        onClick={() =>
                          setSel((s) => {
                            const n = { ...s };
                            delete n[l.part.id];
                            return n;
                          })
                        }
                      >
                        Remove
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="text-right font-semibold">Total {money(total)}</div>
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block font-medium text-gray-700">
            <span className="mb-1 block">Ship To</span>
            <textarea className={inp} rows={4} value={shipTo} placeholder="Type a ship-to address" onChange={(e) => setShipTo(e.target.value)} />
            <span className="mt-1 flex gap-3 text-sm font-normal">
              <button type="button" className="underline" onClick={() => setShipTo("")}>
                Clear
              </button>
              {defaultShip && shipTo !== defaultShip && (
                <button type="button" className="underline" onClick={() => setShipTo(defaultShip)}>
                  Use the default address
                </button>
              )}
            </span>
          </label>
          <div className="space-y-3">
            <label className="block font-medium text-gray-700">
              <span className="mb-1 block">Date you expect the parts (optional)</span>
              <input type="date" className={inp} value={expected} onChange={(e) => setExpected(e.target.value)} />
            </label>
            <label className="block font-medium text-gray-700">
              <span className="mb-1 block">Note on the PO (optional)</span>
              <input className={inp} value={note} maxLength={300} onChange={(e) => setNote(e.target.value)} />
            </label>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <button className={btnCls} disabled={busy || lines.length === 0} onClick={create}>
            {busy ? "Creating…" : "Create PO"}
          </button>
          <button className={btn2Cls} disabled={busy} onClick={onClose}>
            Cancel
          </button>
          <span className="text-sm text-gray-500">The part numbers come from each product’s manufacturer part number.</span>
        </div>
      </div>
    </Modal>
  );
}

/* ============================================================== History */
function History({ dists, reloadKey, active, setFlash }: { dists: Dist[]; reloadKey: number; active: boolean; setFlash: FlashFn }) {
  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<{ total: number; items: PoRow[] }>({ total: 0, items: [] });
  const [loaded, setLoaded] = useState(false);
  const [open, setOpen] = useState<PoDetail | null>(null);
  const [editing, setEditing] = useState<PoDetail | null>(null);
  const [bump, setBump] = useState(0); // reload the list after an edit, delete or sent mark

  useEffect(() => {
    const t = setTimeout(() => {
      setQ(qInput.trim());
      setOffset(0);
    }, 300);
    return () => clearTimeout(t);
  }, [qInput]);

  useEffect(() => {
    if (!active) return;
    const p = new URLSearchParams({ limit: String(PO_LIMIT), offset: String(offset) });
    if (q) p.set("q", q);
    api<{ total: number; items: PoRow[] }>(`/purchase-orders?${p}`)
      .then((d) => {
        setData(d);
        setLoaded(true);
      })
      .catch((e) => setFlash({ kind: "error", text: errMsg(e) }));
  }, [active, q, offset, reloadKey, bump, setFlash]);

  const view = (id: number, edit = false) =>
    api<PoDetail>(`/purchase-orders/${id}`)
      .then((po) => (edit ? setEditing(po) : setOpen(po)))
      .catch((e) => setFlash({ kind: "error", text: errMsg(e) }));

  async function cancelPo(po: { id: number; po_number: number }) {
    if (
      !window.confirm(
        `Cancel purchase order ${po.po_number}? It stays in the history, marked cancelled, and its number is not reused. Stock counts are not changed, and parts that are on no other open PO stop being Backordered.`
      )
    )
      return;
    try {
      await api(`/purchase-orders/${po.id}/cancel`, { method: "POST", body: {} });
      setOpen(null);
      setEditing(null);
      setBump((b) => b + 1);
      setFlash({ kind: "success", text: `Purchase order ${po.po_number} cancelled.` });
    } catch (e) {
      setFlash({ kind: "error", text: errMsg(e) });
    }
  }

  async function markSent(po: PoDetail, sent: boolean) {
    try {
      const r = await api<{ sent_at: string | null }>(`/purchase-orders/${po.id}/sent`, { method: "PUT", body: { sent } });
      setOpen({ ...po, sent_at: r.sent_at });
      setBump((b) => b + 1);
    } catch (e) {
      setFlash({ kind: "error", text: errMsg(e) });
    }
  }
  const dl = (po: { id: number; po_number: number }, fmt: "pdf" | "xlsx") =>
    download(po, fmt).catch((e) => setFlash({ kind: "error", text: errMsg(e) }));

  return (
    <div className="space-y-4">
      <input
        className={`${inp} w-full max-w-3xl`}
        value={qInput}
        onChange={(e) => setQInput(e.target.value)}
        placeholder="Search PO number, distributor, part number, SKU or description"
      />
      <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white">
        <table className="w-full text-left text-base">
          <thead className="bg-gray-50 text-sm uppercase text-gray-500">
            <tr>
              <th className="px-3 py-2">PO #</th>
              <th className="px-3 py-2">Date</th>
              <th className="px-3 py-2">Distributor</th>
              <th className="px-3 py-2">Lines</th>
              <th className="px-3 py-2">Total</th>
              <th className="px-3 py-2">Made by</th>
              <th className="px-3 py-2">Sent</th>
              <th className="px-3 py-2"></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {data.items.map((r) => (
              <tr key={r.id}>
                <td className="px-3 py-2 font-semibold">{r.po_number}</td>
                <td className="whitespace-nowrap px-3 py-2">{new Date(r.created_at).toLocaleDateString()}</td>
                <td className="px-3 py-2">{r.vendor_name}</td>
                <td className="whitespace-nowrap px-3 py-2">
                  {r.line_count}
                  {r.status === "cancelled" ? (
                    <span className="ml-2"><Badge tone="gray">cancelled</Badge></span>
                  ) : r.status === "received" ? (
                    <span className="ml-2"><Badge tone="green">received</Badge></span>
                  ) : r.open_lines < r.line_count ? (
                    <span className="ml-2"><Badge tone="amber">{r.open_lines} open</Badge></span>
                  ) : null}
                  {r.issue_count > 0 && <span className="ml-2"><Badge tone="amber">{r.issue_count} issue{r.issue_count === 1 ? "" : "s"}</Badge></span>}
                </td>
                <td className="px-3 py-2">{money(r.total_cents / 100)}</td>
                <td className="px-3 py-2 text-gray-600">{who(r.created_by_name, r.created_by)}</td>
                <td className="whitespace-nowrap px-3 py-2">
                  {r.status === "cancelled" ? <Badge tone="gray">cancelled</Badge> : r.sent_at ? <Badge tone="green">sent {new Date(r.sent_at).toLocaleDateString()}</Badge> : <Badge tone="amber">not sent</Badge>}
                </td>
                <td className="whitespace-nowrap px-3 py-2">
                  <div className="flex gap-2">
                    <button className={btn2Cls} onClick={() => view(r.id)}>View</button>
                    {r.status !== "cancelled" && <button className={btn2Cls} onClick={() => view(r.id, true)}>Edit</button>}
                    <button className={btn2Cls} onClick={() => dl(r, "pdf")}>PDF</button>
                    <button className={btn2Cls} onClick={() => dl(r, "xlsx")}>Excel</button>
                    {r.status === "open" && <button className={dangerCls} onClick={() => cancelPo(r)}>Cancel PO</button>}
                  </div>
                </td>
              </tr>
            ))}
            {loaded && data.items.length === 0 && (
              <tr>
                <td colSpan={8} className="px-3 py-6 text-center text-gray-500">
                  {q ? "No purchase orders match." : "No purchase orders yet. Create one on the Reorder parts tab."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <Pager total={data.total} limit={PO_LIMIT} offset={offset} onChange={setOffset} />

      {editing && (
        <EditPo
          po={editing}
          dists={dists}
          onClose={() => setEditing(null)}
          onSaved={(po) => {
            setEditing(null);
            setOpen(po);
            setBump((b) => b + 1);
            setFlash({
              kind: "success",
              text: po.renumbered
                ? `Saved. It is now PO ${po.po_number} and is marked not sent. Download it again to send the new one.`
                : "Saved. Download it again if you need the updated PO.",
            });
          }}
          setFlash={setFlash}
        />
      )}

      {open && (
        <Modal title={`Purchase order ${open.po_number}`} onClose={() => setOpen(null)}>
          <div className="space-y-3 text-base">
            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <div className="text-sm font-semibold uppercase text-gray-500">Vendor</div>
                <div className="font-medium">{open.vendor_name}</div>
                <div className="whitespace-pre-line text-gray-600">{open.vendor_address}</div>
              </div>
              <div>
                <div className="text-sm font-semibold uppercase text-gray-500">Ship to</div>
                <div className="whitespace-pre-line text-gray-600">{open.ship_to}</div>
              </div>
            </div>
            <div className="text-gray-600">
              Dated {new Date(open.date + "T12:00:00").toLocaleDateString()} · made by {who(open.created_by_name, open.created_by)}
              {open.note ? ` · ${open.note}` : ""}
            </div>
            {open.status === "cancelled" && (
              <Notice kind="error">
                Cancelled{open.cancelled_at ? ` ${new Date(open.cancelled_at).toLocaleDateString()}` : ""}
                {open.cancelled_by_name || open.cancelled_by ? ` by ${who(open.cancelled_by_name, open.cancelled_by)}` : ""}. It is kept for the record and its number won’t be reused.
              </Notice>
            )}
            <div className="overflow-x-auto rounded border border-gray-200">
              <table className="w-full text-left">
                <thead className="bg-gray-50 text-sm uppercase text-gray-500">
                  <tr>
                    <th className="px-2 py-1">Item</th>
                    <th className="px-2 py-1">Description</th>
                    <th className="px-2 py-1">Qty</th>
                    <th className="px-2 py-1">Cost</th>
                    <th className="px-2 py-1">Amount</th>
                    <th className="px-2 py-1">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {open.lines.map((l) => (
                    <tr key={l.id}>
                      <td className="whitespace-nowrap px-2 py-1">{l.item ?? "—"}</td>
                      <td className="px-2 py-1">{l.description}</td>
                      <td className="px-2 py-1">{l.quantity}</td>
                      <td className="px-2 py-1">{money(l.rate)}</td>
                      <td className="px-2 py-1">{money(l.amount)}</td>
                      <td className="whitespace-nowrap px-2 py-1">
                        {l.received_at ? (
                          <Badge tone={l.received_qty != null && l.received_qty !== l.quantity ? "amber" : "green"}>
                            received {l.received_qty ?? l.quantity} of {l.quantity} · {new Date(l.received_at).toLocaleDateString()}
                          </Badge>
                        ) : open.status === "cancelled" ? (
                          <Badge tone="gray">cancelled</Badge>
                        ) : (l.received_qty ?? 0) > 0 ? (
                          <Badge tone="amber">{l.received_qty} of {l.quantity} in · rest expected</Badge>
                        ) : (
                          <Badge tone="amber">open</Badge>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="text-right text-base font-semibold">Total {money(open.total)}</div>
            <div className="flex flex-wrap items-center gap-2">
              {open.sent_at ? <Badge tone="green">sent {new Date(open.sent_at).toLocaleDateString()}</Badge> : <Badge tone="amber">not sent</Badge>}
              {open.status === "received" && <Badge tone="green">received in</Badge>}
              {open.status !== "cancelled" && (
                <button className={btn2Cls} onClick={() => markSent(open, !open.sent_at)}>
                  {open.sent_at ? "Mark as not sent" : "Mark as sent"}
                </button>
              )}
              {open.status === "open" && !open.sent_at && (
                <span className="text-sm text-gray-500">Mark it sent to make it appear on Inventory › Receive.</span>
              )}
            </div>
            <div className="flex flex-wrap gap-2">
              <button className={btnCls} onClick={() => dl(open, "pdf")}>Download PDF</button>
              <button className={btnCls} onClick={() => dl(open, "xlsx")}>Download Excel</button>
              {open.status !== "cancelled" && (
                <button
                  className={btn2Cls}
                  onClick={() => {
                    setEditing(open);
                    setOpen(null);
                  }}
                >
                  Edit PO
                </button>
              )}
              {open.status === "open" && <button className={dangerCls} onClick={() => cancelPo(open)}>Cancel PO</button>}
              <button className={btn2Cls} onClick={() => setOpen(null)}>Close</button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}

/* ============================================================== Shortages / overages */
function Issues({ reloadKey, active, setFlash }: { reloadKey: number; active: boolean; setFlash: FlashFn }) {
  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState("");
  const [kind, setKind] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<{ total: number; items: IssuePo[] }>({ total: 0, items: [] });
  const [loaded, setLoaded] = useState(false);
  const [open, setOpen] = useState<PoDetail | null>(null);

  useEffect(() => {
    const t = setTimeout(() => {
      setQ(qInput.trim());
      setOffset(0);
    }, 300);
    return () => clearTimeout(t);
  }, [qInput]);

  useEffect(() => {
    if (!active) return;
    const p = new URLSearchParams({ limit: String(PO_LIMIT), offset: String(offset) });
    if (q) p.set("q", q);
    if (kind) p.set("kind", kind);
    api<{ total: number; items: IssuePo[] }>(`/purchase-orders/issues?${p}`)
      .then((d) => {
        setData(d);
        setLoaded(true);
      })
      .catch((e) => setFlash({ kind: "error", text: errMsg(e) }));
  }, [active, q, kind, offset, reloadKey, setFlash]);

  const view = (id: number) =>
    api<PoDetail>(`/purchase-orders/${id}`)
      .then(setOpen)
      .catch((e) => setFlash({ kind: "error", text: errMsg(e) }));

  return (
    <div className="space-y-4">
      <p className="text-base text-gray-600">
        Every purchase order that had a problem when it was received: <strong>short</strong> (fewer than ordered and the
        rest isn’t coming) or <strong>over</strong> (more than ordered, or a part that wasn’t on the PO). A short line whose
        rest is still expected stays on its open PO and isn’t listed here. Each one shows who acknowledged it and when.
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <input
          className={`${inp} w-full max-w-3xl`}
          value={qInput}
          onChange={(e) => setQInput(e.target.value)}
          placeholder="Search PO number, distributor, part number, SKU or description"
        />
        <select
          className={`${inp} w-auto`}
          value={kind}
          onChange={(e) => {
            setKind(e.target.value);
            setOffset(0);
          }}
        >
          <option value="">All problems</option>
          <option value="short">Short</option>
          <option value="over">Over (including not on the PO)</option>
          <option value="not_on_po">Over: not on the PO only</option>
        </select>
      </div>

      <div className="space-y-3">
        {data.items.map((po) => (
          <div key={po.id} className="overflow-hidden rounded-lg border border-gray-200 bg-white">
            <div className="flex flex-wrap items-center justify-between gap-2 bg-gray-50 px-3 py-2">
              <div className="flex flex-wrap items-center gap-3">
                <span className="text-lg font-semibold">PO {po.po_number}</span>
                <span className="text-gray-600">{po.vendor_name}</span>
                <span className="text-sm text-gray-500">{new Date(po.created_at).toLocaleDateString()}</span>
                {po.status === "received" && <Badge tone="green">received</Badge>}
                {po.status === "open" && <Badge tone="amber">still receiving</Badge>}
                {po.status === "cancelled" && <Badge tone="gray">cancelled</Badge>}
                <Badge tone="amber">{po.issue_count} issue{po.issue_count === 1 ? "" : "s"}</Badge>
              </div>
              <button className={btn2Cls} onClick={() => view(po.id)}>View PO</button>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-base">
                <thead className="text-sm uppercase text-gray-500">
                  <tr>
                    <th className="px-3 py-1">Problem</th>
                    <th className="px-3 py-1">Part</th>
                    <th className="px-3 py-1">Ordered</th>
                    <th className="px-3 py-1">Received</th>
                    <th className="px-3 py-1">Difference</th>
                    <th className="px-3 py-1">Acknowledged by</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {po.issues.map((i) => (
                    <tr key={i.id}>
                      <td className="whitespace-nowrap px-3 py-1"><Badge tone={KIND_TONE[i.kind]}>{KIND_LABEL[i.kind]}</Badge></td>
                      <td className="px-3 py-1">
                        <div>{i.description ?? "—"}</div>
                        <div className="text-sm text-gray-500">{[i.item, i.sku].filter(Boolean).join(" · ")}</div>
                      </td>
                      <td className="px-3 py-1">{i.expected_qty}</td>
                      <td className="px-3 py-1">{i.received_qty}</td>
                      <td className={`px-3 py-1 font-semibold ${i.difference < 0 ? "text-red-700" : "text-blue-700"}`}>
                        {i.difference > 0 ? `+${i.difference}` : i.difference}
                      </td>
                      <td className="px-3 py-1">
                        <div>{who(i.acknowledged_by_name, i.acknowledged_by)}</div>
                        <div className="text-sm text-gray-500">{new Date(i.acknowledged_at).toLocaleString()}</div>
                        {i.note && <div className="text-sm text-gray-600">{i.note}</div>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ))}
        {loaded && data.items.length === 0 && (
          <p className="rounded border border-dashed border-gray-300 p-6 text-center text-gray-500">
            {q || kind ? "No purchase orders with issues match." : "No shortages or overages have been recorded."}
          </p>
        )}
      </div>
      <Pager total={data.total} limit={PO_LIMIT} offset={offset} onChange={setOffset} />

      {open && (
        <Modal title={`Purchase order ${open.po_number}`} onClose={() => setOpen(null)}>
          <div className="space-y-3 text-base">
            <div className="text-gray-600">
              {open.vendor_name} · dated {new Date(open.date + "T12:00:00").toLocaleDateString()} · made by {who(open.created_by_name, open.created_by)}
            </div>
            <div className="overflow-x-auto rounded border border-gray-200">
              <table className="w-full text-left">
                <thead className="bg-gray-50 text-sm uppercase text-gray-500">
                  <tr>
                    <th className="px-2 py-1">Item</th>
                    <th className="px-2 py-1">Description</th>
                    <th className="px-2 py-1">Ordered</th>
                    <th className="px-2 py-1">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {open.lines.map((l) => (
                    <tr key={l.id}>
                      <td className="whitespace-nowrap px-2 py-1">{l.item ?? "—"}</td>
                      <td className="px-2 py-1">{l.description}</td>
                      <td className="px-2 py-1">{l.quantity}</td>
                      <td className="whitespace-nowrap px-2 py-1">
                        {l.received_at ? (
                          <Badge tone={l.received_qty != null && l.received_qty !== l.quantity ? "amber" : "green"}>
                            received {l.received_qty ?? l.quantity} of {l.quantity}
                          </Badge>
                        ) : (l.received_qty ?? 0) > 0 ? (
                          <Badge tone="amber">{l.received_qty} of {l.quantity} in · rest expected</Badge>
                        ) : (
                          <Badge tone="amber">open</Badge>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <button className={btn2Cls} onClick={() => setOpen(null)}>Close</button>
          </div>
        </Modal>
      )}
    </div>
  );
}

/* ============================================================== Distributors */
type DForm = { name: string; address: string; email: string; phone: string; notes: string };
const blank: DForm = { name: "", address: "", email: "", phone: "", notes: "" };

function Distributors({ dists, reload, setFlash }: { dists: Dist[]; reload: () => void; setFlash: FlashFn }) {
  const [editing, setEditing] = useState<number | "new" | null>(null);
  const [form, setForm] = useState<DForm>(blank);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  function start(d?: Dist) {
    setError("");
    setEditing(d ? d.id : "new");
    setForm(d ? { name: d.name, address: d.address ?? "", email: d.email ?? "", phone: d.phone ?? "", notes: d.notes ?? "" } : blank);
  }

  async function save() {
    if (!form.name.trim()) return setError("A distributor needs a name.");
    setBusy(true);
    setError("");
    try {
      await api(editing === "new" ? "/purchase-orders/distributors" : `/purchase-orders/distributors/${editing}`, {
        method: editing === "new" ? "POST" : "PUT",
        body: form,
      });
      setEditing(null);
      reload();
      setFlash({ kind: "success", text: "Distributor saved." });
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove(d: Dist) {
    if (!window.confirm(`Delete ${d.name}? Purchase orders already made keep their own copy of the vendor.`)) return;
    try {
      await api(`/purchase-orders/distributors/${d.id}`, { method: "DELETE" });
      reload();
      setFlash({ kind: "success", text: `${d.name} deleted.` });
    } catch (e) {
      setFlash({ kind: "error", text: errMsg(e) });
    }
  }

  const set = (k: keyof DForm, v: string) => setForm((f) => ({ ...f, [k]: v }));

  return (
    <div className="space-y-4">
      <p className="text-base text-gray-600">
        The distributors you order from. The address prints in the Vendor box on the PO, one line per line you type.
      </p>
      <button className={btnCls} onClick={() => start()}>
        Add a distributor
      </button>

      {editing !== null && (
        <div className="space-y-3 rounded-lg border border-gray-200 bg-white p-4 text-base">
          <div className="font-semibold">{editing === "new" ? "New distributor" : "Edit distributor"}</div>
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
            <button className={btn2Cls} disabled={busy} onClick={() => setEditing(null)}>
              Cancel
            </button>
          </div>
        </div>
      )}

      <ul className="divide-y divide-gray-100 rounded-lg border border-gray-200 bg-white">
        {dists.length === 0 && <li className="p-6 text-center text-base text-gray-500">No distributors yet.</li>}
        {dists.map((d) => (
          <li key={d.id} className="flex flex-wrap items-start justify-between gap-3 p-4 text-base">
            <div>
              <div className="font-semibold">{d.name}</div>
              <div className="whitespace-pre-line text-gray-600">{d.address}</div>
              <div className="text-gray-500">{[d.email, d.phone].filter(Boolean).join(" · ")}</div>
              {d.notes && <div className="text-sm text-gray-500">{d.notes}</div>}
            </div>
            <div className="flex gap-2">
              <button className={btn2Cls} onClick={() => start(d)}>Edit</button>
              <button className={dangerCls} onClick={() => remove(d)}>Delete</button>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

/* ============================================================== Edit PO dialog */
type EditLine = { line_id: number | null; product_id: number; item: string | null; description: string; qty: string; cost: string };

function EditPo({
  po,
  dists,
  onClose,
  onSaved,
  setFlash,
}: {
  po: PoDetail;
  dists: Dist[];
  onClose: () => void;
  onSaved: (po: PoDetail) => void;
  setFlash: FlashFn;
}) {
  const sent = !!po.sent_at;
  const [defaults, setDefaults] = useState<{ ship_to: string; next_po_number: number } | null>(null);
  const [distId, setDistId] = useState<number | "">(po.distributor_id ?? "");
  const [number, setNumber] = useState(String(po.po_number));
  const [shipTo, setShipTo] = useState(po.ship_to ?? "");
  const [note, setNote] = useState(po.note ?? "");
  const [expected, setExpected] = useState("");
  const [lines, setLines] = useState<EditLine[]>(
    po.lines
      .filter((l) => !l.received_at && l.product_id != null)
      .map((l) => ({
        line_id: l.id,
        product_id: l.product_id as number,
        item: l.item,
        description: l.description,
        qty: String(l.quantity),
        cost: l.rate.toFixed(2),
      }))
  );
  const received = po.lines.filter((l) => !!l.received_at);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // add a part
  const [qInput, setQInput] = useState("");
  const [found, setFound] = useState<Part[]>([]);

  useEffect(() => {
    api<{ ship_to: string; next_po_number: number }>("/purchase-orders/defaults")
      .then(setDefaults)
      .catch(() => {});
  }, []);

  useEffect(() => {
    const t = qInput.trim();
    if (t.length < 2) {
      setFound([]);
      return;
    }
    const h = setTimeout(() => {
      api<{ items: Part[] }>(`/purchase-orders/parts?${new URLSearchParams({ q: t, show_all: "true", limit: "8" })}`)
        .then((d) => setFound(d.items))
        .catch(() => setFound([]));
    }, 300);
    return () => clearTimeout(h);
  }, [qInput]);

  const patch = (i: number, p: Partial<EditLine>) => setLines((ls) => ls.map((l, n) => (n === i ? { ...l, ...p } : l)));
  function addPart(p: Part) {
    if (lines.some((l) => l.product_id === p.id)) {
      setError("That part is already on this PO. Change its quantity instead.");
      return;
    }
    setError("");
    setLines((ls) => [
      ...ls,
      { line_id: null, product_id: p.id, item: p.part_number, description: p.title, qty: String(p.suggested_qty), cost: p.cost != null ? p.cost.toFixed(2) : "" },
    ]);
    setQInput("");
    setFound([]);
  }

  const numberChanged = Number(number) !== po.po_number;
  const total =
    lines.reduce((n, l) => n + (Number(l.qty) || 0) * (Number(l.cost) || 0), 0) +
    received.reduce((n, l) => n + l.amount, 0);

  async function save() {
    if (!Number.isInteger(Number(number)) || Number(number) < 1) return setError("The PO number must be a whole number, 1 or more.");
    if (lines.some((l) => !Number.isInteger(Number(l.qty)) || Number(l.qty) < 1 || (l.cost !== "" && !(Number(l.cost) >= 0))))
      return setError("Every line needs a whole-number quantity of at least 1 and a cost of 0 or more.");
    if (lines.length === 0 && received.length === 0) return setError("A PO needs at least one part. To get rid of it, cancel the PO from the history list.");
    setBusy(true);
    setError("");
    try {
      const saved = await api<PoDetail>(`/purchase-orders/${po.id}`, {
        method: "PUT",
        body: {
          distributor_id: distId === "" ? null : distId,
          po_number: numberChanged ? Number(number) : null,
          ship_to: shipTo.trim(),
          note: note.trim(),
          expected_date: expected || null,
          lines: lines.map((l) => ({
            line_id: l.line_id,
            product_id: l.product_id,
            quantity: Number(l.qty),
            rate: l.cost === "" ? null : Number(l.cost),
            description: l.description,
          })),
        },
      });
      onSaved(saved);
    } catch (e) {
      setError(errMsg(e));
      setFlash(null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={`Edit purchase order ${po.po_number}`} onClose={onClose}>
      <div className="space-y-4 text-base">
        {error && <Notice kind="error">{error}</Notice>}

        {sent && (
          <Notice kind="info">
            This PO was already sent{po.sent_at ? ` on ${new Date(po.sent_at).toLocaleDateString()}` : ""}. If you change what is on it,
            give it a new, unused PO number below so the distributor doesn’t mix it up with the one they have. Changing the number marks
            it as not sent.
          </Notice>
        )}

        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block font-medium text-gray-700">
            <span className="mb-1 block">PO number</span>
            <div className="flex items-center gap-2">
              <input type="number" min={1} className={`${inp} w-32`} value={number} onChange={(e) => setNumber(e.target.value)} />
              {defaults && (
                <button type="button" className="whitespace-nowrap text-base underline" onClick={() => setNumber(String(defaults.next_po_number))}>
                  Use the next unused ({defaults.next_po_number})
                </button>
              )}
            </div>
            {numberChanged && <span className="mt-1 block text-base font-normal text-amber-700">Was {po.po_number}. It must not be used by another PO.</span>}
          </label>
          <label className="block font-medium text-gray-700">
            <span className="mb-1 block">Distributor</span>
            <select className={inp} value={distId} onChange={(e) => setDistId(e.target.value ? Number(e.target.value) : "")}>
              {po.distributor_id == null && <option value="">{po.vendor_name} (deleted distributor)</option>}
              {dists.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name}
                </option>
              ))}
            </select>
          </label>
        </div>

        <div className="space-y-2">
          <div className="font-medium">Parts, quantities and cost</div>
          <div className="overflow-x-auto rounded border border-gray-200">
            <table className="w-full text-left">
              <thead className="bg-gray-50 text-base uppercase text-gray-500">
                <tr>
                  <th className="px-2 py-1">Item (part #)</th>
                  <th className="px-2 py-1">Description</th>
                  <th className="px-2 py-1">Qty</th>
                  <th className="px-2 py-1">Cost</th>
                  <th className="px-2 py-1"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {lines.map((l, i) => (
                  <tr key={l.product_id}>
                    <td className="whitespace-nowrap px-2 py-1">{l.item ?? <span className="text-base text-amber-700">none on file</span>}</td>
                    <td className="px-2 py-1">
                      <input className={inp} value={l.description} maxLength={250} onChange={(e) => patch(i, { description: e.target.value })} />
                    </td>
                    <td className="px-2 py-1">
                      <input type="number" min={1} className={`${inp} w-20`} value={l.qty} onChange={(e) => patch(i, { qty: e.target.value })} />
                    </td>
                    <td className="px-2 py-1">
                      <input type="number" min={0} step="0.01" className={`${inp} w-24`} value={l.cost} onChange={(e) => patch(i, { cost: e.target.value })} />
                    </td>
                    <td className="px-2 py-1">
                      <button className="text-red-700 underline" onClick={() => setLines((ls) => ls.filter((_, n) => n !== i))}>
                        Remove
                      </button>
                    </td>
                  </tr>
                ))}
                {received.map((l) => (
                  <tr key={l.id} className="bg-gray-50 text-gray-500">
                    <td className="whitespace-nowrap px-2 py-1">{l.item ?? "—"}</td>
                    <td className="px-2 py-1">{l.description}</td>
                    <td className="px-2 py-1">{l.quantity}</td>
                    <td className="px-2 py-1">{money(l.rate)}</td>
                    <td className="whitespace-nowrap px-2 py-1 text-base">received (locked)</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="text-right font-semibold">Total {money(total)}</div>
          <p className="text-base text-gray-500">
            Cost is what you pay the distributor. It starts from the product’s cost and is never shown to customers.
          </p>
        </div>

        <div className="space-y-2">
          <div className="font-medium">Add a part</div>
          <input className={`${inp} max-w-md`} value={qInput} onChange={(e) => setQInput(e.target.value)} placeholder="Search title, SKU or part number" />
          {found.length > 0 && (
            <ul className="divide-y divide-gray-100 rounded border border-gray-200">
              {found.map((p) => (
                <li key={p.id} className="flex items-center justify-between gap-3 px-2 py-1">
                  <span>
                    {p.title} <span className="text-base text-gray-500">{p.sku}{p.part_number ? ` · ${p.part_number}` : ""}</span>
                  </span>
                  <button className={btn2Cls} onClick={() => addPart(p)}>Add</button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block font-medium text-gray-700">
            <span className="mb-1 block">Ship To</span>
            <textarea className={inp} rows={4} value={shipTo} placeholder="Type a ship-to address" onChange={(e) => setShipTo(e.target.value)} />
            <span className="mt-1 flex gap-3 text-base font-normal">
              <button type="button" className="underline" onClick={() => setShipTo("")}>
                Clear
              </button>
              {defaults && shipTo !== defaults.ship_to && (
                <button type="button" className="underline" onClick={() => setShipTo(defaults.ship_to)}>
                  Use the default address
                </button>
              )}
            </span>
          </label>
          <div className="space-y-3">
            <label className="block font-medium text-gray-700">
              <span className="mb-1 block">Change the date you expect the parts (optional)</span>
              <input type="date" className={inp} value={expected} onChange={(e) => setExpected(e.target.value)} />
            </label>
            <label className="block font-medium text-gray-700">
              <span className="mb-1 block">Note on the PO</span>
              <input className={inp} value={note} maxLength={300} onChange={(e) => setNote(e.target.value)} />
            </label>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <button className={btnCls} disabled={busy} onClick={save}>
            {busy ? "Saving…" : "Save changes"}
          </button>
          <button className={btn2Cls} disabled={busy} onClick={onClose}>
            Cancel
          </button>
        </div>
      </div>
    </Modal>
  );
}
