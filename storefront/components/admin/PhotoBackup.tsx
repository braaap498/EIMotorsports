"use client";

import { useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Notice } from "./ui";

// The parts of the browser's folder-access API this button uses (Chrome and Edge on a computer).
type Writable = { write: (data: Blob) => Promise<void>; close: () => Promise<void>; abort?: () => Promise<void> };
type FileHandle = { getFile: () => Promise<File>; createWritable: () => Promise<Writable> };
type DirHandle = {
  name: string;
  getDirectoryHandle: (name: string, opts?: { create?: boolean }) => Promise<DirHandle>;
  getFileHandle: (name: string, opts?: { create?: boolean }) => Promise<FileHandle>;
};
type Picker = (opts?: { mode?: "read" | "readwrite" }) => Promise<DirHandle>;

type ListResult = { base_url: string; count: number; paths: string[] };

// What /storage-backup/manifest must return. One row per product_photos row, plus the eBay map.
// sku is the product's EI number (its SKU), or null when the photo has no product yet. The by_sku folder is
// named after it, with "/" turned into "xx" (9144PS/125777/120216 becomes 9144PSxx125777xx120216). Photos with
// no product fall back to the listing folder in storage_path, such as EBAY-800799374319.
type ManifestPhoto = {
  id: number;
  product_id: number | null;
  storage_path: string;
  source_url: string | null;
  is_primary: boolean;
  sort_order: number;
  created_at: string;
  image_key: string; // like "89/8986f63da8b6....jpg" (the file is full/<image_key>)
  sku: string | null;
};
type Manifest = { photos: ManifestPhoto[]; ebay: { item_number: string | number; custom_label_sku: string | number }[] };

type Summary = {
  total: number;
  copied: number;
  skipped: number;
  failed: number;
  firstErrors: string[];
  stopped: boolean;
  zipParts?: number; // set when the backup was downloaded as ZIP files (Firefox, Safari)
  bySku: number; // copies written into by_sku
  indexNote: string; // set when the CSV files / by_sku could not be made
};

const WORKERS = 6;
// Each ZIP is saved once it holds about this much photo data, so the browser never has to
// keep the whole library in memory. A big library comes down as several ZIPs.
const ZIP_PART_BYTES = 400 * 1024 * 1024;
const ZIP_PART_FILES = 20000; // a ZIP also starts a new part at this many files

// ---- a small ZIP writer (stored, not compressed: photos are already compressed), so no package is needed ----

const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

