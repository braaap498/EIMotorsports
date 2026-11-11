"use client";

import ClearableInput from "./ClearableInput";
import ClickRow from "./ClickRow";
import { useCallback, useEffect, useState } from "react";
import { api, apiDownload, errMsg } from "../../lib/adminApi";
import { Badge, Modal, Notice, Pager, btnCls, btn2Cls, dangerCls, inputCls } from "./ui";
import { SortTh, useSort } from "./SortHeader";

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
  // The distributor parts (Edit product > Distributor parts) that make up this part. Part # and Cost above come from these.
  pieces: { part_number: string; quantity: number; vendor: string | null; cost: number | null }[];
};
type PartsResp = { total: number; low_count: number; out_count: number; items: Part[] };
type View = "all" | "low" | "out";
type PartSort = "sku" | "part" | "partnumber" | "onhand" | "reserved" | "available" | "level" | "cost" | "po";
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
  cancelled_by: string | null;
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
  po_type?: "stock" | "drop_ship" | null;
  supplier_order_number?: string | null;
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
  po_type?: "stock" | "drop_ship" | null;
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
type Tab = "reorder" | "history" | "issues";

const PARTS_LIMIT = 50;
const PO_LIMIT = 30;
const money = (n: number) => `$${n.toFixed(2)}`;
const inp = inputCls;
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
 *  - History: every PO made, searchable; cancelled and received POs can be deleted (the number is then free for the next new PO); view it or download it again as PDF or Excel.
 *  - Shortages / overages: every PO that had a receiving problem, searchable.
 * The distributors you order from are managed on the Distributors page.
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

  // Distributors are managed on their own page. It opens in a new tab so the ticked parts here are kept;
  // coming back to this tab reloads the list, so a distributor you just added is there to pick.
  useEffect(() => {
    window.addEventListener("focus", loadDists);
    return () => window.removeEventListener("focus", loadDists);
  }, [loadDists]);
  const openDistributors = () => window.open("/admin/distributors", "_blank", "noopener");

  const tabBtn = (key: Tab, label: string) => (
    <button
      key={key}
      onClick={() => setTab(key)}
      className={`whitespace-nowrap px-4 py-2 text-sm font-medium ${
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
      </div>

      {flash && <Notice kind={flash.kind}>{flash.text}</Notice>}

      <div className={tab === "reorder" ? "" : "hidden"}>
        <Reorder
          dists={dists}
          sel={sel}
          setSel={setSel}
          partsKey={partsKey}
          goDistributors={openDistributors}
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
  const [view, setView] = useState<View>("all");
  const { sort, toggle } = useSort<PartSort>();
  const onSort = (k: PartSort) => {
    toggle(k);
    setOffset(0);
  };
  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<PartsResp>({ total: 0, low_count: 0, out_count: 0, items: [] });
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
    const p = new URLSearchParams({ limit: String(PARTS_LIMIT), offset: String(offset), view });
    if (sort.key) {
      p.set("sort", sort.key);
      p.set("dir", sort.dir);
    }
    if (q) p.set("q", q);
    api<PartsResp>(`/purchase-orders/parts?${p}`)
      .then((d) => {
        setData(d);
        setLoaded(true);
      })
      .catch((e) => setFlash({ kind: "error", text: errMsg(e) }));
  }, [q, offset, view, sort.key, sort.dir, partsKey, setFlash]);

  function toggle(p: Part) {
    setSel((s) => {
      const next = { ...s };
      if (next[p.id]) delete next[p.id];
      else next[p.id] = { part: p, qty: String(p.suggested_qty), rate: p.cost != null ? p.cost.toFixed(2) : "" };
      return next;
    });
  }

  const ticked = Object.values(sel);
  const viewBtn = (v: View, label: string) => (
    <button
      type="button"
      className={`px-4 py-2 ${view === v ? "bg-black text-white" : "bg-white text-gray-700"}`}
      onClick={() => {
        setView(v);
        setOffset(0);
      }}
    >
      {label}
    </button>
  );

  return (
    <div className="space-y-4">
      <p className="text-sm text-gray-600">
        Pick All, Low stock or Out of stock, the same lists as Inventory &gt; Stock. Low stock parts with the biggest shortfall come
        first. Part # and Cost are the distributor’s part number and our cost from the distributor (Edit product &gt; Distributor
        parts). Tick the parts you want to reorder, then press Create PO.
      </p>

      <div className="flex flex-wrap items-center gap-3">
        <div className="inline-flex overflow-hidden rounded border border-gray-300 text-sm">
          {viewBtn("all", "All")}
          {viewBtn("low", `Low stock (${data.low_count})`)}
          {viewBtn("out", `Out of stock (${data.out_count})`)}
        </div>
        <ClearableInput
          className={`${inp} w-[24rem] max-w-full`}
          value={qInput}
          onChange={(e) => setQInput(e.target.value)}
          placeholder="Search title, SKU or part number"
        />
        <button className={btnCls} disabled={ticked.length === 0} onClick={() => setCreating(true)}>
          Create PO{ticked.length ? ` (${ticked.length})` : ""}
        </button>
        {ticked.length > 0 && (
          <button className="text-sm underline" onClick={() => setSel({})}>
            Clear ticks
          </button>
        )}
      </div>

      <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white">
        <table className="w-full text-left text-sm">
          <thead className="bg-gray-50 text-xs uppercase text-gray-500">
            <tr>
              <th className="px-3 py-2"></th>
              <SortTh label="SKU" k="sku" sort={sort} onSort={onSort} />
              <SortTh label="Part" k="part" sort={sort} onSort={onSort} />
              <SortTh label="Part #" k="partnumber" sort={sort} onSort={onSort} />
              <SortTh label="On hand" k="onhand" sort={sort} onSort={onSort} />
              <SortTh label="Reserved" k="reserved" sort={sort} onSort={onSort} />
              <SortTh label="Available" k="available" sort={sort} onSort={onSort} />
              <SortTh label="Restock level" k="level" sort={sort} onSort={onSort} />
              <SortTh label="Our cost" k="cost" sort={sort} onSort={onSort} />
              <SortTh label="On a PO" k="po" sort={sort} onSort={onSort} />
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {data.items.map((p) => (
              <tr key={p.id} className={sel[p.id] ? "bg-blue-50" : ""}>
                <td className="px-3 py-2">
                  <input type="checkbox" checked={!!sel[p.id]} onChange={() => toggle(p)} aria-label={`Select ${p.sku}`} />
                </td>
                <td className="whitespace-nowrap px-3 py-2">{p.sku}</td>
                <td className="min-w-[14rem] px-3 py-2">
                  <div className="font-medium">{p.title}</div>
                </td>
                <td className="px-3 py-2">
                  {p.pieces.length > 0 ? (
                    <div className="space-y-0.5">
                      {p.pieces.map((pc, n) => (
                        <div key={n} className="whitespace-nowrap">
                          {pc.part_number}
                          {pc.quantity > 1 && <span className="text-gray-500"> ×{pc.quantity}</span>}
                          {pc.vendor && <span className="text-xs text-gray-500"> · {pc.vendor}</span>}
                        </div>
                      ))}
                    </div>
                  ) : (
                    <span className="whitespace-nowrap">{p.part_number ?? <span className="text-amber-700">none on file</span>}</span>
                  )}
                </td>
                <td className="px-3 py-2">{p.on_hand}</td>
                <td className="px-3 py-2">{p.reserved}</td>
                <td className="px-3 py-2 font-semibold">{p.available}</td>
                <td className="px-3 py-2">{p.reorder_point ?? "—"}</td>
                <td className="whitespace-nowrap px-3 py-2">
                  {p.cost != null ? money(p.cost) : "—"}
                  {p.pieces.length > 1 && <div className="text-xs text-gray-500">all pieces</div>}
                </td>
                <td className="whitespace-nowrap px-3 py-2">
                  {p.open_pos.length > 0 ? <Badge tone="blue">PO {p.open_pos.join(", ")}</Badge> : "—"}
                </td>
              </tr>
            ))}
            {loaded && data.items.length === 0 && (
              <tr>
                <td colSpan={10} className="px-3 py-6 text-center text-gray-500">
                  {q || view === "all" ? "No parts match." : view === "low" ? "Nothing is low on stock." : "Nothing is out of stock."}
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
          <div className="space-y-4 text-sm">
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
  const [poType, setPoType] = useState<"stock" | "drop_ship">("stock");
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
          po_type: poType,
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
      <div className="space-y-4 text-sm">
        {error && <Notice kind="error">{error}</Notice>}

        <div className="space-y-2">
          <div className="font-medium">1. Distributor</div>
          {dists.length === 0 ? (
            <p className="text-gray-600">
              You haven’t added a distributor yet.{" "}
              <button className="underline" onClick={goDistributors}>
                Add one on the Distributors page
              </button>{" "}
              (it opens in a new tab, so your ticked parts are kept).
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
                Add / edit / delete distributors (opens in a new tab)
              </button>
            </div>
          )}
        </div>

        <div className="space-y-1">
          <div className="font-medium">Type</div>
          <div className="flex flex-wrap gap-4 text-xs">
            <label className="flex items-center gap-1">
              <input
                type="radio"
                name="po_type"
                checked={poType === "stock"}
                onChange={() => {
                  setPoType("stock");
                  setShipTo(defaultShip);
                }}
              />{" "}
              Stock (buying inventory, ships to us)
            </label>
            <label className="flex items-center gap-1">
              <input
                type="radio"
                name="po_type"
                checked={poType === "drop_ship"}
                onChange={() => {
                  setPoType("drop_ship");
                  setShipTo("");
                }}
              />{" "}
              Drop ship (ships straight to a customer)
            </label>
          </div>
          {poType === "drop_ship" && <div className="text-xs text-gray-600">Type the customer’s name and address in Ship To below. Make one PO per customer address.</div>}
        </div>

        <div className="space-y-2">
          <div className="font-medium">2. Parts, quantities and cost</div>
          <div className="overflow-x-auto rounded border border-gray-200">
            <table className="w-full text-left">
              <thead className="bg-gray-50 text-xs uppercase text-gray-500">
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
                    <td className="px-2 py-1">
                      {l.part.pieces.length > 0 ? (
                        l.part.pieces.map((pc, n) => (
                          <div key={n} className="whitespace-nowrap">
                            {pc.part_number}
                            {pc.quantity > 1 && <span className="text-gray-500"> ×{pc.quantity}</span>}
                          </div>
                        ))
                      ) : (
                        <span className="whitespace-nowrap">{l.part.part_number ?? <span className="text-xs text-amber-700">none on file</span>}</span>
                      )}
                    </td>
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
          {lines.some((l) => l.part.pieces.length > 0) && (
            <p className="text-xs text-gray-500">
              A part made of distributor parts goes on the PO one line per piece, at that distributor’s current cost. Pieces bought
              from a different distributor are left off and reported after the PO is made.
            </p>
          )}
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block font-medium text-gray-700">
            <span className="mb-1 block">Ship To</span>
            <textarea className={inp} rows={4} value={shipTo} placeholder={poType === "drop_ship" ? "Customer name and address" : "Type a ship-to address"} onChange={(e) => setShipTo(e.target.value)} />
            <span className="mt-1 flex gap-3 text-xs font-normal">
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
          <span className="text-xs text-gray-500">Part numbers and cost come from each product’s distributor parts.</span>
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
        `Cancel purchase order ${po.po_number}? It stays in the history, marked cancelled (you can delete it afterwards). Stock counts are not changed, and parts that are on no other open PO stop being Backordered.`
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

  async function deletePo(po: { id: number; po_number: number }) {
    if (
      !window.confirm(
        `Permanently delete purchase order ${po.po_number}? Its lines, receiving history and shortage records are removed and this can't be undone. Stock counts are not changed. Its number becomes free, and the next new PO will use the lowest free number.`
      )
    )
      return;
    try {
      await api(`/purchase-orders/${po.id}`, { method: "DELETE" });
      setOpen(null);
      setEditing(null);
      setBump((b) => b + 1);
      setFlash({ kind: "success", text: `Purchase order ${po.po_number} deleted.` });
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
      <ClearableInput
        className={`${inp} w-full max-w-3xl`}
        value={qInput}
        onChange={(e) => setQInput(e.target.value)}
        placeholder="Search PO number, distributor, part number, SKU or description"
      />
      <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white">
        <table className="w-full text-left text-sm">
          <thead className="bg-gray-50 text-xs uppercase text-gray-500">
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
              <ClickRow key={r.id} onOpen={() => view(r.id)}>
                <td className="px-3 py-2 font-semibold">
                  {r.po_number}
                  {r.po_type === "drop_ship" && <span className="ml-2 font-normal"><Badge tone="blue">drop ship</Badge></span>}
                </td>
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
                    {r.status !== "open" && <button className={dangerCls} onClick={() => deletePo(r)}>Delete</button>}
                  </div>
                </td>
              </ClickRow>
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
          <div className="space-y-3 text-sm">
            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <div className="text-xs font-semibold uppercase text-gray-500">Vendor</div>
                <div className="font-medium">{open.vendor_name}</div>
                <div className="whitespace-pre-line text-gray-600">{open.vendor_address}</div>
              </div>
              <div>
                <div className="text-xs font-semibold uppercase text-gray-500">Ship to</div>
                <div className="whitespace-pre-line text-gray-600">{open.ship_to}</div>
                {open.po_type === "drop_ship" && <div className="mt-1"><Badge tone="blue">drop ship</Badge></div>}
                {open.supplier_order_number && <div className="mt-1 text-xs text-gray-600">Distributor order {open.supplier_order_number}</div>}
              </div>
            </div>
            <div className="text-gray-600">
              Dated {new Date(open.date + "T12:00:00").toLocaleDateString()} · made by {who(open.created_by_name, open.created_by)}
              {open.note ? ` · ${open.note}` : ""}
            </div>
            {open.status === "cancelled" && (
              <Notice kind="error">
                Cancelled{open.cancelled_at ? ` ${new Date(open.cancelled_at).toLocaleDateString()}` : ""}
                {open.cancelled_by_name || open.cancelled_by ? ` by ${who(open.cancelled_by_name, open.cancelled_by)}` : ""}. It is kept for the record until you delete it.
              </Notice>
            )}
            <div className="overflow-x-auto rounded border border-gray-200">
              <table className="w-full text-left">
                <thead className="bg-gray-50 text-xs uppercase text-gray-500">
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
            <div className="text-right text-sm font-semibold">Total {money(open.total)}</div>
            <div className="flex flex-wrap items-center gap-2">
              {open.sent_at ? <Badge tone="green">sent {new Date(open.sent_at).toLocaleDateString()}</Badge> : <Badge tone="amber">not sent</Badge>}
              {open.status === "received" && <Badge tone="green">received in</Badge>}
              {open.status !== "cancelled" && (
                <button className={btn2Cls} onClick={() => markSent(open, !open.sent_at)}>
                  {open.sent_at ? "Mark as not sent" : "Mark as sent"}
                </button>
              )}
              {open.status === "open" && !open.sent_at && (
                <span className="text-xs text-gray-500">Mark it sent to make it appear on Inventory › Receive.</span>
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
              {open.status !== "open" && <button className={dangerCls} onClick={() => deletePo(open)}>Delete PO</button>}
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
      <p className="text-sm text-gray-600">
        Every purchase order that had a problem when it was received: <strong>short</strong> (fewer than ordered and the
        rest isn’t coming) or <strong>over</strong> (more than ordered, or a part that wasn’t on the PO). A short line whose
        rest is still expected stays on its open PO and isn’t listed here. Each one shows who acknowledged it and when.
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <ClearableInput
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
                <span className="text-xs text-gray-500">{new Date(po.created_at).toLocaleDateString()}</span>
                {po.status === "received" && <Badge tone="green">received</Badge>}
                {po.status === "open" && <Badge tone="amber">still receiving</Badge>}
                {po.status === "cancelled" && <Badge tone="gray">cancelled</Badge>}
                <Badge tone="amber">{po.issue_count} issue{po.issue_count === 1 ? "" : "s"}</Badge>
              </div>
              <button className={btn2Cls} onClick={() => view(po.id)}>View PO</button>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs uppercase text-gray-500">
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
                        <div className="text-xs text-gray-500">{[i.item, i.sku].filter(Boolean).join(" · ")}</div>
                      </td>
                      <td className="px-3 py-1">{i.expected_qty}</td>
                      <td className="px-3 py-1">{i.received_qty}</td>
                      <td className={`px-3 py-1 font-semibold ${i.difference < 0 ? "text-red-700" : "text-blue-700"}`}>
                        {i.difference > 0 ? `+${i.difference}` : i.difference}
                      </td>
                      <td className="px-3 py-1">
                        <div>{who(i.acknowledged_by_name, i.acknowledged_by)}</div>
                        <div className="text-xs text-gray-500">{new Date(i.acknowledged_at).toLocaleString()}</div>
                        {i.note && <div className="text-xs text-gray-600">{i.note}</div>}
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
          <div className="space-y-3 text-sm">
            <div className="text-gray-600">
              {open.vendor_name} · dated {new Date(open.date + "T12:00:00").toLocaleDateString()} · made by {who(open.created_by_name, open.created_by)}
            </div>
            <div className="overflow-x-auto rounded border border-gray-200">
              <table className="w-full text-left">
                <thead className="bg-gray-50 text-xs uppercase text-gray-500">
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
      <div className="space-y-4 text-sm">
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
                <button type="button" className="whitespace-nowrap text-sm underline" onClick={() => setNumber(String(defaults.next_po_number))}>
                  Use the next unused ({defaults.next_po_number})
                </button>
              )}
            </div>
            {numberChanged && <span className="mt-1 block text-sm font-normal text-amber-700">Was {po.po_number}. It must not be used by another PO.</span>}
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
                {lines.map((l, i) => (
                  <tr key={l.product_id}>
                    <td className="whitespace-nowrap px-2 py-1">{l.item ?? <span className="text-sm text-amber-700">none on file</span>}</td>
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
                    <td className="whitespace-nowrap px-2 py-1 text-sm">received (locked)</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="text-right font-semibold">Total {money(total)}</div>
          <p className="text-sm text-gray-500">
            Cost is what you pay the distributor. A part added here starts from its distributor part cost (the product’s own cost if it has none) and is never shown to customers.
          </p>
        </div>

        <div className="space-y-2">
          <div className="font-medium">Add a part</div>
          <ClearableInput className={`${inp} max-w-md`} value={qInput} onChange={(e) => setQInput(e.target.value)} placeholder="Search title, SKU or part number" />
          {found.length > 0 && (
            <ul className="divide-y divide-gray-100 rounded border border-gray-200">
              {found.map((p) => (
                <li key={p.id} className="flex items-center justify-between gap-3 px-2 py-1">
                  <span>
                    {p.title} <span className="text-sm text-gray-500">{p.sku}{p.part_number ? ` · ${p.part_number}` : ""}</span>
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
            <span className="mt-1 flex gap-3 text-sm font-normal">
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
