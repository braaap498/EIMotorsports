"use client";

import ClearableInput from "../../../components/admin/ClearableInput";
import Link from "next/link";
import { useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Badge, Modal, Notice, btnCls, btn2Cls, inputCls } from "../../../components/admin/ui";

/* ------------------------------------------------------------------ types */

type NewListing = {
  item_id: string;
  sku: string; // shown the same way as product SKUs, e.g. EBAY-168810301004
  title: string;
  price: string | null;
  currency: string | null;
  image_url: string | null;
  url: string;
  listed_at: string | null;
};
type CheckResp = {
  seller: string;
  deep: boolean;
  pages_read: number;
  listings_read: number;
  ebay_total: number | null;
  reached_end: boolean;
  new_count: number;
  items: NewListing[];
};
type Product = {
  id: number;
  sku: string;
  title: string;
  list_price: number | string | null;
  quantity_on_hand: number;
  is_published: boolean;
  listing_count: number;
  part_number: string | null;
};
type SearchResp = { total: number; shown: number; items: Product[] };
type LinkResp = { ok: boolean; item_id: string; sku: string; product_id: number; product_title: string; listing_count: number };

const money = (v: number | string | null) => (v == null ? "—" : Number(v).toFixed(2));

// The first few words of an eBay title make a good starting search for the matching product.
const startingSearch = (title: string) => title.split(/\s+/).slice(0, 4).join(" ");

/* ------------------------------------------------------ link-to-product box */

