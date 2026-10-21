"use client";

import { useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { useAccess } from "./AdminAccess";
import { Modal, Notice, btn2Cls } from "./ui";
import PhotoPicker from "./PhotoPicker";

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
// What the server says a delete would do to each photo's stored file.
type PreviewItem = { id: number; image_key: string | null; last_use: boolean; also_on: string[] };

// Phone photos are often 5-10 MB. The browser shrinks each one to at most 2000 px
// on its longest side before sending, which keeps uploads quick. The server checks
// and re-encodes it again, so this is only a convenience.
export async function shrink(file: File, maxSide = 2000): Promise<string> {
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
  onDirtyChange,
}: {
  productId: number;
  photos: Photo[];
  onChange: (photos: Photo[]) => void;
  cardCls: string;
  onDirtyChange?: (dirty: boolean) => void; // true while main-photo / order changes are not saved yet
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
  const [pendingDelete, setPendingDelete] = useState<{ list: Photo[]; items: PreviewItem[] } | null>(null); // waiting for the confirmation dialog
  const [showPicker, setShowPicker] = useState(false);
  // Changes to the main photo and the order are only a draft until "Save changes" is pressed.
  const [draftIds, setDraftIds] = useState<number[] | null>(null);

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
  // The order as saved on the server (main photo first), then the draft laid over it.
  const serverMain = photos.find((p) => p.is_primary) ?? photos[0];
  const serverIds = serverMain ? [serverMain.id, ...photos.filter((p) => p.id !== serverMain.id).map((p) => p.id)] : [];
  const orderedIds = draftIds
    ? [...draftIds.filter((id) => serverIds.includes(id)), ...serverIds.filter((id) => !draftIds.includes(id))]
    : serverIds;
  const ordered = orderedIds.map((id) => photos.find((p) => p.id === id)).filter((p): p is Photo => !!p);
  const mainPhoto = ordered[0];
  const others = ordered.slice(1);
  const dirty = orderedIds.join(",") !== serverIds.join(",");
  const locked = !!busy || dirty; // uploads, replaces and deletes wait until the draft is saved or discarded

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  // Closing or reloading the tab with unsaved changes gets the browser's own warning.
  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

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

  // These only change the draft on screen. Nothing is saved until "Save changes".
  function makeMain(photo: Photo) {
    setDraftIds([photo.id, ...orderedIds.filter((id) => id !== photo.id)]);
  }

  function move(photo: Photo, direction: -1 | 1) {
    const ids = [...orderedIds];
    const i = ids.indexOf(photo.id);
    const j = i + direction;
    if (i < 1 || j < 1 || j >= ids.length) return; // position 0 is the main photo
    [ids[i], ids[j]] = [ids[j], ids[i]];
    setDraftIds(ids);
  }

  async function saveArrangement() {
    const ok = await call("Saving photo order…", `${base}/arrange`, { method: "POST", body: { photo_ids: orderedIds } });
    if (ok) {
      setDraftIds(null);
      setMsg({ kind: "success", text: "Photo order saved." });
    }
  }

  // Asks the server which stored files these deletes would remove, then either
  // shows the warning dialog (some file is about to lose its last use) or a plain confirm.
  async function requestDelete(list: Photo[]) {
    setBusy("Checking…");
    setMsg(null);
    let items: PreviewItem[];
    try {
      const r = await api<{ items: PreviewItem[] }>(`${base}/delete-preview`, {
        method: "POST",
        body: { photo_ids: list.map((p) => p.id) },
      });
      items = r.items;
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
      setBusy(null);
      return;
    }
    setBusy(null);
    // Every delete goes through the in-page dialog below. It is the only way to reach
    // deleteMany, and unlike the browser's confirm box it cannot be switched off.
    setPendingDelete({ list, items });
  }

  function remove(photo: Photo) {
    void requestDelete([photo]);
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
              <button type="button" className={small} disabled={!!busy} onClick={() => makeMain(photo)}>
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
              disabled={locked}
              onClick={() => {
                replaceId.current = photo.id;
                replaceInput.current?.click();
              }}
            >
              Replace
            </button>
            <button type="button" className={`${small} text-red-700`} disabled={locked} onClick={() => remove(photo)}>
              Delete
            </button>
          </div>
        )}
      </li>
    );
  }

  const selectedPhotos = ordered.filter((p) => selected.includes(p.id));

  return (
    <section className={cardCls}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-lg font-semibold">Photos</h2>
        {mayEdit && (
          <div className="flex gap-2">
            <button type="button" className={small} disabled={locked} onClick={() => addInput.current?.click()}>
              + Add photos
            </button>
            <button type="button" className={small} disabled={locked} onClick={() => setShowPicker(true)}>
              + Add from existing photos
            </button>
          </div>
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

      {mayEdit && dirty && (
        <div className="flex flex-wrap items-center gap-3 rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          <span className="font-medium">You have unsaved changes to the main photo or photo order.</span>
          <span className="flex gap-2">
            <button
              type="button"
              className="rounded bg-black px-3 py-1.5 text-xs font-semibold text-white hover:bg-gray-800 disabled:opacity-40"
              disabled={!!busy}
              onClick={() => void saveArrangement()}
            >
              Save changes
            </button>
            <button type="button" className={small} disabled={!!busy} onClick={() => setDraftIds(null)}>
              Discard
            </button>
          </span>
          <span className="text-xs text-amber-800">Adding, replacing and deleting photos is paused until you save or discard.</span>
        </div>
      )}

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
                disabled={locked}
                onClick={() => void requestDelete(selectedPhotos)}
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
      {showPicker && (
        <PhotoPicker
          productId={productId}
          currentKeys={photos.map((p) => p.image_key).filter((k): k is string => !!k)}
          onClose={() => setShowPicker(false)}
          onAdded={(next, added, skipped) => {
            onChange(next);
            setShowPicker(false);
            setMsg({
              kind: "success",
              text: `${added === 1 ? "Photo added" : `${added} photos added`}${skipped ? ` (${skipped} skipped: already on this product)` : ""}.`,
            });
          }}
        />
      )}

      {pendingDelete && (() => {
        const { list, items } = pendingDelete;
        const all = list.length >= photos.length;
        const goneKeys: string[] = Array.from(new Set<string>(items.filter((i) => i.last_use && i.image_key).map((i) => i.image_key as string)));
        const mainGoes = list.some((p) => p.id === mainPhoto?.id);
        const danger = goneKeys.length > 0;
        const title = danger
          ? goneKeys.length === 1
            ? "Permanently delete this stored picture?"
            : `Permanently delete ${goneKeys.length} stored pictures?`
          : list.length === 1
          ? "Remove this photo?"
          : `Remove ${list.length} photos?`;
        return (
          <Modal title={title} onClose={() => setPendingDelete(null)}>
            <div className="space-y-4">
              <div className="flex flex-wrap gap-2">
                {list.slice(0, 8).map((p) => {
                  const src = p.thumb ?? p.source_url;
                  return src ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img key={p.id} src={src} alt="A photo about to be removed" className="h-16 w-16 rounded border border-gray-200 object-cover" />
                  ) : (
                    <div key={p.id} className="flex h-16 w-16 items-center justify-center rounded border border-gray-200 bg-gray-100 text-[10px] text-gray-500">
                      No picture
                    </div>
                  );
                })}
                {list.length > 8 && <div className="flex h-16 items-center text-sm text-gray-600">+{list.length - 8} more</div>}
              </div>

              <div className="space-y-3 text-sm text-gray-800">
                <p>
                  {list.length === 1 ? "1 photo" : `${list.length} photos`} will be removed from this product.
                  {all ? " The product will have no photos left." : ` The other ${photos.length - list.length} stay.`}
                  {mainGoes && !all ? " The next remaining photo becomes the main photo." : ""}
                </p>

                {danger && (
                  <div className="space-y-1">
                    <p className="font-semibold text-red-700">
                      {goneKeys.length === 1 ? "This picture is not used anywhere else" : `These ${goneKeys.length} pictures are not used anywhere else`}, so{" "}
                      {goneKeys.length === 1 ? "it and its thumbnail" : "they and their thumbnails"} will be permanently deleted from storage. This cannot be undone.
                    </p>
                    <ul className="max-h-28 overflow-y-auto rounded border border-red-200 bg-red-50 p-2 font-mono text-xs break-all text-gray-800">
                      {goneKeys.map((k) => (
                        <li key={k}>full/{k}</li>
                      ))}
                    </ul>
                  </div>
                )}

              </div>

              <div className="flex flex-wrap justify-end gap-2">
                <button type="button" className={btn2Cls} autoFocus onClick={() => setPendingDelete(null)}>
                  Cancel, keep {list.length === 1 ? "it" : "them"}
                </button>
                <button
                  type="button"
                  className={
                    danger
                      ? "rounded bg-red-700 px-3 py-2 text-sm font-semibold text-white hover:bg-red-800 disabled:opacity-40"
                      : "rounded bg-black px-3 py-2 text-sm font-semibold text-white hover:bg-gray-800 disabled:opacity-40"
                  }
                  disabled={!!busy}
                  onClick={() => {
                    setPendingDelete(null);
                    void deleteMany(list);
                  }}
                >
                  {danger ? "Delete permanently" : "Remove"}
                  {list.length > 1 ? ` (${list.length})` : ""}
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
