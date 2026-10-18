"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import ProductImage from "./ProductImage";

export type GalleryPhoto = { url: string; thumb: string; fallback: string | null };

/**
 * Product photos: a large photo with a strip of thumbnails. Clicking any photo
 * opens a full-screen viewer where you can step through every photo with the
 * arrow buttons, the keyboard (left / right / Esc) or a swipe on a phone.
 */
export default function ProductGallery({ photos, title }: { photos: GalleryPhoto[]; title: string }) {
  const count = photos.length;
  const [rawIndex, setIndex] = useState(0);
  // If the photo list ever gets shorter, never point past the end of it.
  const index = count ? Math.min(rawIndex, count - 1) : 0;
  const [open, setOpen] = useState(false);
  const touchX = useRef<number | null>(null);
  const closeBtn = useRef<HTMLButtonElement>(null);
  const lastFocus = useRef<HTMLElement | null>(null);
  const activeThumb = useRef<HTMLButtonElement>(null);

  const go = useCallback((i: number) => setIndex(((i % count) + count) % count), [count]);

  const openAt = (i: number) => {
    lastFocus.current = document.activeElement as HTMLElement | null;
    setIndex(i);
    setOpen(true);
  };
  const close = useCallback(() => {
    setOpen(false);
    lastFocus.current?.focus();
  }, []);

  // While the viewer is open: keyboard, no page scrolling behind it, and
  // the neighbouring photos are fetched ahead of time so stepping is instant.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
      else if (e.key === "ArrowLeft") go(index - 1);
      else if (e.key === "ArrowRight") go(index + 1);
    };
    window.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeBtn.current?.focus();
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [open, index, close, go]);

  useEffect(() => {
    if (!open || count < 2) return;
    [index - 1, index + 1].forEach((i) => {
      const img = new Image();
      img.src = photos[((i % count) + count) % count].url;
    });
  }, [open, index, count, photos]);

  useEffect(() => {
    if (open) activeThumb.current?.scrollIntoView?.({ block: "nearest", inline: "center" });
  }, [open, index]);

  if (count === 0) {
    return (
      <div className="flex aspect-square items-center justify-center rounded bg-gray-100 text-gray-400">No photo</div>
    );
  }

  const current = photos[index];

  return (
    <div>
      {/* page view: the current photo plus every thumbnail */}
      <button
        type="button"
        onClick={() => openAt(index)}
        className="relative block w-full cursor-zoom-in"
        aria-label={`View larger photo of ${title}`}
      >
        <ProductImage
          key={current.url}
          src={current.url}
          fallback={current.fallback}
          alt={title}
          className="w-full rounded border border-gray-200"
        />
        {count > 1 && (
          <span className="absolute bottom-2 right-2 rounded bg-black/70 px-2 py-0.5 text-xs font-semibold text-white">
            {index + 1} / {count}
          </span>
        )}
      </button>

      {count > 1 && (
        <div className="mt-3 grid grid-cols-4 gap-2 sm:grid-cols-5">
          {photos.map((ph, i) => (
            <button
              key={ph.url}
              type="button"
              onClick={() => openAt(i)}
              aria-label={`View photo ${i + 1} of ${count}`}
              className={`overflow-hidden rounded border-2 ${i === index ? "border-[#06bde7]" : "border-gray-200 hover:border-gray-400"}`}
            >
              <ProductImage src={ph.thumb} fallback={ph.fallback} className="aspect-square w-full object-cover" />
            </button>
          ))}
        </div>
      )}

      {/* full-screen viewer */}
      {open && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label={`Photos of ${title}`}
          className="fixed inset-0 z-50 flex flex-col bg-black/95"
          onTouchStart={(e) => {
            touchX.current = e.touches[0].clientX;
          }}
          onTouchEnd={(e) => {
            if (touchX.current == null || count < 2) return;
            const dx = e.changedTouches[0].clientX - touchX.current;
            touchX.current = null;
            if (Math.abs(dx) > 50) go(index + (dx < 0 ? 1 : -1));
          }}
        >
          <div className="flex items-center justify-between px-4 py-3 text-white">
            <span className="text-sm font-medium" aria-live="polite">
              {index + 1} / {count}
            </span>
            <button
              ref={closeBtn}
              type="button"
              onClick={close}
              aria-label="Close"
              className="rounded px-3 py-1 text-2xl leading-none hover:bg-white/15"
            >
              ×
            </button>
          </div>

          <div
            className="relative flex min-h-0 flex-1 items-center justify-center px-12"
            onClick={(e) => {
              if (e.target === e.currentTarget) close();
            }}
          >
            {count > 1 && (
              <button
                type="button"
                onClick={() => go(index - 1)}
                aria-label="Previous photo"
                className="absolute left-2 top-1/2 -translate-y-1/2 rounded-full bg-white/15 px-3 py-2 text-3xl leading-none text-white hover:bg-white/30"
              >
                ‹
              </button>
            )}
            <ProductImage
              key={current.url}
              src={current.url}
              fallback={current.fallback}
              alt={`${title} (photo ${index + 1} of ${count})`}
              className="max-h-full max-w-full object-contain"
            />
            {count > 1 && (
              <button
                type="button"
                onClick={() => go(index + 1)}
                aria-label="Next photo"
                className="absolute right-2 top-1/2 -translate-y-1/2 rounded-full bg-white/15 px-3 py-2 text-3xl leading-none text-white hover:bg-white/30"
              >
                ›
              </button>
            )}
          </div>

          {count > 1 && (
            <div className="flex gap-2 overflow-x-auto px-4 py-3">
              {photos.map((ph, i) => (
                <button
                  key={ph.url}
                  ref={i === index ? activeThumb : undefined}
                  type="button"
                  onClick={() => go(i)}
                  aria-label={`Show photo ${i + 1}`}
                  className={`h-16 w-16 shrink-0 overflow-hidden rounded border-2 ${i === index ? "border-[#06bde7]" : "border-transparent opacity-60 hover:opacity-100"}`}
                >
                  <ProductImage src={ph.thumb} fallback={ph.fallback} className="h-full w-full object-cover" />
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
