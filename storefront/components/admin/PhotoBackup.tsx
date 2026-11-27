"use client";

import { useRef, useState } from "react";
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
type Summary = { total: number; copied: number; skipped: number; failed: number; firstErrors: string[]; stopped: boolean };

const WORKERS = 6;

export default function PhotoBackup() {
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState("");
  const [summary, setSummary] = useState<Summary | null>(null);
  const [error, setError] = useState("");
  const stop = useRef(false);

  async function start() {
    setError("");
    setSummary(null);
    const picker = (window as unknown as { showDirectoryPicker?: Picker }).showDirectoryPicker;
    if (!picker) {
      setError("This needs Chrome or Edge on a computer. Open the admin in one of those and try again.");
      return;
    }
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
        const res = await fetch(`${list.base_url}/${path.split("/").map(encodeURIComponent).join("/")}`, { cache: "no-store" });
        if (!res.ok) throw new Error(`${res.status} ${path}`);
        const blob = await res.blob();
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

  return (
    <div className="space-y-2 rounded-lg border border-gray-200 bg-white p-4">
      <div className="flex flex-wrap items-center gap-3">
        <button type="button" onClick={() => void start()} disabled={busy} className="rounded bg-black px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">
          {busy ? "Backing up…" : "Back up photos to a folder"}
        </button>
        {busy && (
          <button type="button" onClick={() => (stop.current = true)} className="rounded border border-gray-300 bg-white px-3 py-2 text-sm">
            Stop
          </button>
        )}
      </div>
      <p className="text-xs text-gray-500">
        Pick a folder (make a new one in the picker if you need to). Photos already in it are skipped, so running it again only adds what is missing. Use Chrome
        or Edge on a computer. Windows will not let a browser pick the Downloads folder itself, so choose or create a folder inside it, such as photo_backup.
      </p>
      {progress && <p className="text-sm text-gray-700">{progress}</p>}
      {error && <Notice kind="error">{error}</Notice>}
      {summary && (
        <Notice kind={summary.failed ? "error" : "success"}>
          {summary.stopped ? "Stopped. " : "Done. "}
          {summary.total} photo files in the store: {summary.copied} copied, {summary.skipped} already in the folder
          {summary.failed ? `, ${summary.failed} failed (${summary.firstErrors.join("; ")}). Run it again to retry those.` : "."}
        </Notice>
      )}
    </div>
  );
}
