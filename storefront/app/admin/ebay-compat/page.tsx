"use client";

import { useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { loadSaved, saveSaved } from "../../../lib/savedState";
import { Badge, Notice, btnCls, btn2Cls, inputCls } from "../../../components/admin/ui";

/* ------------------------------------------------------------------ types */

type Job = {
  id: number;
  store: string;
  status: "running" | "stopping" | "done" | "stopped" | "paused" | "failed";
  running: boolean;
  listings_found: number;
  already_saved: number;
  items_total: number;
  items_done: number;
  with_compat: number;
  compat_rows: number;
  no_list: number;
  count_only: number;
  not_found: number;
  failed: number;
  mode: "new_only" | "recheck";
  rechecked: number;
  changed: number;
  new_rows: number;
  ebay_calls: number;
  message: string | null;
  failures: string[];
  started_at: string;
  finished_at: string | null;
};
type Saved = { store: string; status: string; n: number };
type StatusResp = { job: Job | null; saved: Saved[] };

const STATUS_TEXT: Record<Job["status"], string> = {
  running: "Running",
  stopping: "Stopping…",
  done: "Finished",
  stopped: "Stopped",
  paused: "Paused",
  failed: "Failed",
};
const STATUS_TONE: Record<Job["status"], "green" | "gray" | "amber"> = {
  running: "amber",
  stopping: "amber",
  done: "green",
  stopped: "gray",
  paused: "amber",
  failed: "amber",
};
const SAVED_NAME: Record<string, string> = {
  ok: "With a compatibility list",
  count_only: "eBay gave a count only",
  no_list: "No compatibility on the listing",
  not_found: "Ended / not accessible",
};

/* ------------------------------------------------------------------- page */

export default function EbayCompatImportPage() {
  const [store, setStore] = useState("");

  // Remember the store name when you leave this page and come back (this browser tab only).
  const [storeRestored, setStoreRestored] = useState(false);
  useEffect(() => {
    const saved = loadSaved<{ store: string }>("ebay-compat");
    if (saved) setStore(saved.store ?? "");
    setStoreRestored(true);
  }, []);
  useEffect(() => {
    if (storeRestored) saveSaved("ebay-compat", { store });
  }, [storeRestored, store]);
  const [job, setJob] = useState<Job | null>(null);
  const [saved, setSaved] = useState<Saved[]>([]);
  const [error, setError] = useState("");
  const [starting, setStarting] = useState(false);

  async function refresh() {
    try {
      const r = await api<StatusResp>("/ebay-compat/status");
      setJob(r.job);
      setSaved(r.saved);
    } catch (e) {
      setError(errMsg(e));
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  // While a run is going, ask for its progress every few seconds.
  const running = !!job?.running;
  // "stopping" still counts as busy: the conversion is hidden until the import has fully stopped
  const importBusy = running || job?.status === "running" || job?.status === "stopping";
  useEffect(() => {
    if (!importBusy) return;
    const t = setInterval(() => void refresh(), 3000);
    return () => clearInterval(t);
  }, [importBusy]);

  async function start(recheck: boolean) {
    setStarting(true);
    setError("");
    try {
      const j = await api<Job>("/ebay-compat/start", { method: "POST", body: { store: store.trim(), recheck } });
      setJob(j);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setStarting(false);
    }
  }

  async function stop() {
    setError("");
    try {
      const r = await api<StatusResp>("/ebay-compat/stop", { method: "POST", body: {} });
      setJob(r.job);
      setSaved(r.saved);
    } catch (e) {
      setError(errMsg(e));
    }
  }

  const pct = job && job.items_total > 0 ? Math.min(100, Math.round((job.items_done / job.items_total) * 100)) : 0;
  const stores = Array.from(new Set(saved.map((s) => s.store)));

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-xl font-semibold">eBay compatibility import</h2>
        <p className="mt-1 max-w-3xl text-sm text-gray-600">
          Reads each active listing&rsquo;s &ldquo;compatible vehicles&rdquo; table from eBay and saves it by eBay item
          number, together with the make, model and years read from the listing title. It works for listings that are
          not products yet. Any item number that already has compatibility data saved is skipped, so only new listings (and
          ones that failed last time) are looked up. The store must be approved on the Live listings tab first.
        </p>
      </div>

      {error && <Notice kind="error">{error}</Notice>}

      <div className="space-y-4 rounded-lg border border-gray-200 bg-white p-4">
        <label className="block max-w-sm text-sm">
          <span className="mb-1 block font-medium">eBay store name</span>
          <input
            className={inputCls}
            value={store}
            onChange={(e) => setStore(e.target.value)}
            placeholder="eandimotorsports"
            disabled={running}
          />
          <span className="mt-1 block text-xs text-gray-500">Leave blank for eandimotorsports. Type genuine-oem-parts for the other store.</span>
        </label>
        <div className="flex flex-wrap gap-3">
          <button type="button" className={btnCls} disabled={running || starting} onClick={() => void start(false)}>
            Pull compatibility data for listings not saved yet
          </button>
          <button type="button" className={btnCls} disabled={running || starting} onClick={() => void start(true)}>
            Also re-check saved listings for new compatibility data
          </button>
        </div>
        <p className="max-w-3xl text-xs text-gray-500">
          The second button does the first, then re-reads listings that already have compatibility data and adds any vehicle
          eBay lists now that we did not have before (old rows are kept). It uses one eBay lookup per saved listing, so
          a big store takes a few days of the daily limit; listings checked in the last 7 days are skipped, so you can
          press it again each day until it reports nothing left.
        </p>
        <p className="max-w-3xl text-xs text-gray-500">
          eBay allows about 5,000 of these lookups a day. If the limit is reached the import pauses; start it again
          later and it carries on where it stopped.
        </p>
      </div>

      {job && (
        <div className="space-y-3 rounded-lg border border-gray-200 bg-white p-4">
          <div className="flex flex-wrap items-center gap-3">
            <Badge tone={STATUS_TONE[job.status]}>{STATUS_TEXT[job.status]}</Badge>
            <span className="text-sm font-medium">{job.store}</span>
            <span className="text-sm text-gray-500">{job.mode === "recheck" ? "With re-check of saved listings" : "New listings only"}</span>
            <span className="text-xs text-gray-400">started {new Date(job.started_at).toLocaleString()}</span>
            {job.running && (
              <button type="button" className={btn2Cls} onClick={() => void stop()} disabled={job.status === "stopping"}>
                {job.status === "stopping" ? "Stopping…" : "Stop"}
              </button>
            )}
          </div>

          {job.items_total > 0 && (
            <div>
              <div className="h-2 w-full overflow-hidden rounded bg-gray-100">
                <div className="h-2 bg-gray-700" style={{ width: `${pct}%` }} />
              </div>
              <div className="mt-1 text-xs text-gray-500">
                {job.items_done.toLocaleString()} of {job.items_total.toLocaleString()} looked up ({pct}%)
              </div>
            </div>
          )}

          {job.message && <p className="text-sm text-gray-700">{job.message}</p>}

          <dl className="grid max-w-3xl grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-3">
            <div><dt className="text-xs text-gray-500">Active listings on eBay</dt><dd>{job.listings_found.toLocaleString()}</dd></div>
            <div><dt className="text-xs text-gray-500">Skipped (already saved)</dt><dd>{job.already_saved.toLocaleString()}</dd></div>
            <div><dt className="text-xs text-gray-500">With a compatibility list</dt><dd>{job.with_compat.toLocaleString()}</dd></div>
            <div><dt className="text-xs text-gray-500">Vehicles received</dt><dd>{job.compat_rows.toLocaleString()}</dd></div>
            <div><dt className="text-xs text-gray-500">No compatibility on listing</dt><dd>{job.no_list.toLocaleString()}</dd></div>
            <div><dt className="text-xs text-gray-500">Count only / not accessible</dt><dd>{job.count_only.toLocaleString()} / {job.not_found.toLocaleString()}</dd></div>
            {job.mode === "recheck" && (
              <>
                <div><dt className="text-xs text-gray-500">Saved listings re-checked</dt><dd>{job.rechecked.toLocaleString()}</dd></div>
                <div><dt className="text-xs text-gray-500">Had new vehicles</dt><dd>{job.changed.toLocaleString()}</dd></div>
                <div><dt className="text-xs text-gray-500">New vehicles added</dt><dd>{job.new_rows.toLocaleString()}</dd></div>
              </>
            )}
            <div><dt className="text-xs text-gray-500">Failed</dt><dd>{job.failed.toLocaleString()}</dd></div>
            <div><dt className="text-xs text-gray-500">eBay lookups</dt><dd>{job.ebay_calls.toLocaleString()}</dd></div>
          </dl>

          {job.failures.length > 0 && (
            <details className="text-sm">
              <summary className="cursor-pointer text-gray-700">Show failures ({job.failures.length})</summary>
              <ul className="mt-2 max-h-56 list-disc space-y-1 overflow-y-auto pl-5 text-xs text-gray-600">
                {job.failures.map((f, i) => (
                  <li key={i}>{f}</li>
                ))}
              </ul>
              <p className="mt-2 text-xs text-gray-500">Failed listings are not saved, so the next run tries them again.</p>
            </details>
          )}
        </div>
      )}

      {!importBusy && <FitmentCard />}

      {stores.length > 0 && (
        <div className="space-y-3 rounded-lg border border-gray-200 bg-white p-4">
          <h3 className="text-base font-semibold">Saved so far</h3>
          <div className="grid gap-4 sm:grid-cols-2">
            {stores.map((s) => (
              <div key={s}>
                <div className="mb-1 text-sm font-medium">{s}</div>
                <table className="w-full text-left text-sm">
                  <tbody className="divide-y divide-gray-100">
                    {saved
                      .filter((x) => x.store === s)
                      .map((x) => (
                        <tr key={x.status}>
                          <td className="py-1 pr-3 text-gray-600">{SAVED_NAME[x.status] ?? x.status}</td>
                          <td className="py-1 text-right">{x.n.toLocaleString()}</td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}


/* ------------------------------------------------- convert compat to fitment */

type SuggestionT = { model_id: number; name: string; make: string; score: number };
type OpenRow = {
  make_key: string;
  model_key: string;
  make: string;
  model: string;
  products: number;
  rows: number;
  make_exists: boolean;
  ambiguous: boolean;
  suggestion: SuggestionT | null;
};
type Added = Record<string, { rows: number; parts: number }>;
type Plan = {
  include_existing: boolean;
  raw_copied: number;
  raw_updated: number;
  parts_in_scope: number;
  parts_ebay: number;
  parts_title: number;
  parts_nothing: number;
  open_total: number;
  open_parts: number;
  skipped_rows: number;
  open: OpenRow[];
  years_created: number;
  years_created_sample: { model: string; years: number[]; count: number }[];
  links_total: number;
  added: Added;
  added_rows: number;
  added_parts: number;
  already_there: number;
};
type VType = { id: number; name: string };
type ModelHit = { id: number; name: string; make: string };
type MakeHit = { id: number; name: string };

const SRC_NAME: Record<string, string> = {
  ebay: "From eBay compatibility",
  title: "From the listing title",
  title_inferred: "Title with no year (possible fit)",
};

function FitmentCard() {
  const [includeExisting, setIncludeExisting] = useState(false);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [done, setDone] = useState<Plan | null>(null);
  const [busy, setBusy] = useState<"" | "preview" | "merge" | "decide">("");
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");
  const [stale, setStale] = useState(false);
  const [types, setTypes] = useState<VType[]>([]);
  // "Create new" asks for the type right then: createRow is the row being created, createType what was chosen
  // ("" = not chosen yet, "none" = no type on purpose). The bulk create has its own make + type choice.
  const [createRow, setCreateRow] = useState<string | null>(null);
  const [createType, setCreateType] = useState("");
  const [bulkOpen, setBulkOpen] = useState(false);
  const [sortKey, setSortKey] = useState<"" | "name" | "parts" | "guess">("");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  const [sel, setSel] = useState<string[]>([]); // ticked rows for the multi-create panel
  const [selFilter, setSelFilter] = useState("");
  const [tickMake, setTickMake] = useState("");
  const [tickMsg, setTickMsg] = useState("");
  const [bulkType, setBulkType] = useState("");
  const [pick, setPick] = useState<string | null>(null);
  const [pickQ, setPickQ] = useState("");
  const [hits, setHits] = useState<ModelHit[]>([]);
  const [makes, setMakes] = useState<MakeHit[]>([]);
  const [pickMake, setPickMake] = useState("");
  const [makeModels, setMakeModels] = useState<ModelHit[]>([]);
  const [chosen, setChosen] = useState<ModelHit | null>(null);
  const searchSeq = useRef(0);

  useEffect(() => {
    api<VType[]>("/vehicle-types")
      .then((t) => setTypes(Array.isArray(t) ? t : []))
      .catch(() => {});
  }, []);

  const rowKey = (r: OpenRow) => `${r.make_key}|${r.model_key}`;

  async function preview() {
    setBusy("preview");
    setErr("");
    setNote("");
    setDone(null);
    try {
      setPlan(await api<Plan>(`/ebay-compat/fitment/preview?include_existing=${includeExisting}`));
      setStale(false);
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy("");
    }
  }

  async function merge() {
    if (!plan) return;
    const ok = window.confirm(
      `Add ${plan.added_rows.toLocaleString()} fitment rows to ${plan.added_parts.toLocaleString()} parts` +
        (plan.years_created ? ` and ${plan.years_created.toLocaleString()} new model-years` : "") +
        `?\n\nNothing is deleted. Machines that are not in the system yet are left out until you settle them.`
    );
    if (!ok) return;
    setBusy("merge");
    setErr("");
    setNote("");
    try {
      setDone(await api<Plan>("/ebay-compat/fitment/merge", { method: "POST", body: { include_existing: includeExisting } }));
      setPlan(null);
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy("");
    }
  }

  function decision(r: OpenRow, action: "map" | "create" | "skip", modelId?: number, typeId?: number | null) {
    return {
      make_key: r.make_key,
      model_key: r.model_key,
      make: r.make,
      model: r.model,
      action,
      model_id: modelId ?? null,
      type_id: action === "create" ? typeId ?? null : null,
    };
  }

  async function decide(r: OpenRow, action: "map" | "create" | "skip", modelId?: number, typeId?: number | null) {
    setBusy("decide");
    setErr("");
    try {
      await api("/ebay-compat/fitment/decide", { method: "POST", body: { decisions: [decision(r, action, modelId, typeId)] } });
      setPlan((p) => (p ? { ...p, open: p.open.filter((x) => rowKey(x) !== rowKey(r)), open_total: Math.max(0, p.open_total - 1) } : p));
      setPick(null);
      setCreateRow(null);
      setStale(true);
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy("");
    }
  }

  async function bulkAccept() {
    if (!window.confirm("Use the suggested model for every machine whose suggestion scores 0.95 or more (eBay's own submodel equals your model)?")) return;
    setBusy("decide");
    setErr("");
    try {
      const r = await api<{ mapped: number; created: number; skipped: number; left: number }>("/ebay-compat/fitment/decide-bulk", {
        method: "POST",
        body: { mode: "accept_suggestions", include_existing: includeExisting, min_score: 0.95 },
      });
      setNote(`Saved: ${r.mapped} mapped. ${r.left} left for you. Press Preview again for the new counts.`);
      setPlan(null);
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy("");
    }
  }

  // Create the ticked machines as new models, all with the one type chosen in the panel.
  async function bulkCreate() {
    if (!plan || sel.length === 0 || !bulkType) return;
    const rows = plan.open.filter((r) => sel.includes(rowKey(r)) && !r.ambiguous);
    if (rows.length === 0) return;
    const typeName = bulkType === "none" ? "no vehicle type" : types.find((t) => String(t.id) === bulkType)?.name ?? "the chosen type";
    const all = rows.length >= plan.open.filter((r) => !r.ambiguous).length;
    const msg =
      `Create ${rows.length} machine${rows.length === 1 ? "" : "s"} as new models, all with type: ${typeName}, using eBay's spelling?` +
      (all
        ? `\n\nThis is EVERY machine left in the list, so they will all get the same type. If that is not what you intend, cancel, tick just one group (a make, or the rows of one kind), and repeat with the other types.`
        : "") +
      `\n\nCheck the names first: a misspelling would become its own model.`;
    if (!window.confirm(msg)) return;
    setBusy("decide");
    setErr("");
    try {
      const typeId = bulkType === "none" ? null : Number(bulkType);
      const r = await api<{ mapped: number; created: number; skipped: number }>("/ebay-compat/fitment/decide", {
        method: "POST",
        body: { decisions: rows.map((x) => decision(x, "create", undefined, typeId)) },
      });
      const done = new Set(rows.map(rowKey));
      setPlan((p) => (p ? { ...p, open: p.open.filter((x) => !done.has(rowKey(x))), open_total: Math.max(0, p.open_total - rows.length) } : p));
      setSel([]);
      setBulkType("");
      setNote(`Created ${r.created} new model${r.created === 1 ? "" : "s"}. Pick the next group and type, or press Preview for the new counts.`);
      setStale(true);
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy("");
    }
  }

  // Instant search: every keystroke asks for the models whose make or name matches, newest answer wins.
  async function search(q: string) {
    setPickQ(q);
    const seq = ++searchSeq.current;
    if (q.trim().length < 1) {
      setHits([]);
      return;
    }
    try {
      const mk = pickMake ? `&make_id=${pickMake}` : "";
      const r = await api<{ items: ModelHit[] }>(`/ebay-compat/fitment/models?q=${encodeURIComponent(q.trim())}${mk}&limit=25`);
      if (seq === searchSeq.current) setHits(r.items ?? []);
    } catch {
      if (seq === searchSeq.current) setHits([]);
    }
  }

  const norm = (v: string) => v.toLowerCase().replace(/[^a-z0-9]/g, "");

  // Opens the picker for a row; the make is chosen for you when the eBay make is already in the system.
  async function openPick(r: OpenRow) {
    const key = rowKey(r);
    const closing = pick === key;
    setPick(closing ? null : key);
    setPickQ("");
    setHits([]);
    setChosen(null);
    setPickMake("");
    setMakeModels([]);
    setCreateRow(null);
    if (closing) return;
    let list = makes;
    if (list.length === 0) {
      try {
        const res = await api<{ items: MakeHit[] }>("/ebay-compat/fitment/makes");
        list = res.items ?? [];
        setMakes(list);
      } catch {
        return;
      }
    }
    const m = list.find((x) => norm(x.name) === norm(r.make));
    if (m) void chooseMake(String(m.id));
  }

  async function chooseMake(id: string) {
    setPickMake(id);
    setMakeModels([]);
    setChosen(null);
    setPickQ("");
    setHits([]);
    searchSeq.current++;
    if (!id) return;
    try {
      const r = await api<{ items: ModelHit[] }>(`/ebay-compat/fitment/models?make_id=${id}&limit=500`);
      setMakeModels(r.items ?? []);
    } catch {
      setMakeModels([]);
    }
  }

  const off = busy !== "";
  const filteredRows = (plan?.open ?? []).filter((r) => {
    const f = bulkOpen ? selFilter.trim().toLowerCase() : "";
    return !f || `${r.make} ${r.model}`.toLowerCase().includes(f);
  });
  // Click a heading: ascending, again: descending, a third click goes back to the default order (most parts first).
  function clickSort(key: "name" | "parts" | "guess") {
    if (sortKey !== key) {
      setSortKey(key);
      setSortDir("asc");
    } else if (sortDir === "asc") {
      setSortDir("desc");
    } else {
      setSortKey("");
      setSortDir("asc");
    }
  }
  const shownRows = (() => {
    if (!sortKey) return filteredRows;
    const sign = sortDir === "asc" ? 1 : -1;
    const idx = new Map(filteredRows.map((r, i) => [r, i] as const));
    return [...filteredRows].sort((a, b) => {
      let c = 0;
      if (sortKey === "name") c = `${a.make} ${a.model}`.localeCompare(`${b.make} ${b.model}`, undefined, { sensitivity: "base", numeric: true });
      else if (sortKey === "parts") c = a.products - b.products;
      else {
        // Rows with no suggestion always sit at the bottom, whichever way it is sorted.
        if (!a.suggestion && !b.suggestion) return (idx.get(a) ?? 0) - (idx.get(b) ?? 0);
        if (!a.suggestion) return 1;
        if (!b.suggestion) return -1;
        c = a.suggestion.score - b.suggestion.score;
      }
      return c !== 0 ? c * sign : (idx.get(a) ?? 0) - (idx.get(b) ?? 0);
    });
  })();
  const sortTh = (key: "name" | "parts" | "guess", label: string, right = false) => (
    <th className="px-2 py-1" aria-sort={sortKey === key ? (sortDir === "asc" ? "ascending" : "descending") : "none"}>
      <button
        type="button"
        className={`inline-flex items-center gap-1 hover:text-gray-900 ${right ? "float-right" : "text-left"} ${sortKey === key ? "text-gray-900" : ""}`}
        onClick={() => clickSort(key)}
        title={`Sort by ${label.toLowerCase()}`}
      >
        <span>{label}</span>
        <span className={`shrink-0 text-[10px] ${sortKey === key ? "" : "text-gray-300"}`}>{sortKey === key ? (sortDir === "asc" ? "▲" : "▼") : "▲▼"}</span>
      </button>
    </th>
  );
  const bulkMakes = (() => {
    const m = new Map<string, { make_key: string; make: string; n: number }>();
    for (const r of plan?.open ?? []) {
      if (r.ambiguous) continue;
      const e = m.get(r.make_key) ?? { make_key: r.make_key, make: r.make, n: 0 };
      e.n += 1;
      m.set(r.make_key, e);
    }
    return Array.from(m.values()).sort((a, b) => a.make.localeCompare(b.make));
  })();

  return (
    <div className="space-y-4 rounded-lg border border-gray-200 bg-white p-4">
      <div>
        <h3 className="text-base font-semibold">Convert compatibility to fitment</h3>
        <p className="mt-1 max-w-3xl text-sm text-gray-600">
          Links each part that is tied to an eBay listing to its machines in the year-make-model system, using what is
          saved above: the eBay compatibility table first, and for parts eBay gave no table for, the make, model and years
          read from the listing title. Machines that are not in the system yet are listed below so you can map or add
          them; nothing is guessed. Years a known model does not have yet are added. It only adds, never deletes. Preview
          shows the real result first and changes nothing.
        </p>
      </div>

      <label className="flex max-w-3xl items-start gap-2 text-sm">
        <input type="checkbox" className="mt-1" checked={includeExisting} onChange={(e) => setIncludeExisting(e.target.checked)} disabled={off} />
        <span>
          Also top up parts that already have fitment
          <span className="block text-xs text-gray-500">
            Off (recommended): only parts with no fitment yet, so a link you removed by hand is not put back. On: every
            linked part is checked and anything missing is added.
          </span>
        </span>
      </label>

      <div className="flex flex-wrap gap-3">
        <button type="button" className={btn2Cls} disabled={off} onClick={() => void preview()}>
          {busy === "preview" ? "Working it out…" : "Preview"}
        </button>
        <button type="button" className={btnCls} disabled={off || !plan || stale || plan.added_rows === 0} onClick={() => void merge()}>
          {busy === "merge" ? "Converting…" : "Convert to fitment"}
        </button>
      </div>
      {err && <Notice kind="error">{err}</Notice>}
      {note && <Notice kind="info">{note}</Notice>}
      {stale && plan && <Notice kind="info">Decisions saved. Press Preview again to refresh the counts, then convert.</Notice>}

      {done && (
        <Notice kind="success">
          Done. Added {done.added_rows.toLocaleString()} fitment rows to {done.added_parts.toLocaleString()} parts
          {Object.entries(done.added).map(([k, v]) => ` · ${SRC_NAME[k] ?? k}: ${v.rows.toLocaleString()}`).join("")}.{" "}
          {done.years_created > 0 && `${done.years_created.toLocaleString()} new model-years added. `}
          {done.open_total > 0 ? `${done.open_total.toLocaleString()} machine names still need adding or mapping (press Preview to see them).` : ""}
        </Notice>
      )}

      {plan && (
        <div className="space-y-4">
          <dl className="grid max-w-3xl grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-3">
            <div><dt className="text-xs text-gray-500">Parts checked</dt><dd>{plan.parts_in_scope.toLocaleString()}</dd></div>
            <div><dt className="text-xs text-gray-500">Using eBay compatibility</dt><dd>{plan.parts_ebay.toLocaleString()}</dd></div>
            <div><dt className="text-xs text-gray-500">Using the title instead</dt><dd>{plan.parts_title.toLocaleString()}</dd></div>
            <div><dt className="text-xs text-gray-500">Fitment rows to add</dt><dd className="font-semibold">{plan.added_rows.toLocaleString()}</dd></div>
            <div><dt className="text-xs text-gray-500">Parts that gain fitment</dt><dd className="font-semibold">{plan.added_parts.toLocaleString()}</dd></div>
            <div><dt className="text-xs text-gray-500">Already linked</dt><dd>{plan.already_there.toLocaleString()}</dd></div>
            <div><dt className="text-xs text-gray-500">New model-years to add</dt><dd>{plan.years_created.toLocaleString()}</dd></div>
            <div><dt className="text-xs text-gray-500">Machines not in the system</dt><dd>{plan.open_total.toLocaleString()} ({plan.open_parts.toLocaleString()} parts)</dd></div>
            <div><dt className="text-xs text-gray-500">Nothing usable (no compat, no title)</dt><dd>{plan.parts_nothing.toLocaleString()}</dd></div>
          </dl>
          {Object.keys(plan.added).length > 0 && (
            <p className="text-xs text-gray-600">
              {Object.entries(plan.added).map(([k, v]) => `${SRC_NAME[k] ?? k}: ${v.rows.toLocaleString()} rows on ${v.parts.toLocaleString()} parts`).join(" · ")}
            </p>
          )}
          {plan.raw_copied + plan.raw_updated > 0 && (
            <p className="text-xs text-gray-500">
              The raw compatibility table will also be brought up to date ({plan.raw_copied.toLocaleString()} added,{" "}
              {plan.raw_updated.toLocaleString()} refreshed) so the SQL stages see these parts.
            </p>
          )}
          {plan.years_created_sample.length > 0 && (
            <details className="text-sm">
              <summary className="cursor-pointer text-gray-700">New years that would be added to existing models</summary>
              <ul className="mt-2 max-h-48 list-disc space-y-1 overflow-y-auto pl-5 text-xs text-gray-600">
                {plan.years_created_sample.map((y, i) => (
                  <li key={i}>
                    {y.model}: {y.years.join(", ")}
                    {y.count > y.years.length ? ` … (${y.count} in all)` : ""}
                  </li>
                ))}
              </ul>
            </details>
          )}

          {plan.open_total > 0 && (
            <div className="space-y-3">
              <div>
                <h4 className="text-sm font-semibold">Machines not in the system yet ({plan.open_total.toLocaleString()})</h4>
                <p className="max-w-3xl text-xs text-gray-500">
                  For each one: use the suggested model, pick the right existing model, create it as a new model, or skip it
                  (parts keep no link for it). Your choice is saved in the same model map the SQL stages use, so it also holds
                  next time. Parts using these machines are left out of the conversion until you settle them.
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-3 text-sm">
                <button type="button" className={btn2Cls} disabled={off} onClick={() => void bulkAccept()}>
                  Use all suggestions scoring 0.95+
                </button>
                <button type="button" className={btn2Cls} disabled={off} onClick={() => setBulkOpen((v) => !v)}>
                  Create several as new models…
                </button>
              </div>
              {bulkOpen && (
                <div className="max-w-3xl space-y-2 rounded border border-gray-200 bg-gray-50 p-3 text-sm">
                  <p className="text-xs text-gray-600">
                    Tick the machines to create, choose their type, and press Create. Models of different types are done in
                    separate rounds: tick one group, create it, then tick the next group and pick its type.
                  </p>
                  <div className="flex flex-wrap items-center gap-2">
                    <input className={inputCls} placeholder="Filter the list, e.g. Yamaha WaveRunner" value={selFilter} onChange={(e) => setSelFilter(e.target.value)} />
                    <button type="button" className={btn2Cls} disabled={off} onClick={() => setSel(Array.from(new Set([...sel, ...shownRows.filter((r) => !r.ambiguous).map(rowKey)])))}>
                      Tick all shown ({shownRows.filter((r) => !r.ambiguous).length})
                    </button>
                    <select className={inputCls} value={tickMake} disabled={off} onChange={(e) => { setTickMake(e.target.value); setTickMsg(""); }}>
                      <option value="">Choose a make…</option>
                      {bulkMakes.map((m) => (
                        <option key={m.make_key} value={m.make_key}>{m.make} ({m.n})</option>
                      ))}
                    </select>
                    <button
                      type="button"
                      className={btn2Cls}
                      disabled={off || !tickMake}
                      onClick={() => {
                        const keys = (plan?.open ?? []).filter((r) => r.make_key === tickMake && !r.ambiguous).map(rowKey);
                        setSel((cur) => Array.from(new Set([...cur, ...keys])));
                        setTickMsg(`Ticked ${keys.length} ${bulkMakes.find((m) => m.make_key === tickMake)?.make ?? ""} machine${keys.length === 1 ? "" : "s"}.`);
                      }}
                    >
                      Tick this make
                    </button>
                    <button type="button" className={btn2Cls} disabled={off || sel.length === 0} onClick={() => setSel([])}>
                      Clear ticks
                    </button>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-xs text-gray-600">{tickMsg ? `${tickMsg} ` : ""}{sel.length} ticked in all. Type for these:</span>
                    <select className={inputCls} value={bulkType} onChange={(e) => setBulkType(e.target.value)} disabled={off}>
                      <option value="">Choose a type…</option>
                      {types.map((t) => (
                        <option key={t.id} value={t.id}>{t.name}</option>
                      ))}
                      <option value="none">No type (set later)</option>
                    </select>
                    <button type="button" className={btnCls} disabled={off || sel.length === 0 || !bulkType} onClick={() => void bulkCreate()}>
                      Create {sel.length} as new models
                    </button>
                  </div>
                </div>
              )}
              <div className="max-h-[32rem] overflow-auto rounded border border-gray-100">
                <table className="w-full text-left text-xs">
                  <thead className="sticky top-0 bg-gray-50 text-gray-500">
                    <tr>
                      {bulkOpen && <th className="px-2 py-1"></th>}
                      {sortTh("name", "eBay / title says")}
                      {sortTh("parts", "Parts", true)}
                      {sortTh("guess", "Best guess")}
                      <th className="px-2 py-1">Do this</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100">
                    {shownRows.map((r) => (
                      <tr key={rowKey(r)} className="align-top">
                        {bulkOpen && (
                          <td className="px-2 py-1">
                            <input
                              type="checkbox"
                              disabled={off || r.ambiguous}
                              checked={sel.includes(rowKey(r))}
                              onChange={(e) => setSel(e.target.checked ? [...sel, rowKey(r)] : sel.filter((k) => k !== rowKey(r)))}
                            />
                          </td>
                        )}
                        <td className="px-2 py-1">
                          <div className="font-medium">{r.make} {r.model}</div>
                          {!r.make_exists && <div className="text-amber-700">make not in the system either</div>}
                          {r.ambiguous && <div className="text-amber-700">two models have this name; pick one</div>}
                        </td>
                        <td className="px-2 py-1 text-right">{r.products.toLocaleString()}</td>
                        <td className="px-2 py-1 text-gray-600">
                          {r.suggestion ? `${r.suggestion.name} (${r.suggestion.score.toFixed(2)})` : "none"}
                        </td>
                        <td className="px-2 py-1">
                          <div className="flex flex-wrap gap-1">
                            {r.suggestion && (
                              <button type="button" className={btn2Cls} disabled={off} onClick={() => void decide(r, "map", r.suggestion!.model_id)}>
                                Use guess
                              </button>
                            )}
                            <button type="button" className={btn2Cls} disabled={off} onClick={() => void openPick(r)}>
                              Pick model
                            </button>
                            {!r.ambiguous && (
                              <button type="button" className={btn2Cls} disabled={off} onClick={() => { setCreateRow(createRow === rowKey(r) ? null : rowKey(r)); setCreateType(""); setPick(null); }}>
                                Create new
                              </button>
                            )}
                            <button type="button" className={btn2Cls} disabled={off} onClick={() => {
                                if (
                                  window.confirm(
                                    `Skip ${r.make} ${r.model}?\n\nThe ${r.products.toLocaleString()} part${r.products === 1 ? "" : "s"} that use it will NOT be linked to this machine in the fitment tool, now or on any later run. This is saved immediately and cannot be undone from this page.\n\nIf you are not sure, press Cancel and leave it in the list.`
                                  )
                                )
                                  void decide(r, "skip");
                              }}>
                              Skip
                            </button>
                          </div>
                          {createRow === rowKey(r) && (
                            <div className="mt-2 flex flex-wrap items-center gap-2">
                              <select className={inputCls} value={createType} onChange={(e) => setCreateType(e.target.value)} disabled={off}>
                                <option value="">Choose a type…</option>
                                {types.map((t) => (
                                  <option key={t.id} value={t.id}>{t.name}</option>
                                ))}
                                <option value="none">No type (set later)</option>
                              </select>
                              <button
                                type="button"
                                className={btnCls}
                                disabled={off || !createType}
                                onClick={() => void decide(r, "create", undefined, createType === "none" ? null : Number(createType))}
                              >
                                Create {r.make} {r.model}
                              </button>
                            </div>
                          )}
                          {pick === rowKey(r) && (
                            <div className="mt-2 w-80 space-y-2 rounded border border-gray-200 bg-white p-2">
                              <div className="relative">
                                <input
                                  className={inputCls}
                                  autoFocus
                                  placeholder={pickMake ? `Search ${makes.find((m) => String(m.id) === pickMake)?.name ?? "this make"} models` : "Type to search, e.g. yamaha raptor"}
                                  value={pickQ}
                                  onChange={(e) => void search(e.target.value)}
                                />
                                {pickQ.trim() !== "" && (
                                  <div className="absolute left-0 right-0 z-10 mt-1 max-h-56 overflow-y-auto rounded border border-gray-200 bg-white shadow">
                                    {hits.length === 0 && <div className="px-2 py-1 text-gray-500">No model matches.</div>}
                                    {hits.map((h) => (
                                      <button
                                        key={h.id}
                                        type="button"
                                        className="block w-full px-2 py-1 text-left hover:bg-gray-50"
                                        onClick={() => { setChosen(h); setPickQ(""); setHits([]); }}
                                      >
                                        {h.make} {h.name}
                                      </button>
                                    ))}
                                  </div>
                                )}
                              </div>
                              <div className="text-gray-500">{pickMake ? "Searching only this make. Change it here:" : "or choose:"}</div>
                              <select className={inputCls} value={pickMake} onChange={(e) => void chooseMake(e.target.value)}>
                                <option value="">All makes…</option>
                                {makes.map((m) => (
                                  <option key={m.id} value={m.id}>{m.name}</option>
                                ))}
                              </select>
                              <select
                                className={inputCls}
                                value={chosen && makeModels.some((m) => m.id === chosen.id) ? chosen.id : ""}
                                disabled={!pickMake}
                                onChange={(e) => setChosen(makeModels.find((m) => String(m.id) === e.target.value) ?? null)}
                              >
                                <option value="">{pickMake ? "Model…" : "Pick a make first"}</option>
                                {makeModels.map((m) => (
                                  <option key={m.id} value={m.id}>{m.name}</option>
                                ))}
                              </select>
                              {chosen && (
                                <div className="flex items-center justify-between gap-2 rounded bg-gray-50 px-2 py-1">
                                  <span>{chosen.make} {chosen.name}</span>
                                  <button type="button" className={btnCls} disabled={off} onClick={() => void decide(r, "map", chosen.id)}>
                                    Use this model
                                  </button>
                                </div>
                              )}
                            </div>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {plan.open_total > plan.open.length && (
                <p className="text-xs text-gray-500">Showing the {plan.open.length} with the most parts. Settle these, then Preview again for the rest.</p>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