function crc32(data: Uint8Array): number {
  let c = 0xffffffff;
  for (let i = 0; i < data.length; i++) c = CRC_TABLE[(c ^ data[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

class ZipBuilder {
  private body: BlobPart[] = [];
  private central: BlobPart[] = [];
  private offset = 0;
  private count = 0;
  private enc = new TextEncoder();
  private time: number;
  private date: number;

  constructor() {
    const d = new Date();
    this.time = (d.getHours() << 11) | (d.getMinutes() << 5) | (d.getSeconds() >> 1);
    this.date = ((d.getFullYear() - 1980) << 9) | ((d.getMonth() + 1) << 5) | d.getDate();
  }

  add(path: string, data: Uint8Array) {
    const name = this.enc.encode(path);
    const crc = crc32(data);
    const local = new Uint8Array(30 + name.length);
    const l = new DataView(local.buffer);
    l.setUint32(0, 0x04034b50, true);
    l.setUint16(4, 20, true);
    l.setUint16(6, 0x0800, true); // file names are UTF-8
    l.setUint16(8, 0, true); // stored
    l.setUint16(10, this.time, true);
    l.setUint16(12, this.date, true);
    l.setUint32(14, crc, true);
    l.setUint32(18, data.length, true);
    l.setUint32(22, data.length, true);
    l.setUint16(26, name.length, true);
    local.set(name, 30);

    const cen = new Uint8Array(46 + name.length);
    const c = new DataView(cen.buffer);
    c.setUint32(0, 0x02014b50, true);
    c.setUint16(4, 20, true);
    c.setUint16(6, 20, true);
    c.setUint16(8, 0x0800, true);
    c.setUint16(10, 0, true);
    c.setUint16(12, this.time, true);
    c.setUint16(14, this.date, true);
    c.setUint32(16, crc, true);
    c.setUint32(20, data.length, true);
    c.setUint32(24, data.length, true);
    c.setUint16(28, name.length, true);
    c.setUint32(42, this.offset, true);
    cen.set(name, 46);

    this.body.push(local as BlobPart, data as BlobPart);
    this.central.push(cen);
    this.offset += local.length + data.length;
    this.count++;
  }

  finish(): Blob {
    let cenSize = 0;
    for (const c of this.central) cenSize += (c as Uint8Array).length;
    const end = new Uint8Array(22);
    const e = new DataView(end.buffer);
    e.setUint32(0, 0x06054b50, true);
    e.setUint16(8, this.count, true);
    e.setUint16(10, this.count, true);
    e.setUint32(12, cenSize, true);
    e.setUint32(16, this.offset, true);
    return new Blob([...this.body, ...this.central, end as BlobPart], { type: "application/zip" });
  }
}

function getPicker(): Picker | undefined {
  return (window as unknown as { showDirectoryPicker?: Picker }).showDirectoryPicker;
}

function pad(n: number) {
  return String(n).padStart(2, "0");
}

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

async function fetchPhoto(base: string, path: string): Promise<Blob> {
  const res = await fetch(`${base}/${path.split("/").map(encodeURIComponent).join("/")}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`${res.status} ${path}`);
  return res.blob();
}

// ---- index files (same columns and layout as the old script's output) ----

function csvCell(v: unknown): string {
  if (v === null || v === undefined) return "";
  const s = typeof v === "boolean" ? (v ? "True" : "False") : String(v);
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function toCsv(header: string[], rows: unknown[][]): string {
  return [header, ...rows].map((r) => r.map(csvCell).join(",")).join("\r\n") + "\r\n";
}

type IndexFiles = {
  csvs: { path: string; text: string }[];
  // photo path in the store (full/xx/hash.jpg) -> the by_sku copies to make from it
  bySku: Map<string, string[]>;
};

function buildIndex(m: Manifest, withBySku: boolean): IndexFiles {
  const csvs = [
    {
      path: "product_photos.csv",
      text: toCsv(
        ["id", "product_id", "storage_path", "source_url", "is_primary", "sort_order", "created_at", "image_key"],
        m.photos.map((p) => [p.id, p.product_id, p.storage_path, p.source_url, p.is_primary, p.sort_order, p.created_at, p.image_key])
      ),
    },
    {
      path: "ebay_label_import.csv",
      text: toCsv(["item_number", "custom_label_sku"], m.ebay.map((e) => [e.item_number, e.custom_label_sku])),
    },
  ];

  // by_sku/<EI number>/<NN>_<first 8 of the photo's hash>.jpg, where NN is the photo's position (1, 2, 3...)
  const bySku = new Map<string, string[]>();
  if (withBySku) {
    const seen = new Set<string>();
    for (const p of m.photos) {
      if (!p.image_key) continue;
      const sku = p.sku?.trim();
      const listing = p.storage_path.startsWith("full/") ? "" : p.storage_path.split("/")[0];
      const name = sku ? sku.replace(/\//g, "xx") : listing;
      if (!name) continue;
      const file = p.image_key.split("/").pop() as string;
      const dot = file.lastIndexOf(".");
      const hash = (dot > 0 ? file.slice(0, dot) : file).slice(0, 8);
      const ext = dot > 0 ? file.slice(dot + 1) : "jpg";
      const folder = name.replace(/[\\:*?"<>|]/g, "_");
      const target = `by_sku/${folder}/${pad(p.sort_order + 1)}_${hash}.${ext}`;
      if (seen.has(target)) continue;
      seen.add(target);
      const src = `full/${p.image_key}`;
      const list = bySku.get(src);
      if (list) list.push(target);
      else bySku.set(src, [target]);
    }
  }
  return { csvs, bySku };
}

async function loadIndex(withBySku: boolean): Promise<{ index: IndexFiles; note: string }> {
  try {
    const m = await api<Manifest>("/storage-backup/manifest");
    return { index: buildIndex(m, withBySku), note: "" };
  } catch (e) {
    return {
      index: { csvs: [], bySku: new Map() },
      note: `The CSV files and by_sku folders were skipped because the photo index could not be loaded (${errMsg(e)}).`,
    };
  }
}

export default function PhotoBackup() {
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState("");
  const [summary, setSummary] = useState<Summary | null>(null);
  const [error, setError] = useState("");
  const [canPickFolder, setCanPickFolder] = useState(true); // checked in the browser after load
  const [bySkuOnly, setBySkuOnly] = useState(false);
  const stop = useRef(false);

  useEffect(() => {
    setCanPickFolder(!!getPicker());
  }, []);

  // Chrome / Edge: copy straight into a folder the person picks.
  async function backupToFolder(picker: Picker) {
    let root: DirHandle;
    try {
      root = await picker({ mode: "readwrite" });
    } catch {
      return; // the picker was closed without choosing a folder
    }
    stop.current = false;
    setBusy(true);
    try {
      setProgress("Getting the photo list…");
      const list = await api<ListResult>("/storage-backup/list");
      const { index, note } = await loadIndex(bySkuOnly);
      if (bySkuOnly && index.bySku.size === 0) throw new Error(note || "None of the by_sku photos matched the photo list, so there is nothing to copy.");
      // In by_sku-only mode just the full photos that belong to a SKU are fetched; full and thumb are not saved.
      const paths = bySkuOnly ? list.paths.filter((p) => index.bySku.has(p)) : list.paths;

      const dirs = new Map<string, Promise<DirHandle>>();
      const dirFor = (parts: string[]): Promise<DirHandle> => {
        const key = parts.join("/");
        let p = dirs.get(key);
        if (!p) {
          p = parts.length === 0 ? Promise.resolve(root) : dirFor(parts.slice(0, -1)).then((d) => d.getDirectoryHandle(parts[parts.length - 1], { create: true }));
          dirs.set(key, p);
        }
        return p;
      };

      // The file at this path in the folder if it is there and not empty, otherwise null.
      async function existing(path: string): Promise<File | null> {
        const parts = path.split("/");
        const file = parts.pop() as string;
        try {
          const f = await (await (await dirFor(parts)).getFileHandle(file)).getFile();
          return f.size > 0 ? f : null;
        } catch {
          return null;
        }
      }
      async function writeAt(path: string, blob: Blob) {
        const parts = path.split("/");
        const file = parts.pop() as string;
        const dir = await dirFor(parts);
        const w = await (await dir.getFileHandle(file, { create: true })).createWritable();
        try {
          await w.write(blob);
          await w.close();
        } catch (e) {
          await w.abort?.().catch(() => {});
          throw e;
        }
      }

      const s: Summary = { total: paths.length, copied: 0, skipped: 0, failed: 0, firstErrors: [], stopped: false, bySku: 0, indexNote: note };

      // The CSV files are small and always rewritten, so they match the database as of this run.
      for (const f of index.csvs) await writeAt(f.path, new Blob([f.text], { type: "text/csv" }));

      let next = 0;
      let done = 0;

      async function one(path: string) {
        if (bySkuOnly) {
          const missing: string[] = [];
          for (const t of index.bySku.get(path) ?? []) if (!(await existing(t))) missing.push(t);
          if (missing.length === 0) {
            s.skipped++;
            return;
          }
          const blob = await fetchPhoto(list.base_url, path); // fetched once, written to every SKU folder that needs it
          for (const t of missing) await writeAt(t, blob);
          s.copied++;
          s.bySku += missing.length;
          return;
        }
        let blob: Blob | null = await existing(path);
        const already = !!blob;
        if (!blob) {
          blob = await fetchPhoto(list.base_url, path);
          await writeAt(path, blob);
        }
        if (already) s.skipped++;
        else s.copied++;
      }

      async function worker() {
        while (!stop.current && next < paths.length) {
          const path = paths[next++];
          try {
            await one(path);
          } catch (e) {
            try {
              await one(path); // one more try
            } catch (e2) {
              s.failed++;
              if (s.firstErrors.length < 5) s.firstErrors.push(e2 instanceof Error ? e2.message : String(e2 ?? e));
            }
          }
          done++;
          if (done % 25 === 0 || done === paths.length) setProgress(`${done} of ${paths.length} checked. ${s.copied} copied, ${s.skipped} already there${s.failed ? `, ${s.failed} failed` : ""}.`);
        }
      }

      await Promise.all(Array.from({ length: WORKERS }, () => worker()));
      s.stopped = stop.current && done < paths.length;
      setSummary(s);
      setProgress("");
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  // Firefox / Safari: no folder access, so download the photos as one or more ZIP files.
  // Every ZIP uses the same folder layout as the folder backup, so unzipping them all into
  // one folder (merging) gives a folder that Chrome / Edge will recognise and top up.
  async function backupToZip() {
    stop.current = false;
    setBusy(true);
    try {
      setProgress("Getting the photo list…");
      const list = await api<ListResult>("/storage-backup/list");
      const { index, note } = await loadIndex(bySkuOnly);
      if (bySkuOnly && index.bySku.size === 0) throw new Error(note || "None of the by_sku photos matched the photo list, so there is nothing to copy.");
      // In by_sku-only mode just the full photos that belong to a SKU are fetched; full and thumb are not saved.
      const paths = bySkuOnly ? list.paths.filter((p) => index.bySku.has(p)) : list.paths;
      const d = new Date();
      const stamp = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

      const s: Summary = { total: paths.length, copied: 0, skipped: 0, failed: 0, firstErrors: [], stopped: false, zipParts: 0, bySku: 0, indexNote: note };

      // One ZIP being built. Photos are already compressed, so they are stored as they are.
      let zip = new ZipBuilder();
      let dataBytes = 0;
      let filesInPart = 0;

      const startPart = () => {
        zip = new ZipBuilder();
        dataBytes = 0;
        filesInPart = 0;
      };

      const addEntry = (path: string, data: Uint8Array) => {
        zip.add(path, data);
        filesInPart++;
        dataBytes += data.length;
      };

      const finishPart = () => {
        if (filesInPart === 0) return;
        s.zipParts = (s.zipParts ?? 0) + 1;
        saveBlob(zip.finish(), `photo_backup_${stamp}_part${s.zipParts}.zip`);
        startPart();
      };

      const enc = new TextEncoder();
      for (const f of index.csvs) addEntry(f.path, enc.encode(f.text)); // the CSV files go in the first ZIP

      let next = 0;
      let done = 0;

      async function worker() {
        while (!stop.current && next < paths.length) {
          const path = paths[next++];
          try {
            let blob: Blob;
            try {
              blob = await fetchPhoto(list.base_url, path);
            } catch {
              blob = await fetchPhoto(list.base_url, path); // one more try
            }
            const data = new Uint8Array(await blob.arrayBuffer());
            // Adding to the ZIP and saving a full one happen together, with no waiting between.
            if (!bySkuOnly) addEntry(path, data);
            s.copied++;
            for (const t of index.bySku.get(path) ?? []) {
              addEntry(t, data);
              s.bySku++;
            }
            if (dataBytes >= ZIP_PART_BYTES || filesInPart >= ZIP_PART_FILES) finishPart();
          } catch (e) {
            s.failed++;
            if (s.firstErrors.length < 5) s.firstErrors.push(e instanceof Error ? e.message : String(e));
          }
          done++;
          if (done % 25 === 0 || done === paths.length) setProgress(`${done} of ${paths.length} photos fetched${s.failed ? `, ${s.failed} failed` : ""}.`);
        }
      }

      await Promise.all(Array.from({ length: WORKERS }, () => worker()));
      s.stopped = stop.current && done < paths.length;
      setProgress("Saving the last ZIP…");
      finishPart();
      setSummary(s);
      setProgress("");
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  async function start() {
    setError("");
    setSummary(null);
    const picker = getPicker();
    if (picker) await backupToFolder(picker);
    else await backupToZip();
  }

  return (
    <div className="space-y-2 rounded-lg border border-gray-200 bg-white p-4">
      <div className="flex flex-wrap items-center gap-3">
        <button type="button" onClick={() => void start()} disabled={busy} className="rounded bg-black px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">
          {busy ? "Backing up…" : canPickFolder ? "Back up photos to a folder" : "Download photos as ZIP"}
        </button>
        {busy && (
          <button type="button" onClick={() => (stop.current = true)} className="rounded border border-gray-300 bg-white px-3 py-2 text-sm">
            Stop
          </button>
        )}
        <label className="flex items-center gap-2 text-sm text-gray-700">
          <input type="checkbox" checked={bySkuOnly} onChange={(e) => setBySkuOnly(e.target.checked)} disabled={busy} />
          Only make by_sku folders (skip full and thumb)
        </label>
      </div>
      {canPickFolder ? (
        <p className="text-xs text-gray-500">
          Pick a folder (make a new one in the picker if you need to). Photos already in it are skipped, so running it again only adds what is missing. It writes
          product_photos.csv and ebay_label_import.csv (rewritten each run) and either the full and thumb photos, or, if the box is ticked, only a by_sku folder
          with each photo under its SKU (a photo used by several SKUs is saved once per SKU). Windows will not let a browser pick the Downloads folder itself, so choose or create a folder inside
          it, such as photo_backup.
        </p>
      ) : (
        <p className="text-xs text-gray-500">
          This browser cannot write to a folder, so the photos download as one or more ZIP files (photo_backup_date_part1.zip, and so on), with the two CSV files in
          part 1. It downloads every photo each time. If the box is ticked, the ZIPs hold only the by_sku folders. Unzip them all into the same backup folder, choosing merge if asked, with no extra folder level, and Chrome or
          Edge will treat those photos as already backed up.
        </p>
      )}
      {progress && <p className="text-sm text-gray-700">{progress}</p>}
      {error && <Notice kind="error">{error}</Notice>}
      {summary && (
        <Notice kind={summary.failed || summary.indexNote ? "error" : "success"}>
          {summary.stopped ? "Stopped. " : "Done. "}
          {summary.zipParts !== undefined
            ? `${summary.total} photo files in the store: ${summary.copied} saved into ${summary.zipParts} ZIP file${summary.zipParts === 1 ? "" : "s"}`
            : `${summary.total} photo files in the store: ${summary.copied} copied, ${summary.skipped} already in the folder`}
          {summary.bySku ? `, plus ${summary.bySku} by_sku copies` : ""}
          {summary.failed ? `, ${summary.failed} failed (${summary.firstErrors.join("; ")}). Run it again to retry those.` : "."}
          {summary.indexNote ? ` ${summary.indexNote}` : ""}
        </Notice>
      )}
    </div>
  );
}
