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
type Summary = {
  total: number;
  copied: number;
  skipped: number;
  failed: number;
  firstErrors: string[];
  stopped: boolean;
  zipParts?: number; // set when the backup was downloaded as ZIP files (Firefox, Safari)
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

export default function PhotoBackup() {
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState("");
  const [summary, setSummary] = useState<Summary | null>(null);
  const [error, setError] = useState("");
  const [canPickFolder, setCanPickFolder] = useState(true); // checked in the browser after load
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

      const s: Summary = { total: paths.length, copied: 0, skipped: 0, failed: 0, firstErrors: [], stopped: false };
      let next = 0;
      let done = 0;

      async function one(path: string) {
        const parts = path.split("/");
        const file = parts.pop() as string;
        const dir = await dirFor(parts);
        try {
          const existing = await (await dir.getFileHandle(file)).getFile();
          if (existing.size > 0) {
            s.skipped++;
            return;
          }
        } catch {
          // not there yet: copy it
        }
        const blob = await fetchPhoto(list.base_url, path);
        const w = await (await dir.getFileHandle(file, { create: true })).createWritable();
        try {
          await w.write(blob);
          await w.close();
        } catch (e) {
          await w.abort?.().catch(() => {});
          throw e;
        }
        s.copied++;
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
      const d = new Date();
      const stamp = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

      const s: Summary = { total: paths.length, copied: 0, skipped: 0, failed: 0, firstErrors: [], stopped: false, zipParts: 0 };

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

      const finishPart = () => {
        if (filesInPart === 0) return;
        zip.end();
        if (zipError) throw zipError;
        s.zipParts = (s.zipParts ?? 0) + 1;
        saveBlob(new Blob(chunks as BlobPart[], { type: "application/zip" }), `photo_backup_${stamp}_part${s.zipParts}.zip`);
        startPart();
      };

      startPart();

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
            const entry = new ZipPassThrough(path);
            zip.add(entry);
            entry.push(data, true);
            filesInPart++;
            dataBytes += data.length;
            s.copied++;
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
      </div>
      {canPickFolder ? (
        <p className="text-xs text-gray-500">
          Pick a folder (make a new one in the picker if you need to). Photos already in it are skipped, so running it again only adds what is missing. Windows
          will not let a browser pick the Downloads folder itself, so choose or create a folder inside it, such as photo_backup.
        </p>
      ) : (
        <p className="text-xs text-gray-500">
          This browser cannot write to a folder, so the photos download as one or more ZIP files (photo_backup_date_part1.zip, and so on). It downloads every photo
          each time. Unzip them all into the same backup folder, choosing merge if asked, with no extra folder level, and Chrome or Edge will treat those photos as
          already backed up.
        </p>
      )}
      {progress && <p className="text-sm text-gray-700">{progress}</p>}
      {error && <Notice kind="error">{error}</Notice>}
      {summary && (
        <Notice kind={summary.failed ? "error" : "success"}>
          {summary.stopped ? "Stopped. " : "Done. "}
          {summary.zipParts !== undefined
            ? `${summary.total} photo files in the store: ${summary.copied} saved into ${summary.zipParts} ZIP file${summary.zipParts === 1 ? "" : "s"}`
            : `${summary.total} photo files in the store: ${summary.copied} copied, ${summary.skipped} already in the folder`}
          {summary.failed ? `, ${summary.failed} failed (${summary.firstErrors.join("; ")}). Run it again to retry those.` : "."}
        </Notice>
      )}
    </div>
  );
}
