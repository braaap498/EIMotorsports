"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Badge, Notice, btnCls, btn2Cls, inputCls } from "../../../components/admin/ui";
import DescriptionEditor from "../../../components/admin/DescriptionEditor";
import { sanitizeHtml } from "../../../components/admin/sanitizeHtml";
import { shrink } from "../../../components/admin/PhotosPanel";

type Variation = { sku: string; price: string | null; quantity_available: number; quantity_sold: number; specifics: string };
type Item = {
  store: string;
  item_id: string;
  title: string;
  price: string | null;
  currency: string;
  quantity_available: number;
  quantity_sold: number;
  sku: string;
  listing_type: string;
  status: string;
  url: string;
  image_url: string;
  description: string;
  has_variations: boolean;
  variations: Variation[];
  item_specifics: { name: string; values: string[] }[];
  specifics_editable: boolean;
  condition: { id: string; name: string; note: string };
  compatibility: { count: number; rows: Record<string, string>[]; truncated: boolean };
  photos: { url: string }[];
  photos_editable: boolean;
  editable: boolean;
  not_editable_reason: string | null;
  ended: boolean; // not live on eBay right now
  relistable: boolean; // ended, and eBay can bring it back
  draft?: { changes: DraftChanges; updated_by: string | null; updated_at: string | null } | null; // edits saved on the website
};
// What is saved on the website for a listing (same shape as what is sent to eBay, plus a small picture for the screen).
type DraftChanges = {
  title?: string;
  description?: string;
  price?: string;
  quantity?: number;
  sku?: string;
  variations?: { sku: string; price?: string; quantity?: number }[];
  photos?: { url?: string; key?: string; thumb?: string }[];
  item_specifics?: { name: string; values: string[] }[];
};
type Diff = { field: string; before: string | null; after: string; note?: string };
type Preview = {
  calls: { call: string; xml: string }[];
  diff: Diff[];
  fingerprint: string;
  live_pushes_enabled: boolean;
};
type LogRow = {
  id: number;
  created_at: string;
  user_email: string | null;
  user_name: string | null;
  item_id: string;
  action: string;
  dry_run: boolean;
  ok: boolean;
  error: string | null;
};
type VarEdit = { price: string; qty: string };
// One picture in the list being edited: either already on the listing (url) or from the website's storage (key).
type PhotoEdit = { id: string; thumb: string; url?: string; key?: string };
type Found = { id: number; sku: string; title: string; photos: { image_key: string; thumb: string | null }[] };
const MAX_PHOTOS = 24;
type SpecEdit = { id: string; name: string; value: string }; // several values are separated by semicolons
const splitValues = (v: string) => v.split(";").map((x) => x.trim()).filter(Boolean);
const specSig = (list: { name: string; value: string }[]) =>
  JSON.stringify(list.filter((x) => x.name.trim() || x.value.trim()).map((x) => [x.name.trim(), splitValues(x.value)]));

const norm = (s: string) => s.replace(/\r\n/g, "\n").trim();
const photoId = (p: PhotoEdit) => (p.url ? p.url : "key:" + p.key);
// One string that stands for everything on the form, so "has anything changed since it was saved" is a comparison.
const sigOf = (v: { title: string; description: string; price: string; qty: string; sku: string; vars: Record<string, VarEdit>; photos: PhotoEdit[]; specs: SpecEdit[] }) =>
  JSON.stringify([v.title, norm(v.description), v.price, v.qty, v.sku, Object.keys(v.vars).sort().map((k) => [k, v.vars[k].price, v.vars[k].qty]), v.photos.map(photoId), specSig(v.specs)]);

// Does the visual editor keep this description as it is? It only keeps simple formatting (bold, lists,
// headings, links, tables); styles, images and the like would be dropped. Read in an inert document,
// so nothing in the description can load or run while we compare.
const inert = (h: string) => new DOMParser().parseFromString(h, "text/html").body.innerHTML.replace(/\s+/g, "");
const losesFormatting = (h: string) => h.trim() !== "" && inert(h) !== inert(sanitizeHtml(h));

// The visual editor can hand back its own tidied-up version of the HTML it was given (on load, on blur,
// after a re-render) without the person having typed anything. That must not count as an edit: an edit
// clears the preview, which made the preview vanish the moment it appeared.
const sameHtml = (a: string, b: string) => inert(a) === inert(b) || inert(a) === inert(sanitizeHtml(b));