function LinkModal({
  listing,
  onClose,
  onLinked,
}: {
  listing: NewListing;
  onClose: () => void;
  onLinked: (r: LinkResp) => void;
}) {
  const [q, setQ] = useState(startingSearch(listing.title));
  const [results, setResults] = useState<SearchResp | null>(null);
  const [searching, setSearching] = useState(false);
  const [linking, setLinking] = useState<number | null>(null);
  const [error, setError] = useState("");

  async function search() {
    if (q.trim().length < 2) return;
    setSearching(true);
    setError("");
    try {
      setResults(await api<SearchResp>(`/cleanup/search?${new URLSearchParams({ q: q.trim(), limit: "40" })}`));
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setSearching(false);
    }
  }

  async function linkTo(p: Product) {
    setLinking(p.id);
    setError("");
    try {
      const r = await api<LinkResp>("/ebay-new/link", {
        method: "POST",
        body: { item_id: listing.item_id, product_id: p.id },
      });
      onLinked(r);
    } catch (e) {
      setError(errMsg(e));
      setLinking(null);
    }
  }

  return (
    <Modal title="Link this eBay listing to a product" onClose={onClose}>
      <div className="space-y-3">
        <div className="rounded border border-gray-200 bg-gray-50 p-3 text-sm">
          <div className="font-medium">{listing.title}</div>
          <div className="text-xs text-gray-500">{listing.sku}</div>
        </div>

        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            void search();
          }}
        >
          <ClearableInput
            className={inputCls}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search products by title or SKU"
            autoFocus onClear={() => { setQ(""); setResults(null); }} />
          <button type="submit" className={btnCls} disabled={searching || q.trim().length < 2}>
            {searching ? "Searching…" : "Search"}
          </button>
        </form>
        <p className="text-xs text-gray-500">Every word you type must match the product, so use fewer words if nothing shows up.</p>

        {error && <Notice kind="error">{error}</Notice>}

        {results && results.items.length === 0 && <p className="text-sm text-gray-500">No products match.</p>}
        {results && results.items.length > 0 && (
          <>
            {results.total > results.shown && (
              <p className="text-xs text-gray-500">
                Showing {results.shown} of {results.total}. Add a word to narrow it down.
              </p>
            )}
            <ul className="max-h-96 divide-y divide-gray-100 overflow-y-auto rounded border border-gray-200">
              {results.items.map((p) => (
                <li key={p.id} className="flex items-center justify-between gap-3 p-3">
                  <div className="min-w-0 text-sm">
                    <div className="font-medium">{p.title}</div>
                    <div className="mt-0.5 flex flex-wrap items-center gap-2 text-xs text-gray-500">
                      <span>{p.sku}</span>
                      {p.part_number && <span>PN {p.part_number}</span>}
                      <span>${money(p.list_price)}</span>
                      <span>{p.quantity_on_hand} on hand</span>
                      <Badge tone={p.listing_count > 0 ? "green" : "gray"}>{p.listing_count} eBay</Badge>
                      {!p.is_published && <Badge tone="amber">Draft</Badge>}
                    </div>
                  </div>
                  <button type="button" className={btnCls} disabled={linking !== null} onClick={() => void linkTo(p)}>
                    {linking === p.id ? "Linking…" : "Link"}
                  </button>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    </Modal>
  );
}

/* ------------------------------------------------------------------- page */

export default function NewEbayListingsPage() {
  const [deep, setDeep] = useState(false);
  const [checking, setChecking] = useState(false);
  const [result, setResult] = useState<CheckResp | null>(null);
  const [items, setItems] = useState<NewListing[]>([]);
  const [error, setError] = useState("");
  const [linkedMsg, setLinkedMsg] = useState<LinkResp | null>(null);
  const [linking, setLinking] = useState<NewListing | null>(null);

  async function check() {
    setChecking(true);
    setError("");
    setLinkedMsg(null);
    try {
      const r = await api<CheckResp>(`/ebay-new/check?deep=${deep}`);
      setResult(r);
      setItems(r.items);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setChecking(false);
    }
  }

  function linked(r: LinkResp) {
    setItems((list) => list.filter((i) => i.item_id !== r.item_id));
    setLinkedMsg(r);
    setLinking(null);
  }

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-bold">Link new eBay listings to an existing product</h1>
        <p className="mt-1 max-w-3xl text-sm text-gray-600">
          Finds eBay listings that are not linked to any product yet. Nothing is imported: pick the product each one belongs
          to and its eBay item number is added to that product&rsquo;s existing eBay item numbers.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-4 rounded-lg border border-gray-200 bg-white p-4">
        <button type="button" className={btnCls} onClick={() => void check()} disabled={checking}>
          {checking ? "Checking eBay…" : "Check eBay for new listings"}
        </button>
        <label className="flex items-center gap-2 text-sm text-gray-700">
          <input type="checkbox" checked={deep} onChange={(e) => setDeep(e.target.checked)} disabled={checking} />
          Scan everything (slower)
        </label>
        <span className="text-xs text-gray-500">
          A normal check reads the newest listings and stops once it reaches ones that are already linked.
        </span>
      </div>

      {error && <Notice kind="error">{error}</Notice>}
      {linkedMsg && (
        <Notice kind="success">
          Linked {linkedMsg.sku} to{" "}
          <Link href={`/admin/products/${linkedMsg.product_id}`} className="font-medium underline">
            {linkedMsg.product_title}
          </Link>
          , which now has {linkedMsg.listing_count} eBay item number{linkedMsg.listing_count === 1 ? "" : "s"}.
        </Notice>
      )}

      {result && (
        <div className="space-y-3">
          <p className="text-sm text-gray-600">
            Read {result.listings_read.toLocaleString()} listing{result.listings_read === 1 ? "" : "s"} from{" "}
            <span className="font-medium">{result.seller}</span>
            {result.ebay_total != null && <> (eBay reports {result.ebay_total.toLocaleString()} in total)</>}.{" "}
            {items.length === 0
              ? "Everything we read is already linked."
              : `${items.length} not linked yet.`}
            {!result.deep && !result.reached_end && " Stopped early; run “Scan everything” to read further back."}
          </p>

          {items.length > 0 && (
            <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white">
              <table className="w-full text-left text-sm">
                <thead className="bg-gray-50 text-xs uppercase text-gray-500">
                  <tr>
                    <th className="px-3 py-2"></th>
                    <th className="px-3 py-2">Listing</th>
                    <th className="px-3 py-2">Price</th>
                    <th className="px-3 py-2">Listed</th>
                    <th className="px-3 py-2"></th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {items.map((i) => (
                    <tr key={i.item_id}>
                      <td className="w-16 px-3 py-2">
                        {i.image_url && (
                          // eslint-disable-next-line @next/next/no-img-element
                          <img src={i.image_url} alt="" className="h-12 w-12 rounded border border-gray-200 object-cover" />
                        )}
                      </td>
                      <td className="max-w-md px-3 py-2">
                        <a href={i.url} target="_blank" rel="noopener noreferrer" className="font-medium hover:underline">
                          {i.title}
                        </a>
                        <div className="text-xs text-gray-500">{i.sku}</div>
                      </td>
                      <td className="whitespace-nowrap px-3 py-2">{i.price ? `$${money(i.price)}` : "—"}</td>
                      <td className="whitespace-nowrap px-3 py-2">
                        {i.listed_at ? new Date(i.listed_at).toLocaleDateString() : "—"}
                      </td>
                      <td className="px-3 py-2 text-right">
                        <button type="button" className={btn2Cls} onClick={() => setLinking(i)}>
                          Link to a product…
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {linking && <LinkModal listing={linking} onClose={() => setLinking(null)} onLinked={linked} />}
    </div>
  );
}
