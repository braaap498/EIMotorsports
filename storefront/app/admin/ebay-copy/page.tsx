"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Notice, btnCls, btn2Cls, inputCls } from "../../../components/admin/ui";
import DescriptionEditor from "../../../components/admin/DescriptionEditor";

type Spec = { name: string; values: string[] };
type Copy = {
  id: number;
  store: string;
  source_item_id: string;
  product_id: number | null;
  status: string;
  new_item_id: string | null;
  title: string;
  description: string;
  price: string;
  sku: string;
  photos: string[];
  original_photos: string[];
  item_specifics: Spec[];
  currency: string;
  category_name: string;
  compat_count: number;
  condition_note: string;
};
type Preview = {
  ok: boolean;
  errors: { code: string; message: string }[];
  warnings: { code: string; message: string }[];
  fees: { name: string; amount: string }[];
  quantity: number;
  duplicates: string[];
  fingerprint: string | null;
  live_pushes_enabled: boolean;
};

const squash = (h: string) => h.replace(/\s+/g, "");

function CopyEditor({ id }: { id: number }) {
  const [copy, setCopy] = useState<Copy | null>(null);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [price, setPrice] = useState("");
  const [sku, setSku] = useState("");
  const [photos, setPhotos] = useState<string[]>([]);
  const [specs, setSpecs] = useState<{ name: string; value: string }[]>([]);
  const [dirty, setDirty] = useState(false);
  const [pv, setPv] = useState<Preview | null>(null);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);

  function fill(c: Copy) {
    setCopy(c);
    setTitle(c.title);
    setDescription(c.description);
    setPrice(c.price);
    setSku(c.sku);
    setPhotos(c.photos);
    setSpecs(c.item_specifics.map((s) => ({ name: s.name, value: s.values.join("; ") })));
    setDirty(false);
  }

  useEffect(() => {
    let cancelled = false;
    api<Copy>(`/ebay-copy/${id}`)
      .then((c) => {
        if (!cancelled) fill(c);
      })
      .catch((e) => {
        if (!cancelled) setMsg({ kind: "error", text: errMsg(e) });
      });
    return () => {
      cancelled = true;
    };
  }, [id]);

  function touch() {
    setDirty(true);
    setPv(null); // an edit makes any preview out of date
  }

  async function save(): Promise<boolean> {
    setBusy("save");
    setMsg(null);
    try {
      const c = await api<Copy>(`/ebay-copy/${id}`, {
        method: "PUT",
        body: {
          title,
          description,
          price,
          photos,
          item_specifics: specs
            .filter((s) => s.name.trim() || s.value.trim())
            .map((s) => ({ name: s.name, values: s.value.split(";").map((v) => v.trim()).filter(Boolean) })),
        },
      });
      fill(c);
      setMsg({ kind: "success", text: "Draft saved on the website. Nothing has gone to eBay." });
      return true;
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
      return false;
    } finally {
      setBusy("");
    }
  }

  async function check() {
    if (dirty && !(await save())) return;
    setBusy("check");
    setMsg(null);
    setPv(null);
    try {
      setPv(await api<Preview>(`/ebay-copy/${id}/preview`, { method: "POST" }));
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  async function listIt() {
    if (!pv?.fingerprint || !copy) return;
    if (!window.confirm(`List this as a NEW listing on ${copy.store}?\n\nQuantity ${pv.quantity}, price ${price}.\n\nIt goes live on eBay now.`)) return;
    setBusy("list");
    setMsg(null);
    try {
      const r = await api<{ item_id: string; warnings: { message: string }[]; notes: string[] }>(`/ebay-copy/${id}/list`, {
        method: "POST",
        body: { fingerprint: pv.fingerprint },
      });
      setMsg({
        kind: "success",
        text: `Live on eBay as item ${r.item_id}. ${r.notes.join(" ")}${r.warnings.length ? " eBay warnings: " + r.warnings.map((w) => w.message).join("; ") : ""}`,
      });
      setCopy((c) => (c ? { ...c, status: "listed", new_item_id: r.item_id } : c));
      setPv(null);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy("");
    }
  }

  async function discard() {
    if (!window.confirm("Throw this draft away? The original listing is not touched.")) return;
    try {
      await api(`/ebay-copy/${id}`, { method: "DELETE" });
      window.location.href = "/admin/ebay-listings";
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    }
  }

  if (!copy) return <div className="space-y-4">{msg ? <Notice kind={msg.kind}>{msg.text}</Notice> : <p className="text-sm text-gray-500">Loading…</p>}</div>;

  const listed = copy.status === "listed";
  const off = listed || busy !== "";

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">Sell similar</h1>
        <Link href="/admin/ebay-listings" className={btn2Cls}>Back to Listings</Link>
      </div>
      <p className="text-sm text-gray-600">
        Copy of eBay item {copy.source_item_id} in {copy.store}
        {copy.category_name ? ` · ${copy.category_name}` : ""}
        {copy.compat_count ? ` · ${copy.compat_count} compatible vehicles` : ""}. This is a draft on the website only.
        When it is listed, the new item number is linked to the same product (id {copy.product_id}).
      </p>
      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}
      {listed && (
        <Notice kind="success">
          Listed as eBay item {copy.new_item_id}.{" "}
          <Link className="underline" href={`/admin/ebay-edit?store=${encodeURIComponent(copy.store)}&item=${copy.new_item_id}`}>Open it</Link>
        </Notice>
      )}

      <div className="space-y-4 rounded border border-gray-200 bg-white p-4 text-sm">
        <label className="block">
          <span className="mb-1 block font-semibold">Title ({title.length}/80)</span>
          <input className={`${inputCls} w-full`} value={title} disabled={off} maxLength={80} onChange={(e) => { setTitle(e.target.value); touch(); }} />
        </label>
        <div className="flex flex-wrap gap-4">
          <label className="block">
            <span className="mb-1 block font-semibold">Price ({copy.currency})</span>
            <input className={inputCls} value={price} disabled={off} onChange={(e) => { setPrice(e.target.value); touch(); }} />
          </label>
          <label className="block">
            <span className="mb-1 block font-semibold">Custom label (SKU), from the product</span>
            <input className={`${inputCls} bg-gray-100 text-gray-600`} value={sku} readOnly tabIndex={-1} title="Read only. The custom label comes from the product." />
          </label>
        </div>
        <p className="text-xs text-gray-500">
          Quantity is not set here: the new listing gets the product&rsquo;s available stock from the website. eBay may refuse a listing that
          is identical to a live one in the same store; change the title if it does.
        </p>
      </div>

      <div className="space-y-2 rounded border border-gray-200 bg-white p-4 text-sm">
        <h2 className="font-semibold">Photos ({photos.length})</h2>
        <div className="flex flex-wrap gap-3">
          {photos.map((u, n) => (
            <div key={u} className="w-28 space-y-1">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={u} alt="" className="h-28 w-28 rounded border border-gray-200 object-cover" />
              <div className="flex gap-1 text-xs">
                {n > 0 && (
                  <button type="button" className="text-blue-700 hover:underline" disabled={off} onClick={() => { setPhotos([u, ...photos.filter((x) => x !== u)]); touch(); }}>
                    Make main
                  </button>
                )}
                {photos.length > 1 && (
                  <button type="button" className="text-red-700 hover:underline" disabled={off} onClick={() => { setPhotos(photos.filter((x) => x !== u)); touch(); }}>
                    Remove
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="space-y-2 rounded border border-gray-200 bg-white p-4 text-sm">
        <h2 className="font-semibold">Description</h2>
        <DescriptionEditor
          label=""
          value={description}
          disabled={off}
          onChange={(h) => {
            if (squash(h) === squash(description)) return;
            setDescription(h);
            touch();
          }}
        />
      </div>

      <div className="space-y-2 rounded border border-gray-200 bg-white p-4 text-sm">
        <div className="flex items-center justify-between">
          <h2 className="font-semibold">Item specifics</h2>
          <button type="button" className={btn2Cls} disabled={off} onClick={() => { setSpecs([...specs, { name: "", value: "" }]); touch(); }}>
            Add
          </button>
        </div>
        {specs.map((s, n) => (
          <div key={n} className="flex flex-wrap gap-2">
            <input className={`${inputCls} w-48`} placeholder="Name" value={s.name} disabled={off} onChange={(e) => { setSpecs(specs.map((x, i) => (i === n ? { ...x, name: e.target.value } : x))); touch(); }} />
            <input className={`${inputCls} min-w-[16rem] flex-1`} placeholder="Value (separate several with ;)" value={s.value} disabled={off} onChange={(e) => { setSpecs(specs.map((x, i) => (i === n ? { ...x, value: e.target.value } : x))); touch(); }} />
            <button type="button" className="text-red-700 hover:underline" disabled={off} onClick={() => { setSpecs(specs.filter((_, i) => i !== n)); touch(); }}>
              Remove
            </button>
          </div>
        ))}
      </div>

      {pv && (
        <div className="space-y-2 rounded border border-gray-200 bg-white p-4 text-sm">
          <h2 className="font-semibold">What eBay says (nothing was listed)</h2>
          {pv.errors.map((e, i) => <Notice key={i} kind="error">{e.message} (eBay {e.code})</Notice>)}
          {pv.warnings.map((e, i) => <Notice key={i}>{e.message} (eBay {e.code})</Notice>)}
          {pv.ok && (
            <p>
              eBay accepts this listing. Quantity {pv.quantity}.
              {pv.fees.length > 0 ? ` Fees: ${pv.fees.map((f) => `${f.name} ${f.amount}`).join(", ")}.` : " No listing fee shown."}
            </p>
          )}
          {pv.duplicates.length > 0 && (
            <Notice kind="error">
              This store already has {pv.duplicates.length === 1 ? "a listing" : "listings"} with exactly this title ({pv.duplicates.join(", ")}). eBay may
              refuse an identical duplicate. Change the title or photos if it does.
            </Notice>
          )}
          {pv.ok && !pv.live_pushes_enabled && (
            <Notice kind="error">Live pushes are OFF for {copy.store}, so it cannot be listed. Turn them on in Store API → Stores.</Notice>
          )}
        </div>
      )}

      {!listed && (
        <div className="flex flex-wrap items-center gap-3">
          <button type="button" className={btn2Cls} disabled={off || !dirty} onClick={() => void save()}>
            {busy === "save" ? "Saving…" : "Save draft"}
          </button>
          <button type="button" className={btn2Cls} disabled={off} onClick={() => void check()}>
            {busy === "check" ? "Asking eBay…" : "Check with eBay"}
          </button>
          <button type="button" className={btnCls} disabled={off || !pv?.ok || !pv.fingerprint || !pv.live_pushes_enabled || dirty} onClick={() => void listIt()}>
            {busy === "list" ? "Listing…" : "List on eBay"}
          </button>
          <button type="button" className="text-sm text-red-700 hover:underline" disabled={off} onClick={() => void discard()}>
            Discard draft
          </button>
        </div>
      )}
    </div>
  );
}

function Inner() {
  const p = useSearchParams();
  const id = Number(p.get("id"));
  if (!id) return <Notice kind="error">No copy was chosen. Go to Live listings, tick one listing and press Sell similar.</Notice>;
  return <CopyEditor key={id} id={id} />;
}

export default function EbayCopyPage() {
  return (
    <Suspense fallback={null}>
      <Inner />
    </Suspense>
  );
}