// What exactly the visual editor would drop, so the warning is not a guess. Tags are counted in the
// description and in its cleaned version; attributes are counted in the description (the editor keeps
// only link addresses and table spans).
function lossDetail(h: string): string[] {
  const a = new DOMParser().parseFromString(h, "text/html").body;
  const b = new DOMParser().parseFromString(sanitizeHtml(h), "text/html").body;
  const tally = (root: HTMLElement) => {
    const m: Record<string, number> = {};
    root.querySelectorAll("*").forEach((e) => {
      const t = e.tagName.toLowerCase();
      m[t] = (m[t] ?? 0) + 1;
    });
    return m;
  };
  const ca = tally(a);
  const cb = tally(b);
  const out: string[] = [];
  const tags = Object.keys(ca)
    .filter((t) => ca[t] - (cb[t] ?? 0) > 0)
    .map((t) => `<${t}> x${ca[t] - (cb[t] ?? 0)}`);
  if (tags.length) out.push("Tags removed (their text stays): " + tags.join(", "));
  const attrs: Record<string, number> = {};
  a.querySelectorAll("*").forEach((e) =>
    Array.from(e.attributes).forEach((at) => {
      if (!["href", "colspan", "rowspan"].includes(at.name)) attrs[at.name] = (attrs[at.name] ?? 0) + 1;
    }),
  );
  const attrList = Object.entries(attrs).map(([k, n]) => `${k} x${n}`);
  if (attrList.length) out.push("Settings removed: " + attrList.join(", "));
  return out;
}

