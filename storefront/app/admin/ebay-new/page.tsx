"use client";

import ClearableInput from "../../../components/admin/ClearableInput";
import Link from "next/link";
import { useEffect, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { loadSaved, saveSaved } from "../../../lib/savedState";
import { Badge, Modal, Notice, btnCls, btn2Cls, inputCls } from "../../../components/admin/ui";

/* ------------------------------------------------------------------ types */

type NewListing = {
  item_id: string;
  sku: string; // the custom label when we have one on file, otherwise EBAY- plus the item number, e.g. EBAY-168810301004
  custom_label: string | null; // the real custom label SKU, when we have it on file
  title: string;
  price: string | null;
  currency: string | null;
  image_url: string | null;
  url: string;
  listed_at: string | null;
};
type CheckResp = {
  store: string; // the store key the listings were found in (saved on the link)
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
  ebay_item_id?: string | null;
  custom_label_sku?: string | null;
  title: string;
  list_price: number | string | null;
  quantity_on_hand: number;
  is_published: boolean;
  listing_count: number;
  part_number: string | null;
  label_match?: number; // 1 = has exactly this listing's custom label SKU
  mpn_match?: number; // 1 = its part number appears in the listing title
};
type SuggestResp = { searching: boolean; items: Product[] };
type LinkResp = {
  ok: boolean;
  created: boolean; // true when a new product was made from the listing
  item_id: string;
  sku: string;
  store: string;
  product_id: number;
  product_title: string;
  listing_count: number;
};

const money = (v: number | string | null) => (v == null ? "—" : Number(v).toFixed(2));

/* ------------------------------------------------------ link-to-product box */

function ProductList({ items, linking, onLink }: { items: Product[]; linking: number | null; onLink: (p: Product) => void }) {
  return (
    <ul className="max-h-80 divide-y divide-gray-100 overflow-y-auto rounded border border-gray-200">
      {items.map((p) => (
        <li key={p.id} className="flex items-center justify-between gap-3 p-3">
          <div className="min-w-0 text-sm">
            <div className="font-medium">{p.title}</div>
            <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-gray-500">
              <span>eBay item # {p.ebay_item_id ?? "—"}</span>
              <span>Custom label SKU {p.custom_label_sku ?? "—"}</span>
              {p.part_number && <span>PN {p.part_number}</span>}
              <span>${money(p.list_price)}</span>
              <span>{p.quantity_on_hand} on hand</span>
              <Badge tone={p.listing_count > 0 ? "green" : "gray"}>{p.listing_count} eBay</Badge>
              {p.label_match === 1 && <Badge tone="green">Same custom label</Badge>}
              {p.mpn_match === 1 && <Badge tone="blue">Part # in title</Badge>}
              {!p.is_published && <Badge tone="amber">Draft</Badge>}
            </div>
          </div>
          <button type="button" className={btnCls} disabled={linking !== null} onClick={() => onLink(p)}>
            {linking === p.id ? "Linking…" : "Link"}
          </button>
        </li>
      ))}
    </ul>
  );
}

function LinkModal({
  listing,
  store,
  onClose,
  onLinked,
}: {
  listing: NewListing;
  store: string;
  onClose: () => void;
  onLinked: (r: LinkResp) => void;
}) {
  const [mode, setMode] = useState<"link" | "create">("link");
  const [q, setQ] = useState("");
  const [items, setItems] = useState<Product[] | null>(null);
  const [searching, setSearching] = useState(false); // true when the list is the result of typed words
  const [loadingList, setLoadingList] = useState(false);
  const [linking, setLinking] = useState<number | null>(null);
  const [error, setError] = useState("");

  // New-product form, filled in from the listing.
  const [newTitle, setNewTitle] = useState(listing.title);
  const [newLabel, setNewLabel] = useState(listing.custom_label ?? "");
  const [newPrice, setNewPrice] = useState(listing.price ? Number(listing.price).toFixed(2) : "");
  const [newQty, setNewQty] = useState("0");
  const [creating, setCreating] = useState(false);

  // The list: likely products for this listing, narrowed by whatever is typed in the box.
  useEffect(() => {
    if (mode !== "link") return;
    let live = true;
    const t = setTimeout(
      async () => {
        setLoadingList(true);
        try {
          const r = await api<SuggestResp>(
            `/ebay-new/suggest?${new URLSearchParams({ label: listing.custom_label ?? "", title: listing.title, q: q.trim() })}`,
          );
          if (live) {
            setItems(r.items);
            setSearching(r.searching);
            setError("");
          }
        } catch (e) {
          if (live) setError(errMsg(e));
        } finally {
          if (live) setLoadingList(false);
        }
      },
      q.trim() ? 300 : 0,
    );
    return () => {
      live = false;
      clearTimeout(t);
    };
  }, [mode, q, listing.custom_label, listing.title]);

  async function linkTo(p: Product) {
    setLinking(p.id);
    setError("");
    try {
      const r = await api<LinkResp>("/ebay-new/link", {
        method: "POST",
        body: { item_id: listing.item_id, product_id: p.id, store },
      });
      onLinked(r);
    } catch (e) {
      setError(errMsg(e));
      setLinking(null);
    }
  }

  async function createProduct() {
    setCreating(true);
    setError("");
    try {
      const r = await api<LinkResp>("/ebay-new/create-product", {
        method: "POST",
        body: {
          item_id: listing.item_id,
          store,
          title: newTitle,
          custom_label_sku: newLabel.trim() || null,
          list_price: newPrice.trim() === "" ? null : Number(newPrice),
          quantity_on_hand: Math.max(0, Math.floor(Number(newQty) || 0)),
        },
      });
      onLinked(r);
    } catch (e) {
      setError(errMsg(e));
      setCreating(false);
    }
  }

  const header = (
    <div className="rounded border border-gray-200 bg-gray-50 p-3 text-sm">
      <div className="font-medium">{listing.title}</div>
      <div className="mt-1 flex flex-wrap gap-x-4 text-xs text-gray-500">
        <span>eBay item # EBAY-{listing.item_id}</span>
        <span>Custom label SKU {listing.custom_label ?? "—"}</span>
        {listing.price && <span>eBay price ${money(listing.price)}</span>}
      </div>
    </div>
  );

  if (mode === "create") {
    return (
      <Modal title="Create a new product from this listing" onClose={onClose}>
        <div className="space-y-3">
          {header}
          <label className="block text-sm font-medium text-gray-700">
            <span className="mb-1 block">Title</span>
            <input className={inputCls} value={newTitle} onChange={(e) => setNewTitle(e.target.value)} />
          </label>
          <label className="block text-sm font-medium text-gray-700">
            <span className="mb-1 block">Custom label SKU (optional)</span>
            <input className={inputCls} value={newLabel} onChange={(e) => setNewLabel(e.target.value)} placeholder="Leave empty if the listing has none" />
          </label>
          <div className="flex gap-3">
            <label className="block flex-1 text-sm font-medium text-gray-700">
              <span className="mb-1 block">Price</span>
              <input className={inputCls} inputMode="decimal" value={newPrice} onChange={(e) => setNewPrice(e.target.value)} />
            </label>
            <label className="block flex-1 text-sm font-medium text-gray-700">
              <span className="mb-1 block">Quantity on hand</span>
              <input className={inputCls} inputMode="numeric" value={newQty} onChange={(e) => setNewQty(e.target.value.replace(/[^0-9]/g, ""))} />
            </label>
          </div>
          <p className="text-xs text-gray-500">
            This makes a new product that is not published on the website yet, and links this eBay listing to it. Photos, description
            and fitment are added on the product page afterwards.
          </p>
          {error && <Notice kind="error">{error}</Notice>}
          <div className="flex gap-2">
            <button type="button" className={btnCls} disabled={creating || !newTitle.trim()} onClick={() => void createProduct()}>
              {creating ? "Creating…" : "Create product and link"}
            </button>
            <button type="button" className={btn2Cls} disabled={creating} onClick={() => setMode("link")}>
              Back to the product list
            </button>
          </div>
        </div>
      </Modal>
    );
  }

  return (
    <Modal title="Link this eBay listing to a product" onClose={onClose}>
      <div className="space-y-3">
        {header}

        <ClearableInput
          className={inputCls}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Narrow the list: title, custom label SKU, eBay item # or part #"
          autoFocus
          onClear={() => setQ("")}
        />

        {error && <Notice kind="error">{error}</Notice>}

        <p className="text-sm font-medium">
          {loadingList && items === null
            ? "Looking for matching products…"
            : searching
            ? `${items?.length ?? 0} product${items?.length === 1 ? "" : "s"} match what you typed`
            : `${items?.length ?? 0} likely product${items?.length === 1 ? "" : "s"} for this listing`}
          {loadingList && items !== null && <span className="ml-2 font-normal text-gray-400">updating…</span>}
        </p>
        {items && items.length > 0 && <ProductList items={items} linking={linking} onLink={(p) => void linkTo(p)} />}
        {items && items.length === 0 && (
          <p className="text-sm text-gray-500">
            {searching
              ? "No products match. Try fewer words."
              : listing.custom_label
              ? `No product has the custom label SKU ${listing.custom_label}, and none look similar. Type above to search all products.`
              : "Nothing looks similar yet. Type above to search all products."}
          </p>
        )}

        <div className="border-t border-gray-200 pt-3">
          <p className="mb-2 text-sm text-gray-600">Not a product on the website yet?</p>
          <button type="button" className={btn2Cls} onClick={() => setMode("create")}>
            Create a new product from this listing…
          </button>
        </div>
      </div>
    </Modal>
  );
}

/* ------------------------------------------------------------------- page */

export default function NewEbayListingsPage() {
  const [store, setStore] = useState("");
  const [deep, setDeep] = useState(false);
  const [checking, setChecking] = useState(false);
  const [result, setResult] = useState<CheckResp | null>(null);
  const [items, setItems] = useState<NewListing[]>([]);
  const [error, setError] = useState("");
  const [linkedMsg, setLinkedMsg] = useState<LinkResp | null>(null);
  const [linking, setLinking] = useState<NewListing | null>(null);
  const [restored, setRestored] = useState(false);

  // Coming back from another admin page: show the same results instead of an empty screen.
  useEffect(() => {
    const saved = loadSaved<{ store: string; deep: boolean; result: CheckResp | null; items: NewListing[] }>("ebay-new");
    if (saved) {
      setStore(saved.store);
      setDeep(saved.deep);
      setResult(saved.result);
      setItems(saved.items);
    }
    setRestored(true);
  }, []);

  useEffect(() => {
    if (restored) saveSaved("ebay-new", { store, deep, result, items });
  }, [restored, store, deep, result, items]);

  async function check() {
    setChecking(true);
    setError("");
    setLinkedMsg(null);
    try {
      const r = await api<CheckResp>(
        `/ebay-new/check?${new URLSearchParams({ deep: String(deep), ...(store.trim() ? { seller: store.trim() } : {}) })}`,
      );
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

      <div className="space-y-4 rounded-lg border border-gray-200 bg-white p-4">
        <label className="block max-w-sm text-sm">
          <span className="mb-1 block font-medium">eBay store name</span>
          <input
            className={inputCls}
            value={store}
            onChange={(e) => setStore(e.target.value)}
            placeholder="eandimotorsports"
            disabled={checking}
          />
        </label>
        <div className="flex flex-wrap items-center gap-4">
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
      </div>

      {error && <Notice kind="error">{error}</Notice>}
      {linkedMsg && (
        <Notice kind="success">
          {linkedMsg.created ? (
            <>
              Created a new product,{" "}
              <Link href={`/admin/products/${linkedMsg.product_id}`} className="font-medium underline">
                {linkedMsg.product_title}
              </Link>
              , and linked EBAY-{linkedMsg.item_id} to it. It is not published yet.
            </>
          ) : (
            <>
              Linked EBAY-{linkedMsg.item_id} to{" "}
              <Link href={`/admin/products/${linkedMsg.product_id}`} className="font-medium underline">
                {linkedMsg.product_title}
              </Link>
              , which now has {linkedMsg.listing_count} eBay item number{linkedMsg.listing_count === 1 ? "" : "s"}.
            </>
          )}
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
                    <th className="px-3 py-2">eBay item #</th>
                    <th className="px-3 py-2">Custom label SKU</th>
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
                      <td className="whitespace-nowrap px-3 py-2 text-gray-600">EBAY-{i.item_id}</td>
                      <td className="whitespace-nowrap px-3 py-2 text-gray-600">{i.custom_label ?? "—"}</td>
                      <td className="max-w-md px-3 py-2">
                        <a href={i.url} target="_blank" rel="noopener noreferrer" className="font-medium hover:underline">
                          {i.title}
                        </a>
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

      {linking && result && <LinkModal listing={linking} store={result.store} onClose={() => setLinking(null)} onLinked={linked} />}
    </div>
  );
}
