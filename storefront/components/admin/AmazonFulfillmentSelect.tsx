"use client";

/* Fulfillment dropdown for the Amazon new-listing screen (AmazonNew.tsx).
   It does not store anything itself: it reads and rewrites the draft's "extra attributes" JSON text, setting or clearing
   fulfillment_availability. The backend (amazon_new.py) treats a draft as FBA when that key holds an AMAZON_* channel code. */

export type Fulfillment = "mfn" | "fba";

const FBA_ENTRY = [{ fulfillment_channel_code: "AMAZON_NA" }];

/** Reads the mode out of the JSON text. Returns null when the text is not a JSON object (the dropdown is then disabled). */
export function fulfillmentOf(json: string | null | undefined): Fulfillment | null {
  const t = (json ?? "").trim();
  if (!t) return "mfn";
  try {
    const o = JSON.parse(t);
    if (!o || typeof o !== "object" || Array.isArray(o)) return null;
    const fa = (o as Record<string, unknown>).fulfillment_availability;
    const isFba = Array.isArray(fa) && fa.some((x) => String((x as Record<string, unknown> | null)?.fulfillment_channel_code ?? "").toUpperCase().startsWith("AMAZON"));
    return isFba ? "fba" : "mfn";
  } catch {
    return null;
  }
}

/** Returns the JSON text with the chosen mode applied. Every other key the owner typed is kept. */
export function withFulfillment(json: string | null | undefined, mode: Fulfillment): string {
  const t = (json ?? "").trim();
  let o: Record<string, unknown> = {};
  if (t) {
    const parsed = JSON.parse(t);   // callers only get here when fulfillmentOf() was not null
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) o = parsed as Record<string, unknown>;
  }
  if (mode === "fba") {
    o.fulfillment_availability = FBA_ENTRY;
  } else {
    delete o.fulfillment_availability;   // the backend then sends the default merchant-fulfilled entry with the draft's quantity
  }
  return Object.keys(o).length ? JSON.stringify(o, null, 2) : "";
}

export default function AmazonFulfillmentSelect({ value, onChange, disabled }: { value: string | null | undefined; onChange: (next: string) => void; disabled?: boolean }) {
  const mode = fulfillmentOf(value);
  return (
    <label className="block text-sm">
      <span className="mb-1 block text-xs font-semibold uppercase text-gray-500">Fulfillment</span>
      <select
        className="w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-sm disabled:bg-gray-100"
        disabled={disabled || mode === null}
        value={mode ?? "mfn"}
        onChange={(e) => onChange(withFulfillment(value, e.target.value as Fulfillment))}
      >
        <option value="mfn">Fulfilled by us (drop ship or our stock)</option>
        <option value="fba">Fulfilled by Amazon (FBA)</option>
      </select>
      {mode === null && <span className="mt-1 block text-xs text-red-600">The extra attributes box is not valid JSON, so fix it before changing this.</span>}
      {mode === "fba" && <span className="mt-1 block text-xs text-gray-500">No quantity is sent. Send the stock to Amazon from Seller Central after the listing is live.</span>}
    </label>
  );
}
