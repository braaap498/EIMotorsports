"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Badge, Notice, btnCls, btn2Cls, inputCls } from "../../../components/admin/ui";
import DescriptionEditor from "../../../components/admin/DescriptionEditor";
import { sanitizeHtml } from "../../../components/admin/sanitizeHtml";
import { shrink } from "../../../components/admin/PhotosPanel";
import CompatEditor, { type CompatRow } from "../../../components/admin/CompatEditor";

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
  product_label?: string | null;
  dropship?: { listing_id: number; on: boolean; product_on: boolean } | null;
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
  category_id?: string;
  category_name?: string;
  compatibility: {
    count: number;
    rows: Record<string, string>[];
    truncated: boolean;
    list: CompatRow[]; // the complete list as eBay has it
    editable: boolean;
    reason: string | null;
  };
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
  compatibility?: CompatRow[];
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
const compatSig = (rows: CompatRow[]) => JSON.stringify(rows.map((r) => [[...r.pairs].sort(), r.notes.trim()]).sort());
const sigOf = (v: { title: string; description: string; price: string; qty: string; sku: string; vars: Record<string, VarEdit>; photos: PhotoEdit[]; specs: SpecEdit[]; compat: CompatRow[] }) =>
  JSON.stringify([v.title, norm(v.description), v.price, v.qty, v.sku, Object.keys(v.vars).sort().map((k) => [k, v.vars[k].price, v.vars[k].qty]), v.photos.map(photoId), specSig(v.specs), compatSig(v.compat)]);

// Read in an inert document, so nothing in the description can load or run while we compare.
const inert = (h: string) => new DOMParser().parseFromString(h, "text/html").body.innerHTML.replace(/\s+/g, "");

// The visual editor can hand back its own tidied-up version of the HTML it was given (on load, on blur,
// after a re-render) without the person having typed anything. That must not count as an edit: an edit
// clears the preview, which made the preview vanish the moment it appeared.
const sameHtml = (a: string, b: string) => inert(a) === inert(b) || inert(a) === inert(sanitizeHtml(b));

