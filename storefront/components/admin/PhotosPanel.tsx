"use client";

import { useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { useAccess } from "./AdminAccess";
import { Modal, Notice, btn2Cls } from "./ui";

export type Photo = {
  id: number;
  source_url: string | null;
  storage_path: string | null;
  image_key?: string | null;
  thumb?: string | null;
  url?: string | null;
  is_primary: boolean;
};

type Listing = { photos: Photo[] };

// Phone photos are often 5-10 MB. The browser shrinks each one to at most 2000 px
// on its longest side before sending, which keeps uploads quick. The server checks
// and re-encodes it again, so this is only a convenience.
async function shrink(file: File, maxSide = 2000): Promise<string> {
  let bmp: ImageBitmap;
  try {
    bmp = await createImageBitmap(file, { imageOrientation: "from-image" });
  } catch {
    throw new Error(`"${file.name}" could not be read. Use a JPG, PNG or WEBP picture.`);
  }
  const scale = Math.min(1, maxSide / Math.max(bmp.width, bmp.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(bmp.width * scale));
  canvas.height = Math.max(1, Math.round(bmp.height * scale));
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("This browser cannot prepare pictures for upload.");
  ctx.fillStyle = "#fff"; // transparent PNGs get a white background
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(bmp, 0, 0, canvas.width, canvas.height);
  bmp.close();
  const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.88));
  if (!blob) throw new Error(`"${file.name}" could not be prepared for upload.`);
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result));
    r.onerror = () => reject(new Error("The picture could not be read."));
    r.readAsDataURL(blob);
  });
  return dataUrl.split(",")[1] ?? "";
}

// What to show for "which file is this?": the name in the storage bucket, or why there is none.
function storageInfo(p: Photo): { file: string; path: string | null; inStorage: boolean } {
  if (p.image_key) {
    return { file: p.image_key.split("/").pop() ?? p.image_key, path: `full/${p.image_key}`, inStorage: true };
  }
  if (p.storage_path) return { file: "Not in storage yet", path: `local: ${p.storage_path}`, inStorage: false };
  return { file: "Not in storage (eBay link only)", path: null, inStorage: false };
}

const HOVER_MS = 700; // how long the pointer must rest on a photo before the file name appears

const small =
  "rounded border border-gray-300 bg-white px-2 py-1 text-xs font-medium text-gray-800 hover:bg-gray-50 disabled:opacity-40";

