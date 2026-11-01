// Reads eBay "All orders" report files in the browser (a .csv, or the .zip eBay gives you) and sends them to the
// API in batches. Only the columns the analytics need leave the browser: buyer names, emails and addresses do not.

import { api } from "../../../lib/adminApi";

export const KEEP = [
  "Sales Record Number", "Order Number", "Buyer Username", "Buyer State", "Buyer Country",
  "Item Number", "Item Title", "Custom Label", "Sold Via Promoted Listings", "Quantity", "Sold For",
  "Shipping And Handling", "Seller Collected Tax", "eBay Collected Tax", "Total Price", "Sale Date",
  "Paid On Date", "Shipped On Date", "Feedback Received", "Shipping Service", "Transaction ID",
  "Global Shipping Program",
];

export type ReportFile = { name: string; store: string | null; orders: number; rows: Record<string, string>[] };

/** RFC 4180-style CSV parser (quoted fields, doubled quotes, newlines inside quotes, BOM). */
export function parseCsv(text: string): string[][] {
  if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
  const out: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i++;
        } else quoted = false;
      } else field += c;
    } else if (c === '"') quoted = true;
    else if (c === ",") {
      row.push(field);
      field = "";
    } else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(field);
      field = "";
      out.push(row);
      row = [];
    } else field += c;
  }
  if (field !== "" || row.length) {
    row.push(field);
    out.push(row);
  }
  return out;
}

/** One report's text -> the rows we keep, plus the seller id eBay prints at the bottom of the file. */
export function readReport(name: string, text: string): ReportFile {
  const grid = parseCsv(text);
  const hi = grid.findIndex((r) => r.includes("Order Number"));
  if (hi < 0) throw new Error(`${name}: this doesn't look like an eBay "All orders" report (no "Order Number" column).`);
  const header = grid[hi];
  const idx = KEEP.map((k) => [k, header.indexOf(k)] as const).filter(([, i]) => i >= 0);
  let store: string | null = null;
  const rows: Record<string, string>[] = [];
  for (const r of grid.slice(hi + 1)) {
    const first = (r[0] ?? "").trim();
    const m = /^Seller ID\s*:\s*(.+)$/i.exec(first);
    if (m) {
      store = m[1].trim().toLowerCase();
      continue;
    }
    const num = (r[header.indexOf("Order Number")] ?? "").trim();
    if (!num || /record\(s\) downloaded/i.test(num)) continue;
    const o: Record<string, string> = {};
    for (const [k, i] of idx) o[k] = r[i] ?? "";
    rows.push(o);
  }
  const orders = new Set(rows.map((r) => r["Order Number"])).size;
  return { name, store, orders, rows };
}

// ---- .zip support (eBay delivers the report zipped). Uses the browser's built-in inflate; no library.
async function inflateRaw(data: Uint8Array): Promise<Uint8Array> {
  const ds = new DecompressionStream("deflate-raw");
  const stream = new Blob([data as unknown as BlobPart]).stream().pipeThrough(ds);
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

export async function unzipCsvs(buf: ArrayBuffer): Promise<{ name: string; text: string }[]> {
  const u8 = new Uint8Array(buf);
  const dv = new DataView(buf);
  let eocd = -1;
  for (let i = u8.length - 22; i >= Math.max(0, u8.length - 65557); i--) {
    if (dv.getUint32(i, true) === 0x06054b50) {
      eocd = i;
      break;
    }
  }
  if (eocd < 0) throw new Error("That zip file couldn't be read.");
  const count = dv.getUint16(eocd + 10, true);
  let p = dv.getUint32(eocd + 16, true);
  const dec = new TextDecoder("utf-8");
  const files: { name: string; text: string }[] = [];
  for (let n = 0; n < count; n++) {
    if (dv.getUint32(p, true) !== 0x02014b50) break;
    const method = dv.getUint16(p + 10, true);
    const csize = dv.getUint32(p + 20, true);
    const nlen = dv.getUint16(p + 28, true);
    const xlen = dv.getUint16(p + 30, true);
    const clen = dv.getUint16(p + 32, true);
    const local = dv.getUint32(p + 42, true);
    const name = dec.decode(u8.subarray(p + 46, p + 46 + nlen));
    p += 46 + nlen + xlen + clen;
    if (!/\.csv$/i.test(name) || name.startsWith("__MACOSX")) continue;
    const start = local + 30 + dv.getUint16(local + 26, true) + dv.getUint16(local + 28, true);
    const raw = u8.subarray(start, start + csize);
    const bytes = method === 0 ? raw : method === 8 ? await inflateRaw(raw) : null;
    if (!bytes) throw new Error(`${name}: unsupported zip compression.`);
    files.push({ name: name.split("/").pop() || name, text: dec.decode(bytes) });
  }
  return files;
}

/** A dropped/selected file (.csv or .zip) -> one report per CSV inside. */
export async function readFiles(files: File[]): Promise<ReportFile[]> {
  const out: ReportFile[] = [];
  for (const f of files) {
    if (/\.zip$/i.test(f.name)) {
      for (const c of await unzipCsvs(await f.arrayBuffer())) out.push(readReport(c.name, c.text));
    } else out.push(readReport(f.name, await f.text()));
  }
  return out;
}

export type ImportResult = { received: number; added: number; updated: number; skipped: number };

/** Send one report's rows in batches, never splitting an order across two batches. */
export async function sendReport(
  rep: ReportFile,
  store: string,
  onProgress: (done: number, total: number) => void,
  batchRows = 1500
): Promise<ImportResult> {
  const byOrder = new Map<string, Record<string, string>[]>();
  for (const r of rep.rows) {
    const k = r["Order Number"];
    const g = byOrder.get(k);
    if (g) g.push(r);
    else byOrder.set(k, [r]);
  }
  const total: ImportResult = { received: 0, added: 0, updated: 0, skipped: 0 };
  let batch: Record<string, string>[] = [];
  let sentOrders = 0;
  let pendingOrders = 0;
  const flush = async () => {
    if (!batch.length) return;
    const res = await api<ImportResult>("/analytics/ebay/import", {
      method: "POST",
      body: { store, source_file: rep.name, rows: batch },
    });
    total.received += res.received;
    total.added += res.added;
    total.updated += res.updated;
    total.skipped += res.skipped;
    sentOrders += pendingOrders;
    onProgress(sentOrders, byOrder.size);
    batch = [];
    pendingOrders = 0;
  };
  for (const g of byOrder.values()) {
    if (batch.length + g.length > batchRows) await flush();
    batch.push(...g);
    pendingOrders++;
  }
  await flush();
  return total;
}