function EbayEditPage({ urlStore, urlItem }: { urlStore: string; urlItem: string }) {
  const [store, setStore] = useState("");
  const [item, setItem] = useState<Item | null>(null);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [price, setPrice] = useState("");
  const [qty, setQty] = useState("");
  const [sku, setSku] = useState("");
  const [vars, setVars] = useState<Record<string, VarEdit>>({});
  const [photos, setPhotos] = useState<PhotoEdit[]>([]);
  const [specs, setSpecs] = useState<SpecEdit[]>([]);
  const [compat, setCompat] = useState<CompatRow[]>([]);
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
  const shownId = useRef(""); // the item number the form is showing right now
  const loadSeq = useRef(0); // each load gets a number; only the newest one may fill the form

  // Show a listing on the form: what eBay has, with any edits saved on the website laid over it.
  function fill(i: Item) {
    const d = i.draft?.changes;
    const vTitle = d?.title ?? i.title;
    const vDesc = d?.description ?? i.description;
    const vPrice = d?.price ?? i.price ?? "";
    const vQty = String(i.quantity_available); // read only: the website owns stock, so a quantity saved in an older edit is ignored
    const vSku = i.product_label || i.sku; // the custom label comes from the product
    const vVars: Record<string, VarEdit> = Object.fromEntries(i.variations.map((v) => [v.sku, { price: v.price ?? "", qty: String(v.quantity_available) }]));
    (d?.variations ?? []).forEach((x) => {
      if (vVars[x.sku]) vVars[x.sku] = { price: x.price ?? vVars[x.sku].price, qty: vVars[x.sku].qty };
    });
    const vPhotos: PhotoEdit[] = d?.photos
      ? d.photos.map((p, n) => (p.url ? { id: `e${n}`, thumb: p.url, url: p.url } : { id: "k:" + p.key, thumb: p.thumb ?? "", key: p.key }))
      : i.photos.map((p, n) => ({ id: `e${n}`, thumb: p.url, url: p.url }));
    const vSpecs: SpecEdit[] = (d?.item_specifics ?? i.item_specifics).map((x, n) => ({ id: `s${n}`, name: x.name, value: x.values.join("; ") }));
    const vCompat: CompatRow[] = d?.compatibility ?? i.compatibility.list;
    shownId.current = i.item_id;
    setItem(i);
    setTitle(vTitle);
    setDescription(vDesc);
    setPrice(vPrice);
    setQty(vQty);
    setSku(vSku);
    setVars(vVars);
    setPhotos(vPhotos);
    setSpecs(vSpecs);
    setCompat(vCompat);
    setPhotoMsg("");
    setBaseSig(sigOf({ title: vTitle, description: vDesc, price: vPrice, qty: vQty, sku: vSku, vars: vVars, photos: vPhotos, specs: vSpecs, compat: vCompat }));
  }

  async function toggleDropship(on: boolean) {
    if (!item?.dropship) return;
    setBusy("dropship");
    setMsg(null);
    try {
      const r = await api<{ dropship_ok: boolean; queued: number }>(`/availability/listings/${item.dropship.listing_id}/dropship`, { method: "POST", body: { on } });
      setItem({ ...item, dropship: { ...item.dropship, on: r.dropship_ok } });
      setMsg({ kind: "success", text: `Drop ship is ${r.dropship_ok ? "on" : "off"} for this listing. ${r.queued ? "The new quantity is queued for eBay." : "No quantity change was needed."}` });
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
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
      const mine = ++loadSeq.current;
      const got = await api<Item>(`/ebay-edit/item?store=${encodeURIComponent(s)}&item_id=${encodeURIComponent(id)}`);
      if (mine !== loadSeq.current) return; // a newer item was asked for meanwhile: this answer is old
      fill(got);
      void loadLog(s, id);
    } catch (e) {
      setItem(null);
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  // Follow the address: when the store or item in it changes (a click from the list, a pasted address), show that item.
  // Never keep what the form was showing for the previous item.
  useEffect(() => {
    setStore(urlStore);
    if (!urlStore || !urlItem) return;
    if (shownId.current === urlItem) return; // already showing it (e.g. the address was just updated after a relist)
    loadSeq.current += 1;
    shownId.current = "";
    setItem(null);
    setMsg(null);
    setLog([]);
    setDifferences([]);
    void load(urlStore, urlItem);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlStore, urlItem]);

  // Only what differs from eBay goes into the request; the server checks again.
  const specsChanged =
    !!item &&
    specSig(specs) !== specSig(item.item_specifics.map((x) => ({ name: x.name, value: x.values.join("; ") })));
  const specsPart = () =>
    item && item.specifics_editable && specsChanged
      ? { item_specifics: specs.filter((x) => x.name.trim() || x.value.trim()).map((x) => ({ name: x.name, values: splitValues(x.value) })) }
      : {};
  const compatChanged = !!item && compatSig(compat) !== compatSig(item.compatibility.list);
  const compatPart = () => (item && item.compatibility.editable && compatChanged ? { compatibility: compat } : {});
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
          .map((v) => ({ sku: v.sku, price: vars[v.sku]?.price ?? "" }))
          .filter((v, i) => v.price !== (item.variations[i].price ?? "")),
        ...photoPart(),
        ...specsPart(),
        ...compatPart(),
      };
    }
    return { title, description, price, ...photoPart(), ...specsPart(), ...compatPart() }; // quantity is never sent from here: the website owns stock
  }

  const dirty =
    !!item &&
    (title !== item.title ||
      (item.photos_editable && photosChanged) ||
      (item.specifics_editable && specsChanged) ||
      (item.compatibility.editable && compatChanged) ||
      norm(description) !== norm(item.description) ||
      (item.has_variations
        ? item.variations.some((v) => (vars[v.sku]?.price ?? "") !== (v.price ?? ""))
        : price !== (item.price ?? "")));

  const touch = () => setDifferences([]);

  const unsaved = !!item && baseSig !== sigOf({ title, description, price, qty, sku, vars, photos, specs, compat });

  // Everything on the form, for saving on the website (the whole state, so it comes back exactly as left).
  function draftChanges() {
    if (!item) return {};
    const d: Record<string, unknown> = { title, description };
    if (item.has_variations) {
      d.variations = item.variations.map((v) => ({ sku: v.sku, price: vars[v.sku]?.price ?? "" }));
    } else {
      Object.assign(d, { price });
    }
    if (item.photos_editable) d.photos = photos.map((p) => (p.url ? { url: p.url, thumb: p.thumb } : { key: p.key, thumb: p.thumb }));
    if (item.compatibility.editable) d.compatibility = compat;
    if (item.specifics_editable) d.item_specifics = specs.filter((x) => x.name.trim() || x.value.trim()).map((x) => ({ name: x.name, values: splitValues(x.value) }));
    return d;
  }

  async function persistDraft(): Promise<Item | null> {
    if (!item) return null;
    const r = await api<{ draft: Item["draft"] }>("/ebay-edit/draft", { method: "PUT", body: { store, item_id: item.item_id, changes: draftChanges() } });
    const next = { ...item, draft: r.draft };
    setItem(next);
    setBaseSig(sigOf({ title, description, price, qty, sku, vars, photos, specs, compat }));
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

  // Relist (ended listing) / Revise listing (live listing with edits): bring an ended listing back on eBay, or publish edits to a live one. The edits are saved first, so nothing is
  // lost if eBay says no; the exact request is checked (a dry run) and read out before anything is sent.
  async function activate() {
    if (!item) return;
    const relist = item.ended;
    const warn = relist
      ? `Relist will make this a LIVE listing on eBay (${store}) at the quantity the website has available. Buyers will be able to buy it, and eBay will give it a new item number.`
      : `Revise listing will publish these changes to the LIVE eBay listing (${store}, item ${item.item_id}). Buyers will see them straight away.`;
    setBusy("activate");
    setMsg(null);
    try {
      if (unsaved) await persistDraft();
      const body = { store, item_id: item.item_id, changes: changes(), action: relist ? "relist" : "revise" };
      const pv = await api<Preview>("/ebay-edit/preview", { method: "POST", body });
      if (!pv.live_pushes_enabled) {
        setMsg({ kind: "error", text: `Live pushes are OFF for ${store}, so nothing can go to eBay. Turn them on in Store API → Stores. Your edits are saved.` });
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

  // End listing: end the live listing on eBay.
  async function makeInactive() {
    if (!item) return;
    if (!window.confirm(`End listing will END this listing on eBay (${store}, item ${item.item_id}). Buyers will no longer be able to buy it. You can bring it back later with Relist.\n\nContinue?`)) return;
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
      setPhotoMsg("Added to the list below. Nothing goes to eBay until you press Revise listing or Relist.");
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
          {busy === "activate" ? "Working…" : item.ended ? "Relist" : "Revise listing"}
        </button>
      )}
      {!item.ended && item.editable && (
        <button type="button" className={`${btn2Cls} text-red-700`} disabled={busy !== ""} onClick={() => void makeInactive()}>
          {busy === "end" ? "Working…" : "End listing"}
        </button>
      )}
      <span className="text-xs text-gray-500">
        {item.ended
          ? "Not live on eBay. Relist puts it live."
          : dirty
            ? "Save keeps edits on the website only. Revise listing publishes them to eBay."
            : "Live on eBay."}
      </span>
    </div>
  ) : null;

  return (
    <div className="w-full space-y-5">
      <div className="flex items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">Edit eBay listing</h1>
        <Link href="/admin/ebay-listings" className={btn2Cls}>
          Back to Listings
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

      {!item && busy !== "load" && !msg && <Notice>Open this page from the Edit link on a listing in Listings.</Notice>}

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
            <Notice>This listing is not live on eBay ({item.status}). Edit it here, then press Relist to bring it back; eBay gives a relisted item a new number.</Notice>
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
                  <input className={`${inputCls} bg-gray-100 text-gray-600`} value={qty} readOnly tabIndex={-1} title="Read only. The website's stock sets this: sales, receiving and adjustments update it." />
                  <span className="mt-1 block text-xs font-normal text-gray-500">Read only. Set by the website&rsquo;s stock. To pause the listing, use Set 0 Qty on the Listings page.</span>
                </label>
                <label className="block text-sm font-medium text-gray-700">
                  <span className="mb-1 block">Custom label (SKU)</span>
                  <input className={`${inputCls} bg-gray-100 text-gray-600`} value={sku} readOnly tabIndex={-1} title="Read only. The custom label comes from the product." />
                  <span className="mt-1 block text-xs font-normal text-gray-500">
                    Read only. The custom label comes from the product.
                    {item.product_label && item.product_label !== item.sku && (
                      <> eBay currently shows &ldquo;{item.sku || "(empty)"}&rdquo;; it is updated to the product&rsquo;s label when the listing is relisted.</>
                    )}
                  </span>
                </label>
              </div>
            )}

            {!item.has_variations && item.dropship && (
              <label className="flex items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  className="mt-1"
                  checked={item.dropship.on || item.dropship.product_on}
                  disabled={busy !== "" || item.dropship.product_on}
                  onChange={(e) => void toggleDropship(e.target.checked)}
                />
                <span>
                  <span className="font-medium">Drop ship from the distributor</span>
                  <span className="block text-xs text-gray-500">
                    {item.dropship.product_on
                      ? "On for every listing of this product (set on the Available page)."
                      : "The quantity is the distributor's stock (Indiana + Nevada) plus our own shelf stock. Untick it and the quantity falls back to our own stock only, and eBay is updated."}
                  </span>
                </span>
              </label>
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
                          <span className="text-gray-700" title="Read only. The website's stock sets this.">{vars[v.sku]?.qty ?? v.quantity_available}</span>
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
              </div>
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
              The whole list is replaced when sent. Separate several values with a semicolon (for example: Cast; Single Ring). Condition is below and cannot be changed here yet.
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

          <details className="rounded border border-gray-200 bg-white p-4 text-sm" open={compatChanged || item.compatibility.count === 0}>
            <summary className="cursor-pointer font-semibold">
              Compatible vehicles ({compat.length}
              {compatChanged ? `, was ${item.compatibility.count} on eBay` : ""})
            </summary>
            <div className="mt-3">
              <CompatEditor
                categoryId={item.category_id ?? ""}
                rows={compat}
                baseline={item.compatibility.list}
                disabled={busy !== ""}
                lockedReason={item.compatibility.editable ? null : item.compatibility.reason || item.not_editable_reason}
                onChange={(r) => {
                  setCompat(r);
                  touch();
                }}
              />
              {item.compatibility.truncated && <p className="mt-2 text-xs text-gray-500">eBay returned {item.compatibility.list.length} rows.</p>}
            </div>
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

export default function EbayEditRoute() {
  return (
    <Suspense fallback={null}>
      <EbayEditFromUrl />
    </Suspense>
  );
}

function EbayEditFromUrl() {
  const p = useSearchParams();
  return <EbayEditPage urlStore={p.get("store") ?? ""} urlItem={(p.get("item") ?? "").trim()} />;
}
