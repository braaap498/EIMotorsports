"use client";

import { useEffect, useRef, useState } from "react";
import { Zip, ZipPassThrough } from "fflate";
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
// sku_folder is the by_sku folder name for that photo (for example EBAY-800799374319, or a product SKU
// with "/" turned into "xx" like 9144PSxx125777xx120216).
type ManifestPhoto = {
  id: number;
  product_id: number | null;
  storage_path: string;
  source_url: string | null;
  is_primary: boolean;
  sort_order: number;
  created_at: string;
  image_key: string; // like "89/8986f63da8b6....jpg" (the file is full/<image_key>)
  sku_folder: string;
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

  // by_sku/<sku_folder>/<NN>_<first 8 of the photo's hash>.jpg, where NN is the photo's position (1, 2, 3...)
  const bySku = new Map<string, string[]>();
  if (withBySku) {
    const seen = new Set<string>();
    for (const p of m.photos) {
      if (!p.image_key || !p.sku_folder) continue;
      const file = p.image_key.split("/").pop() as string;
      const dot = file.lastIndexOf(".");
      const hash = (dot > 0 ? file.slice(0, dot) : file).slice(0, 8);
      const ext = dot > 0 ? file.slice(dot + 1) : "jpg";
      const folder = p.sku_folder.replace(/[\\/:*?"<>|]/g, "_");
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
  const [withBySku, setWithBySku] = useState(true);
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
      const paths = list.paths;
      const { index, note } = await loadIndex(withBySku);

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
        let blob: Blob | null = await existing(path);
        const already = !!blob;
        if (!blob) {
          blob = await fetchPhoto(list.base_url, path);
          await writeAt(path, blob);
        }
        let extra = 0;
        for (const t of index.bySku.get(path) ?? []) {
          if (await existing(t)) continue;
          await writeAt(t, blob);
          extra++;
        }
        if (already) s.skipped++;
        else s.copied++;
        s.bySku += extra;
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
      const paths = list.paths;
      const { index, note } = await loadIndex(withBySku);
      const d = new Date();
      const stamp = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

      const s: Summary = { total: paths.length, copied: 0, skipped: 0, failed: 0, firstErrors: [], stopped: false, zipParts: 0, bySku: 0, indexNote: note };

      // One ZIP being built. Photos are already compressed, so they are stored as they are.
      let chunks: Uint8Array[] = [];
      let zip: Zip;
      let dataBytes = 0;
      let filesInPart = 0;
      let zipError: unknown = null;

      const startPart = () => {
        chunks = [];
        dataBytes = 0;
        filesInPart = 0;
        zip = new Zip((err, chunk) => {
          if (err) zipError = err;
          else chunks.push(chunk);
        });
      };

      const addEntry = (path: string, data: Uint8Array) => {
        const entry = new ZipPassThrough(path);
        zip.add(entry);
        entry.push(data, true);
        filesInPart++;
        dataBytes += data.length;
      };

      const finishPart = () => {
        if (filesInPart === 0) return;
        zip.end();
        if (zipError) throw zipError;
        s.zipParts = (s.zipParts ?? 0) + 1;
        saveBlob(new Blob(chunks as BlobPart[], { type: "application/zip" }), `photo_backup_${stamp}_part${s.zipParts}.zip`);
        startPart();
      };

      startPart();
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
            addEntry(path, data);
            s.copied++;
            for (const t of index.bySku.get(path) ?? []) {
              addEntry(t, data);
              s.bySku++;
            }
            if (dataBytes >= ZIP_PART_BYTES) finishPart();
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
          <input type="checkbox" checked={withBySku} onChange={(e) => setWithBySku(e.target.checked)} disabled={busy} />
          Also make by_sku folders
        </label>
      </div>
      {canPickFolder ? (
        <p className="text-xs text-gray-500">
          Pick a folder (make a new one in the picker if you need to). Photos already in it are skipped, so running it again only adds what is missing. Along with
          the full and thumb photos it writes product_photos.csv and ebay_label_import.csv (rewritten each run) and, if ticked, a by_sku folder with a copy of each
          photo under its SKU, which takes extra disk space. Windows will not let a browser pick the Downloads folder itself, so choose or create a folder inside
          it, such as photo_backup.
        </p>
      ) : (
        <p className="text-xs text-gray-500">
          This browser cannot write to a folder, so the photos download as one or more ZIP files (photo_backup_date_part1.zip, and so on), with the two CSV files in
          part 1. It downloads every photo each time. Unzip them all into the same backup folder, choosing merge if asked, with no extra folder level, and Chrome or
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