export default function EbayEditPage() {
  const [store, setStore] = useState("");
  const [item, setItem] = useState<Item | null>(null);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [price, setPrice] = useState("");
  const [qty, setQty] = useState("");
  const [sku, setSku] = useState("");
  const [vars, setVars] = useState<Record<string, VarEdit>>({});
  const [photos, setPhotos] = useState<PhotoEdit[]>([]);
  const [htmlMode, setHtmlMode] = useState(false);
  const [lossy, setLossy] = useState(false);
  const [lossInfo, setLossInfo] = useState<string[]>([]);
  const [specs, setSpecs] = useState<SpecEdit[]>([]);
  const [showLib, setShowLib] = useState(false);
  const [libQ, setLibQ] = useState("");
  const [libResults, setLibResults] = useState<Found[] | null>(null);
  const [photoMsg, setPhotoMsg] = useState("");
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [baseSig, setBaseSig] = useState(""); // the form as last saved (or as eBay has it), to tell what is unsaved
  const [log, setLog] = useState<LogRow[]>([]);
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const [differences, setDifferences] = useState<string[]>([]);
  const [busy, setBusy] = useState("");
  const started = useRef(false);

  // Show a listing on the form: what eBay has, with any edits saved on the website laid over it.
  function fill(i: Item) {
    const d = i.draft?.changes;
    const vTitle = d?.title ?? i.title;
    const vDesc = d?.description ?? i.description;
    const vPrice = d?.price ?? i.price ?? "";
    const vQty = d?.quantity !== undefined && d?.quantity !== null ? String(d.quantity) : String(i.quantity_available);
    const vSku = d?.sku ?? i.sku;
    const vVars: Record<string, VarEdit> = Object.fromEntries(i.variations.map((v) => [v.sku, { price: v.price ?? "", qty: String(v.quantity_available) }]));
    (d?.variations ?? []).forEach((x) => {
      if (vVars[x.sku]) vVars[x.sku] = { price: x.price ?? vVars[x.sku].price, qty: x.quantity !== undefined && x.quantity !== null ? String(x.quantity) : vVars[x.sku].qty };
    });
    const vPhotos: PhotoEdit[] = d?.photos
      ? d.photos.map((p, n) => (p.url ? { id: `e${n}`, thumb: p.url, url: p.url } : { id: "k:" + p.key, thumb: p.thumb ?? "", key: p.key }))
      : i.photos.map((p, n) => ({ id: `e${n}`, thumb: p.url, url: p.url }));
    const vSpecs: SpecEdit[] = (d?.item_specifics ?? i.item_specifics).map((x, n) => ({ id: `s${n}`, name: x.name, value: x.values.join("; ") }));
    setItem(i);
    setTitle(vTitle);
    setDescription(vDesc);
    setPrice(vPrice);
    setQty(vQty);
    setSku(vSku);
    setVars(vVars);
    setPhotos(vPhotos);
    const l = losesFormatting(vDesc);
    setLossy(l);
    setLossInfo(l ? lossDetail(vDesc) : []);
    setSpecs(vSpecs);
    setHtmlMode(l); // a description with styling opens as HTML so nothing is lost by accident
    setPhotoMsg("");
    setBaseSig(sigOf({ title: vTitle, description: vDesc, price: vPrice, qty: vQty, sku: vSku, vars: vVars, photos: vPhotos, specs: vSpecs }));
  }

  async function loadLog(s: string, id: string) {
    try {
      const r = await api<{ rows: LogRow[] }>(`/ebay-edit/log?store=${encodeURIComponent(s)}&item_id=${encodeURIComponent(id)}&limit=10`);
      setLog(r.rows);
    } catch {
      /* the log is a convenience; never block the page on it */
    }
  }

  async function load(s: string, id: string) {
    setBusy("load");
    setMsg(null);
    try {
      fill(await api<Item>(`/ebay-edit/item?store=${encodeURIComponent(s)}&item_id=${encodeURIComponent(id)}`));
      void loadLog(s, id);
    } catch (e) {
      setItem(null);
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const p = new URLSearchParams(window.location.search);
    const s = p.get("store") ?? "";
    const id = p.get("item") ?? "";
    setStore(s);
    if (s && id) void load(s, id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Only what differs from eBay goes into the request; the server checks again.
  const specsChanged =
    !!item &&
    specSig(specs) !== specSig(item.item_specifics.map((x) => ({ name: x.name, value: x.values.join("; ") })));
  const specsPart = () =>
    item && item.specifics_editable && specsChanged
      ? { item_specifics: specs.filter((x) => x.name.trim() || x.value.trim()).map((x) => ({ name: x.name, values: splitValues(x.value) })) }
      : {};
  const photosChanged = !!item && photos.map(photoId).join("|") !== item.photos.map((p) => p.url).join("|");
  const photoPart = () =>
    item && item.photos_editable && photosChanged ? { photos: photos.map((p) => (p.url ? { url: p.url } : { key: p.key })) } : {};

  function changes() {
    if (!item) return {};
    if (item.has_variations) {
      return {
        title,
        description,
        variations: item.variations
          .map((v) => ({ sku: v.sku, price: vars[v.sku]?.price ?? "", quantity: Number(vars[v.sku]?.qty ?? v.quantity_available) }))
          .filter((v, i) => v.price !== (item.variations[i].price ?? "") || v.quantity !== item.variations[i].quantity_available),
        ...photoPart(),
        ...specsPart(),
      };
    }
    return { title, description, price, quantity: qty.trim() === "" ? null : Number(qty), sku, ...photoPart(), ...specsPart() };
  }

  const dirty =
    !!item &&
    (title !== item.title ||
      (item.photos_editable && photosChanged) ||
      (item.specifics_editable && specsChanged) ||
      norm(description) !== norm(item.description) ||
      (item.has_variations
        ? item.variations.some((v) => (vars[v.sku]?.price ?? "") !== (v.price ?? "") || vars[v.sku]?.qty !== String(v.quantity_available))
        : price !== (item.price ?? "") || qty !== String(item.quantity_available) || sku !== item.sku));

  const touch = () => setDifferences([]);

  const unsaved = !!item && baseSig !== sigOf({ title, description, price, qty, sku, vars, photos, specs });

  // Everything on the form, for saving on the website (the whole state, so it comes back exactly as left).
  function draftChanges() {
    if (!item) return {};
    const d: Record<string, unknown> = { title, description };
    if (item.has_variations) {
      d.variations = item.variations.map((v) => ({ sku: v.sku, price: vars[v.sku]?.price ?? "", quantity: Number(vars[v.sku]?.qty ?? v.quantity_available) }));
    } else {
      Object.assign(d, { price, sku });
      if (qty.trim() !== "") d.quantity = Number(qty);
    }
    if (item.photos_editable) d.photos = photos.map((p) => (p.url ? { url: p.url, thumb: p.thumb } : { key: p.key, thumb: p.thumb }));
    if (item.specifics_editable) d.item_specifics = specs.filter((x) => x.name.trim() || x.value.trim()).map((x) => ({ name: x.name, values: splitValues(x.value) }));
    return d;
  }

  async function persistDraft(): Promise<Item | null> {
    if (!item) return null;
    const r = await api<{ draft: Item["draft"] }>("/ebay-edit/draft", { method: "PUT", body: { store, item_id: item.item_id, changes: draftChanges() } });
    const next = { ...item, draft: r.draft };
    setItem(next);
    setBaseSig(sigOf({ title, description, price, qty, sku, vars, photos, specs }));
    return next;
  }

  async function saveChanges() {
    setBusy("save");
    setMsg(null);
    try {
      await persistDraft();
      setMsg({ kind: "success", text: "Saved on the website. Nothing has been sent to eBay." });
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  function cancelChanges() {
    if (!item) return;
    fill(item); // back to what was last saved (or what eBay has, if nothing was saved)
    setMsg(null);
  }

  async function discardSaved() {
    if (!item || !window.confirm("Throw away the edits saved on the website for this listing? The form goes back to what eBay has.")) return;
    setBusy("discard");
    setMsg(null);
    try {
      await api(`/ebay-edit/draft?store=${encodeURIComponent(store)}&item_id=${encodeURIComponent(item.item_id)}`, { method: "DELETE" });
      fill({ ...item, draft: null });
      setMsg({ kind: "success", text: "Saved edits discarded." });
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  // Activate: bring an ended listing back on eBay, or publish edits to a live one. The edits are saved first, so nothing is
  // lost if eBay says no; the exact request is checked (a dry run) and read out before anything is sent.
  async function activate() {
    if (!item) return;
    const relist = item.ended;
    const warn = relist
      ? `Activate will make this a LIVE listing on eBay (${store}). Buyers will be able to buy it, and eBay will give it a new item number.`
      : `Activate will publish these changes to the LIVE eBay listing (${store}, item ${item.item_id}). Buyers will see them straight away.`;
    setBusy("activate");
    setMsg(null);
    try {
      if (unsaved) await persistDraft();
      const body = { store, item_id: item.item_id, changes: changes(), action: relist ? "relist" : "revise" };
      const pv = await api<Preview>("/ebay-edit/preview", { method: "POST", body });
      if (!pv.live_pushes_enabled) {
        setMsg({ kind: "error", text: `Live pushes are OFF for ${store}, so nothing can go to eBay. Turn them on in the Stores box on Live listings. Your edits are saved.` });
        return;
      }
      const list = pv.diff.map((d) => `${d.field}: ${d.before ?? "—"} → ${d.after}`).join("\n");
      if (!window.confirm(`${warn}\n\n${list}\n\nContinue?`)) return;
      const r = await api<{ item: Item; warnings: { message: string }[]; differences: string[]; notes?: string[] }>("/ebay-edit/push", {
        method: "POST",
        body: { ...body, fingerprint: pv.fingerprint },
      });
      if (r.item.item_id !== item.item_id) window.history.replaceState(null, "", `/admin/ebay-edit?store=${encodeURIComponent(store)}&item=${r.item.item_id}`);
      fill(r.item);
      setDifferences(r.differences);
      setMsg({
        kind: "success",
        text: `${relist ? `Live on eBay again as item ${r.item.item_id}.` : "Published. eBay accepted the change."}${r.warnings.length ? " eBay warnings: " + r.warnings.map((w) => w.message).join("; ") : ""}${r.notes && r.notes.length ? " " + r.notes.join(" ") : ""}`,
      });
      void loadLog(store, r.item.item_id);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
      void loadLog(store, item.item_id);
    } finally {
      setBusy("");
    }
  }

  // Inactivate: end the live listing on eBay.
  async function makeInactive() {
    if (!item) return;
    if (!window.confirm(`Inactivate will END this listing on eBay (${store}, item ${item.item_id}). Buyers will no longer be able to buy it. You can bring it back later with Activate.\n\nContinue?`)) return;
    setBusy("end");
    setMsg(null);
    try {
      if (unsaved) await persistDraft();
      const r = await api<{ item: Item }>("/ebay-edit/end", { method: "POST", body: { store, item_id: item.item_id } });
      fill(r.item);
      setMsg({ kind: "success", text: "The listing was ended on eBay." });
      void loadLog(store, item.item_id);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  // ---- photos -----------------------------------------------------------
  function movePhoto(n: number, to: number) {
    if (to < 0 || to >= photos.length) return;
    const next = photos.slice();
    const [p] = next.splice(n, 1);
    next.splice(to, 0, p);
    setPhotos(next);
    touch();
  }

  function removePhoto(n: number) {
    setPhotos(photos.filter((_, i) => i !== n));
    touch();
  }

  function addKeys(list: { key: string; thumb: string }[]): number {
    const have = new Set(photos.map(photoId));
    const fresh = list.filter((x) => !have.has("key:" + x.key)).slice(0, Math.max(0, MAX_PHOTOS - photos.length));
    if (fresh.length) {
      setPhotos([...photos, ...fresh.map((x) => ({ id: "k:" + x.key, thumb: x.thumb, key: x.key }))]);
      touch();
    }
    return fresh.length;
  }

  async function uploadFiles(files: FileList | null) {
    if (!files || files.length === 0) return;
    setBusy("upload");
    setPhotoMsg("");
    const added: { key: string; thumb: string }[] = [];
    try {
      for (const f of Array.from(files).slice(0, MAX_PHOTOS - photos.length)) {
        const data = await shrink(f);
        const r = await api<{ key: string; thumb: string }>("/ebay-edit/photo-upload", { method: "POST", body: { data } });
        added.push({ key: r.key, thumb: r.thumb });
      }
      addKeys(added);
      setPhotoMsg("Added to the list below. Nothing goes to eBay until you press Activate.");
    } catch (e) {
      if (added.length) addKeys(added);
      setPhotoMsg(errMsg(e));
    } finally {
      setBusy("");
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  useEffect(() => {
    const term = libQ.trim();
    if (!showLib || term.length < 2) {
      setLibResults(null);
      return;
    }
    let cancelled = false;
    const timer = setTimeout(() => {
      api<{ products: Found[] }>(`/photo-library?q=${encodeURIComponent(term)}`)
        .then((r) => {
          if (!cancelled) setLibResults(r.products);
        })
        .catch((e) => {
          if (!cancelled) setPhotoMsg(errMsg(e));
        });
    }, 400);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [libQ, showLib]);

  // ---- description ------------------------------------------------------
  function switchEditor() {
    if (htmlMode && lossy && !window.confirm("This description has styling the visual editor cannot keep (for example colours, fonts or images). If you change it in the visual editor, that styling will be simplified. Switch anyway?")) return;
    setHtmlMode(!htmlMode);
  }

  const off = !item?.editable;

  // The same controls at the top and the bottom of the page.
  const bar = item ? (
    <div className="flex flex-wrap items-center gap-3 rounded border border-gray-200 bg-gray-50 px-3 py-2">
      {unsaved && (
        <>
          <button type="button" className={btnCls} disabled={busy !== ""} onClick={() => void saveChanges()}>
            {busy === "save" ? "Saving…" : "Save changes"}
          </button>
          <button type="button" className={btn2Cls} disabled={busy !== ""} onClick={cancelChanges}>
            Cancel changes
          </button>
        </>
      )}
      {(item.ended ? item.relistable : dirty) && (
        <button type="button" className={btnCls} disabled={busy !== ""} onClick={() => void activate()}>
          {busy === "activate" ? "Working…" : "Activate"}
        </button>
      )}
      {!item.ended && item.editable && (
        <button type="button" className={`${btn2Cls} text-red-700`} disabled={busy !== ""} onClick={() => void makeInactive()}>
          {busy === "end" ? "Working…" : "Inactivate"}
        </button>
      )}
      <span className="text-xs text-gray-500">
        {item.ended
          ? "Not live on eBay. Activate puts it live."
          : dirty
            ? "Save keeps edits on the website only. Activate publishes them to eBay."
            : "Live on eBay."}
      </span>
    </div>
  ) : null;

  return (
    <div className="w-full space-y-5">
      <div className="flex items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">Edit eBay listing</h1>
        <Link href="/admin/ebay-listings" className={btn2Cls}>
          Back to Live listings
        </Link>
      </div>

      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}
      {differences.length > 0 && (
        <Notice kind="error">
          eBay accepted the change, but what it shows now is not exactly what was sent:
          <ul className="mt-1 list-disc pl-5">
            {differences.map((d) => (
              <li key={d}>{d}</li>
            ))}
          </ul>
        </Notice>
      )}
      {busy === "load" && <p className="text-sm text-gray-600">Reading the listing from eBay…</p>}

      {!item && busy !== "load" && !msg && <Notice>Open this page from the Edit link on a listing in Live listings.</Notice>}

      {item && (
        <>
          <div className="flex gap-4 rounded border border-gray-200 bg-white p-4">
            {item.image_url ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={item.image_url} alt="" className="h-20 w-20 rounded object-cover" />
            ) : null}
            <div className="space-y-1 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{store}</span>
                <a href={item.url} target="_blank" rel="noreferrer" className="text-blue-700 hover:underline">
                  {item.item_id}
                </a>
                <Badge tone="gray">{item.status || "—"}</Badge>
                {item.has_variations && <Badge tone="blue">Variations</Badge>}
                <span className="text-gray-500">{item.quantity_sold} sold</span>
              </div>
            </div>
          </div>

          {bar}
          {item.draft && (
            <Notice>
              This listing has edits saved on the website
              {item.draft.updated_at ? ` (${new Date(item.draft.updated_at).toLocaleString()})` : ""} that are not on eBay.{" "}
              <button type="button" className="underline" disabled={busy !== ""} onClick={() => void discardSaved()}>
                Discard saved edits
              </button>
            </Notice>
          )}
          {item.ended && item.relistable && (
            <Notice>This listing is not live on eBay ({item.status}). Edit it here, then press Activate to bring it back; eBay gives a relisted item a new number.</Notice>
          )}
          {!item.editable && <Notice kind="error">{item.not_editable_reason}</Notice>}
          {item.quantity_sold > 0 && (
            <p className="text-xs text-gray-600">Something has already sold, so eBay will not allow the title to be changed.</p>
          )}

          <div className="space-y-4 rounded border border-gray-200 bg-white p-4">
            <label className="block text-sm font-medium text-gray-700">
              <span className="mb-1 block">
                Title <span className="font-normal text-gray-500">({title.length}/80)</span>
              </span>
              <input
                className={inputCls}
                value={title}
                maxLength={80}
                disabled={off || item.quantity_sold > 0}
                onChange={(e) => {
                  setTitle(e.target.value);
                  touch();
                }}
              />
            </label>

            {!item.has_variations && (
              <div className="grid gap-4 sm:grid-cols-3">
                <label className="block text-sm font-medium text-gray-700">
                  <span className="mb-1 block">Price ({item.currency})</span>
                  <input className={inputCls} inputMode="decimal" value={price} disabled={off} onChange={(e) => { setPrice(e.target.value); touch(); }} />
                </label>
                <label className="block text-sm font-medium text-gray-700">
                  <span className="mb-1 block">Available quantity</span>
                  <input className={inputCls} inputMode="numeric" value={qty} disabled={off} onChange={(e) => { setQty(e.target.value); touch(); }} />
                </label>
                <label className="block text-sm font-medium text-gray-700">
                  <span className="mb-1 block">Custom label (SKU)</span>
                  <input className={inputCls} value={sku} maxLength={50} disabled={off} onChange={(e) => { setSku(e.target.value); touch(); }} />
                </label>
              </div>
            )}

            {item.has_variations && (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead className="text-xs uppercase text-gray-500">
                    <tr>
                      <th className="py-1 pr-3">Variation</th>
                      <th className="py-1 pr-3">SKU</th>
                      <th className="py-1 pr-3">Price ({item.currency})</th>
                      <th className="py-1 pr-3">Available</th>
                      <th className="py-1">Sold</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100">
                    {item.variations.map((v) => (
                      <tr key={v.sku} className="align-middle">
                        <td className="py-1.5 pr-3">{v.specifics || "—"}</td>
                        <td className="py-1.5 pr-3 text-gray-600">{v.sku}</td>
                        <td className="py-1.5 pr-3">
                          <input
                            className={`${inputCls} w-28`}
                            inputMode="decimal"
                            value={vars[v.sku]?.price ?? ""}
                            disabled={off}
                            onChange={(e) => { setVars({ ...vars, [v.sku]: { ...vars[v.sku], price: e.target.value } }); touch(); }}
                          />
                        </td>
                        <td className="py-1.5 pr-3">
                          <input
                            className={`${inputCls} w-24`}
                            inputMode="numeric"
                            value={vars[v.sku]?.qty ?? ""}
                            disabled={off}
                            onChange={(e) => { setVars({ ...vars, [v.sku]: { ...vars[v.sku], qty: e.target.value } }); touch(); }}
                          />
                        </td>
                        <td className="py-1.5 text-gray-600">{v.quantity_sold}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <p className="mt-2 text-xs text-gray-500">A variation&rsquo;s quantity must stay at 1 or more; at 0 eBay may drop the variation from the listing.</p>
              </div>
            )}

            <div className="space-y-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-sm font-medium text-gray-700">
                  Photos <span className="font-normal text-gray-500">({photos.length} of {MAX_PHOTOS}; the first is the main photo)</span>
                </span>
                <div className="flex flex-wrap gap-2">
                  <button type="button" className={btn2Cls} disabled={off || !item.photos_editable || busy !== "" || photos.length >= MAX_PHOTOS} onClick={() => fileRef.current?.click()}>
                    {busy === "upload" ? "Uploading…" : "Add from computer"}
                  </button>
                  <button type="button" className={btn2Cls} disabled={off || !item.photos_editable || photos.length >= MAX_PHOTOS} onClick={() => setShowLib(!showLib)}>
                    Add from website photos
                  </button>
                  <input ref={fileRef} type="file" accept="image/*" multiple className="hidden" onChange={(e) => void uploadFiles(e.target.files)} />
                </div>
              </div>
              {!item.photos_editable && item.editable && (
                <p className="text-xs text-gray-500">Photos on a listing with variations cannot be changed here yet.</p>
              )}
              {photoMsg && <p className="text-xs text-gray-600">{photoMsg}</p>}
              {showLib && (
                <div className="space-y-2 rounded border border-gray-200 bg-gray-50 p-3">
                  <input className={inputCls} placeholder="Find a website product by title, SKU or item number" value={libQ} onChange={(e) => setLibQ(e.target.value)} />
                  {libResults && libResults.length === 0 && <p className="text-xs text-gray-500">No website product with photos matches that.</p>}
                  {libResults?.map((pr) => (
                    <div key={pr.id}>
                      <div className="text-xs text-gray-700">
                        <span className="font-medium">{pr.sku}</span> {pr.title}
                      </div>
                      <div className="mt-1 flex flex-wrap gap-2">
                        {pr.photos.map((ph) => {
                          const used = photos.some((x) => x.key === ph.image_key);
                          return (
                            <button
                              key={ph.image_key}
                              type="button"
                              disabled={used || photos.length >= MAX_PHOTOS}
                              onClick={() => addKeys([{ key: ph.image_key, thumb: ph.thumb ?? "" }])}
                              className="rounded border border-gray-300 bg-white p-0.5 hover:border-blue-500 disabled:opacity-40"
                              title={used ? "Already in the list" : "Add this photo"}
                            >
                              {/* eslint-disable-next-line @next/next/no-img-element */}
                              <img src={ph.thumb ?? ""} alt="" className="h-16 w-16 object-cover" />
                            </button>
                          );
                        })}
                      </div>
                    </div>
                  ))}
                </div>
              )}
              <ul className="flex flex-wrap gap-3">
                {photos.map((p, n) => (
                  <li key={p.id} className="w-28 space-y-1 text-center text-xs">
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img src={p.thumb} alt="" className="h-28 w-28 rounded border border-gray-200 object-cover" />
                    <div className="flex justify-center gap-1">
                      {n === 0 && <Badge tone="blue">Main</Badge>}
                      {p.key && <Badge tone="green">New</Badge>}
                    </div>
                    {item.photos_editable && !off && (
                      <div className="flex flex-wrap justify-center gap-1">
                        <button type="button" className="rounded border border-gray-300 px-1.5 py-0.5 hover:bg-gray-100 disabled:opacity-40" disabled={n === 0} onClick={() => movePhoto(n, n - 1)} title="Move earlier">←</button>
                        <button type="button" className="rounded border border-gray-300 px-1.5 py-0.5 hover:bg-gray-100 disabled:opacity-40" disabled={n === photos.length - 1} onClick={() => movePhoto(n, n + 1)} title="Move later">→</button>
                        <button type="button" className="rounded border border-gray-300 px-1.5 py-0.5 hover:bg-gray-100 disabled:opacity-40" disabled={n === 0} onClick={() => movePhoto(n, 0)} title="Make this the main photo">Main</button>
                        <button type="button" className="rounded border border-gray-300 px-1.5 py-0.5 text-red-700 hover:bg-red-50 disabled:opacity-40" disabled={photos.length <= 1} onClick={() => removePhoto(n)} title="Take this photo off the listing">Remove</button>
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            </div>

            <div className="space-y-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-sm font-medium text-gray-700">
                  Description <span className="font-normal text-gray-500">({description.length.toLocaleString("en-US")} characters; the whole description is replaced when sent)</span>
                </span>
                <button type="button" className={btn2Cls} disabled={off} onClick={switchEditor}>
                  {htmlMode ? "Switch to visual editor" : "Edit as HTML"}
                </button>
              </div>
              {lossy && htmlMode && (
                <Notice>
                  This description has formatting the visual editor cannot keep, so it opened as HTML. Edit it here to keep everything as it is.
                  {lossInfo.length > 0 && (
                    <ul className="mt-1 list-disc pl-5 text-xs">
                      {lossInfo.map((x) => (
                        <li key={x}>{x}</li>
                      ))}
                    </ul>
                  )}
                </Notice>
              )}
              {htmlMode ? (
                <textarea
                  className={`${inputCls} h-48 font-mono text-xs`}
                  value={description}
                  disabled={off}
                  aria-label="Description (HTML)"
                  onChange={(e) => {
                    setDescription(e.target.value);
                    touch();
                  }}
                />
              ) : (
                <DescriptionEditor
                  label=""
                  value={description}
                  disabled={off}
                  onChange={(h) => {
                    if (sameHtml(h, description)) return; // the editor re-saying what we already have, not an edit
                    setDescription(h);
                    touch();
                  }}
                />
              )}
            </div>
          </div>

          <div className="space-y-3 rounded border border-gray-200 bg-white p-4 text-sm">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 className="font-semibold">Item specifics</h2>
              <button
                type="button"
                className={btn2Cls}
                disabled={off || !item.specifics_editable}
                onClick={() => {
                  setSpecs([...specs, { id: `n${Date.now()}`, name: "", value: "" }]);
                  touch();
                }}
              >
                Add item specific
              </button>
            </div>
            <p className="text-xs text-gray-500">
              The whole list is replaced when sent. Separate several values with a semicolon (for example: Cast; Single Ring). Condition and compatibility are below and cannot be changed here yet.
              {!item.specifics_editable && item.editable && " Item specifics on a listing with variations cannot be changed here yet."}
            </p>
            <div className="space-y-2">
              {specs.map((sp, n) => (
                <div key={sp.id} className="flex flex-wrap items-center gap-2">
                  <input
                    className={`${inputCls} w-48 font-bold`}
                    placeholder="Name"
                    value={sp.name}
                    maxLength={65}
                    disabled={off || !item.specifics_editable}
                    onChange={(e) => {
                      setSpecs(specs.map((x, i) => (i === n ? { ...x, name: e.target.value } : x)));
                      touch();
                    }}
                  />
                  <input
                    className={`${inputCls} min-w-0 flex-1`}
                    placeholder="Value"
                    value={sp.value}
                    disabled={off || !item.specifics_editable}
                    onChange={(e) => {
                      setSpecs(specs.map((x, i) => (i === n ? { ...x, value: e.target.value } : x)));
                      touch();
                    }}
                  />
                  <button
                    type="button"
                    className="rounded border border-gray-300 px-2 py-1 text-xs text-red-700 hover:bg-red-50 disabled:opacity-40"
                    disabled={off || !item.specifics_editable}
                    onClick={() => {
                      setSpecs(specs.filter((_, i) => i !== n));
                      touch();
                    }}
                  >
                    Remove
                  </button>
                </div>
              ))}
              {specs.length === 0 && <p className="text-xs text-gray-500">This listing has no item specifics.</p>}
            </div>
            <div className="border-t border-gray-100 pt-2 text-xs text-gray-700">
              <span className="font-medium">Condition:</span> {item.condition.name || item.condition.id || "not shown by eBay"}
              {item.condition.note && <span className="text-gray-500"> ({item.condition.note})</span>} <span className="text-gray-500">(read only for now)</span>
            </div>
          </div>

          <details className="rounded border border-gray-200 bg-white p-4 text-sm">
            <summary className="cursor-pointer font-semibold">
              Compatibility ({item.compatibility.count} vehicle{item.compatibility.count === 1 ? "" : "s"}, read only for now)
            </summary>
            {item.compatibility.rows.length === 0 ? (
              <p className="mt-2 text-xs text-gray-500">
                {item.compatibility.count > 0 ? "eBay reports a compatibility list but did not return its rows." : "This listing has no compatibility list."}
              </p>
            ) : (
              <div className="mt-2 overflow-x-auto">
                {(() => {
                  const cols: string[] = Array.from(new Set<string>(item.compatibility.rows.flatMap((r) => Object.keys(r)))).sort((x, y) =>
                    x === "Notes" ? 1 : y === "Notes" ? -1 : 0,
                  );
                  return (
                    <table className="w-full text-left text-xs">
                      <thead className="uppercase text-gray-500">
                        <tr>
                          {cols.map((c) => (
                            <th key={c} className="py-1 pr-3">{c}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-gray-100">
                        {item.compatibility.rows.map((r, i) => (
                          <tr key={i}>
                            {cols.map((c) => (
                              <td key={c} className="py-1 pr-3">{r[c] ?? ""}</td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  );
                })()}
                {item.compatibility.truncated && (
                  <p className="mt-2 text-xs text-gray-500">Showing the first {item.compatibility.rows.length} of {item.compatibility.count}.</p>
                )}
              </div>
            )}
          </details>

          {bar}

          <div className="rounded border border-gray-200 bg-white p-4">
            <h2 className="text-sm font-semibold">Recent activity on this listing</h2>
            {log.length === 0 ? (
              <p className="mt-2 py-1.5 text-sm text-gray-500">Nothing yet.</p>
            ) : (
              <div className="mt-2 overflow-x-auto">
                {/* A table sizes each column to its widest entry, so the columns stay left aligned whatever the name or date length. */}
                <table className="w-full text-left text-sm">
                  <tbody className="divide-y divide-gray-100">
                    {log.map((l) => (
                      <tr key={l.id} className="align-top">
                        <td className="whitespace-nowrap py-1.5 pr-4 text-gray-500">{new Date(l.created_at).toLocaleString()}</td>
                        <td className="whitespace-nowrap py-1.5 pr-4">
                          <Badge tone={l.dry_run ? "gray" : l.ok ? "green" : "amber"}>{l.dry_run ? "Preview" : l.ok ? "Sent" : "Failed"}</Badge>
                        </td>
                        <td className="whitespace-nowrap py-1.5 pr-4 text-gray-600">{l.action.replace(/_/g, " ").replace(" fixed price item", "")}</td>
                        <td className="whitespace-nowrap py-1.5 pr-4 text-gray-600">{l.user_name ?? l.user_email ?? ""}</td>
                        <td className="py-1.5 text-red-700">{l.error ?? ""}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