export default function PhotosPanel({
  productId,
  photos,
  onChange,
  cardCls,
}: {
  productId: number;
  photos: Photo[];
  onChange: (photos: Photo[]) => void;
  cardCls: string;
}) {
  const { can } = useAccess();
  const mayEdit = can("products.photos");
  const addInput = useRef<HTMLInputElement>(null);
  const replaceInput = useRef<HTMLInputElement>(null);
  const replaceId = useRef<number | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const [tipId, setTipId] = useState<number | null>(null); // photo whose file name is showing
  const tipTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [selected, setSelected] = useState<number[]>([]); // photos ticked for bulk delete
  const [pendingDelete, setPendingDelete] = useState<Photo[] | null>(null); // waiting for the confirmation dialog

  useEffect(() => () => { if (tipTimer.current) clearTimeout(tipTimer.current); }, []);

  function tipStart(id: number) {
    if (tipTimer.current) clearTimeout(tipTimer.current);
    tipTimer.current = setTimeout(() => setTipId(id), HOVER_MS);
  }
  function tipStop() {
    if (tipTimer.current) clearTimeout(tipTimer.current);
    setTipId(null);
  }

  const base = `/products/${productId}/photos`;
  const mainPhoto = photos.find((p) => p.is_primary) ?? photos[0];
  const others = photos.filter((p) => p.id !== mainPhoto?.id);

  async function call(label: string, path: string, init: { method: "POST"; body?: unknown }) {
    setBusy(label);
    setMsg(null);
    try {
      const r = await api<Listing>(path, init);
      onChange(r.photos);
      return true;
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
      return false;
    } finally {
      setBusy(null);
    }
  }

  async function addFiles(files: FileList | null) {
    if (!files || files.length === 0) return;
    const list = Array.from(files);
    const problems: string[] = [];
    let added = 0;
    setMsg(null);
    for (let i = 0; i < list.length; i++) {
      setBusy(`Uploading photo ${i + 1} of ${list.length}…`);
      try {
        const data = await shrink(list[i]);
        const r = await api<Listing>(base, { method: "POST", body: { data } });
        onChange(r.photos);
        added++;
      } catch (e) {
        problems.push(`${list[i].name}: ${errMsg(e)}`);
      }
    }
    setBusy(null);
    if (problems.length) setMsg({ kind: "error", text: problems.join("  ") });
    else setMsg({ kind: "success", text: added === 1 ? "Photo added." : `${added} photos added.` });
    if (addInput.current) addInput.current.value = "";
  }

  async function replaceFile(files: FileList | null) {
    const id = replaceId.current;
    const file = files?.[0];
    if (replaceInput.current) replaceInput.current.value = "";
    if (!id || !file) return;
    setBusy("Replacing photo…");
    setMsg(null);
    try {
      const data = await shrink(file);
      const r = await api<Listing>(`${base}/${id}/replace`, { method: "POST", body: { data } });
      onChange(r.photos);
      setMsg({ kind: "success", text: "Photo replaced." });
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    } finally {
      setBusy(null);
    }
  }

  function move(photo: Photo, direction: -1 | 1) {
    const ids = others.map((p) => p.id);
    const i = ids.indexOf(photo.id);
    const j = i + direction;
    if (i < 0 || j < 0 || j >= ids.length) return;
    [ids[i], ids[j]] = [ids[j], ids[i]];
    void call("Saving order…", `${base}/order`, { method: "POST", body: { photo_ids: ids } });
  }

  function remove(photo: Photo) {
    // The last photo gets its own, stronger warning (see the dialog below).
    if (photos.length <= 1) {
      setPendingDelete([photo]);
      return;
    }
    const extra = photo.is_primary && photos.length > 1 ? " The next photo will become the main photo." : "";
    if (!window.confirm(`Remove this photo from the product?${extra}`)) return;
    void call("Removing photo…", `${base}/${photo.id}/delete`, { method: "POST", body: {} });
  }

  function toggle(id: number) {
    setSelected((sel) => (sel.includes(id) ? sel.filter((i) => i !== id) : [...sel, id]));
  }

  // Deletes one after another with the same route as a single delete, so the
  // "keep files another row still uses" check runs for every photo.
  async function deleteMany(list: Photo[]) {
    setMsg(null);
    let done = 0;
    try {
      for (const ph of list) {
        setBusy(`Removing photo ${done + 1} of ${list.length}…`);
        const r = await api<Listing>(`${base}/${ph.id}/delete`, { method: "POST", body: {} });
        onChange(r.photos);
        setSelected((sel) => sel.filter((i) => i !== ph.id));
        done++;
      }
      setMsg({ kind: "success", text: done === 1 ? "Photo removed." : `${done} photos removed.` });
    } catch (e) {
      setMsg({ kind: "error", text: `Removed ${done} of ${list.length}. ${errMsg(e)}` });
    } finally {
      setBusy(null);
    }
  }

  function renderTile(photo: Photo, index: number) {
    const src = photo.thumb ?? photo.source_url;
    const isMain = photo.id === mainPhoto?.id;
    const posInOthers = others.findIndex((p) => p.id === photo.id);
    const picked = selected.includes(photo.id);
    return (
      <li key={photo.id} className="w-36 space-y-1.5">
        <div
          className={`relative overflow-hidden rounded border-2 ${
            picked ? "border-red-600" : isMain ? "border-blue-600" : "border-gray-200"
          }`}
          onMouseEnter={() => tipStart(photo.id)}
          onMouseLeave={tipStop}
        >
          {src ? (
            // eslint-disable-next-line @next/next/no-img-element
            <a href={photo.url ?? src} target="_blank" rel="noopener noreferrer" aria-label="Open full size">
              <img src={src} alt={`Product photo ${index + 1}`} className="h-36 w-36 object-cover" />
            </a>
          ) : (
            <div className="flex h-36 w-36 items-center justify-center bg-gray-100 text-xs text-gray-500">No picture</div>
          )}
          {isMain && (
            <span className="absolute left-1 top-1 rounded bg-blue-600 px-1.5 py-0.5 text-xs font-semibold text-white">
              Main photo
            </span>
          )}
          {mayEdit && (
            <label className="absolute right-1 top-1 flex h-6 w-6 cursor-pointer items-center justify-center rounded bg-white/90 shadow" title="Select for deleting">
              <input type="checkbox" className="h-4 w-4 cursor-pointer" checked={picked} disabled={!!busy} onChange={() => toggle(photo.id)} aria-label="Select this photo" />
            </label>
          )}
        </div>
        {mayEdit && (
          <div className="flex flex-wrap gap-1">
            {!isMain && (
              <button type="button" className={small} disabled={!!busy} onClick={() => void call("Saving…", `${base}/${photo.id}/main`, { method: "POST", body: {} })}>
                Make main
              </button>
            )}
            {!isMain && (
              <>
                <button type="button" className={small} disabled={!!busy || posInOthers <= 0} onClick={() => move(photo, -1)} title="Move earlier" aria-label="Move earlier">
                  ◀
                </button>
                <button type="button" className={small} disabled={!!busy || posInOthers >= others.length - 1} onClick={() => move(photo, 1)} title="Move later" aria-label="Move later">
                  ▶
                </button>
              </>
            )}
            <button
              type="button"
              className={small}
              disabled={!!busy}
              onClick={() => {
                replaceId.current = photo.id;
                replaceInput.current?.click();
              }}
            >
              Replace
            </button>
            <button type="button" className={`${small} text-red-700`} disabled={!!busy} onClick={() => remove(photo)}>
              Delete
            </button>
          </div>
        )}
      </li>
    );
  }

  const ordered = mainPhoto ? [mainPhoto, ...others] : [];
  const selectedPhotos = ordered.filter((p) => selected.includes(p.id));

  return (
    <section className={cardCls}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-lg font-semibold">Photos</h2>
        {mayEdit && (
          <button type="button" className={small} disabled={!!busy} onClick={() => addInput.current?.click()}>
            + Add photos
          </button>
        )}
      </div>
      <input ref={addInput} type="file" accept="image/jpeg,image/png,image/webp" multiple hidden onChange={(e) => void addFiles(e.target.files)} />
      <input ref={replaceInput} type="file" accept="image/jpeg,image/png,image/webp" hidden onChange={(e) => void replaceFile(e.target.files)} />

      {busy && <p className="text-sm text-gray-600">{busy}</p>}
      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}

      {ordered.length > 0 && (() => {
        const tipPhoto = tipId != null ? photos.find((p) => p.id === tipId) : null;
        const info = tipPhoto ? storageInfo(tipPhoto) : null;
        return (
          <div className="h-14 overflow-hidden rounded border border-gray-200 bg-gray-50 px-3 py-2 text-sm" aria-live="polite">
            {info ? (
              <>
                <p className="truncate font-mono font-semibold text-gray-900" title={info.file}>{info.file}</p>
                {info.path && <p className="truncate font-mono text-xs text-gray-500" title={info.path}>{info.path}</p>}
              </>
            ) : (
              <p className="truncate leading-5 text-gray-500">Rest the pointer on a photo to see its file name in storage.</p>
            )}
          </div>
        );
      })()}

      {mayEdit && ordered.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          {selectedPhotos.length > 0 && <span className="font-medium text-gray-800">{selectedPhotos.length} selected</span>}
          {selectedPhotos.length < ordered.length && (
            <button type="button" className={small} disabled={!!busy} onClick={() => setSelected(ordered.map((p) => p.id))}>
              Select all
            </button>
          )}
          {selectedPhotos.length > 0 && (
            <>
              <button type="button" className={small} disabled={!!busy} onClick={() => setSelected([])}>
                Clear
              </button>
              <button
                type="button"
                className="rounded border border-red-300 bg-white px-2 py-1 text-xs font-semibold text-red-700 hover:bg-red-50 disabled:opacity-40"
                disabled={!!busy}
                onClick={() => setPendingDelete(selectedPhotos)}
              >
                Delete selected ({selectedPhotos.length})
              </button>
            </>
          )}
        </div>
      )}

      {ordered.length === 0 ? (
        <p className="text-sm text-gray-500">No photos yet.{mayEdit ? " Use “Add photos” to upload some." : ""}</p>
      ) : (
        <ul className="flex flex-wrap gap-4">
          {ordered.map((p, i) => renderTile(p, i))}
        </ul>
      )}
      {pendingDelete && (() => {
        const list = pendingDelete;
        const all = list.length >= photos.length;
        const keys = Array.from(new Set(list.map((p) => p.image_key).filter((k): k is string => !!k)));
        const mainGoes = list.some((p) => p.id === mainPhoto?.id);
        const title = all ? (list.length === 1 ? "Delete the last photo?" : `Delete ALL ${list.length} photos?`) : `Delete ${list.length} photos?`;
        return (
          <Modal title={title} onClose={() => setPendingDelete(null)}>
            <div className="space-y-4">
              <div className="flex flex-wrap gap-2">
                {list.slice(0, 8).map((p) => {
                  const src = p.thumb ?? p.source_url;
                  return src ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img key={p.id} src={src} alt="A photo about to be deleted" className="h-16 w-16 rounded border border-gray-200 object-cover" />
                  ) : (
                    <div key={p.id} className="flex h-16 w-16 items-center justify-center rounded border border-gray-200 bg-gray-100 text-[10px] text-gray-500">
                      No picture
                    </div>
                  );
                })}
                {list.length > 8 && <div className="flex h-16 items-center text-sm text-gray-600">+{list.length - 8} more</div>}
              </div>
              <div className="space-y-2 text-sm text-gray-800">
                {all ? (
                  <>
                    <p className="font-semibold text-red-700">
                      {list.length === 1 ? "This is the only photo left on this product." : "This removes every photo on this product."}
                    </p>
                    <p>After this, the product will have no pictures at all.</p>
                  </>
                ) : (
                  <p>
                    {list.length} photos will be removed from this product. The other {photos.length - list.length} stay.
                    {mainGoes ? " The next remaining photo becomes the main photo." : ""}
                  </p>
                )}
                {keys.length > 0 ? (
                  <>
                    <p>
                      {keys.length === 1 ? "This stored picture and its thumbnail" : `These ${keys.length} stored pictures and their thumbnails`} will be
                      permanently deleted from storage, unless another product uses the same picture. This cannot be undone.
                    </p>
                    <ul className="max-h-28 overflow-y-auto rounded border border-gray-200 bg-gray-50 p-2 font-mono text-xs break-all text-gray-700">
                      {keys.map((k) => (
                        <li key={k}>full/{k}</li>
                      ))}
                    </ul>
                  </>
                ) : (
                  <p>None of these are in your storage (they are only links), so nothing will be deleted from storage.</p>
                )}
              </div>
              <div className="flex flex-wrap justify-end gap-2">
                <button type="button" className={btn2Cls} autoFocus onClick={() => setPendingDelete(null)}>
                  Cancel, keep {list.length === 1 ? "it" : "them"}
                </button>
                <button
                  type="button"
                  className="rounded bg-red-700 px-3 py-2 text-sm font-semibold text-white hover:bg-red-800 disabled:opacity-40"
                  disabled={!!busy}
                  onClick={() => {
                    setPendingDelete(null);
                    void deleteMany(list);
                  }}
                >
                  Delete permanently{list.length > 1 ? ` (${list.length})` : ""}
                </button>
              </div>
            </div>
          </Modal>
        );
      })()}
      <p className="text-xs text-gray-500">
        The main photo is the one shoppers see first, on the product page and in search results.
        {mayEdit ? " JPG, PNG or WEBP. Large pictures are shrunk automatically." : " You do not have permission to change photos."}
      </p>
    </section>
  );
}
