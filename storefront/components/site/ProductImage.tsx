"use client";

import { useEffect, useState } from "react";

/**
 * A product photo that falls back to a second address if the first one
 * fails to load (for example the old eBay link while a file is missing).
 *
 * Where a box is meant to be filled (the className has "object-cover": shop cards, search, home,
 * cart, header and gallery thumbnails) the WHOLE photo is shown, never cropped. The space left over
 * on the sides or top is filled with a blurred, enlarged copy of the same photo, so on white studio
 * shots it blends in and you don't see any bars.
 */
export default function ProductImage({
  src,
  fallback,
  alt = "",
  className,
}: {
  src: string;
  fallback?: string | null;
  alt?: string;
  className?: string;
}) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [src]);

  const shown = failed && fallback ? fallback : src;
  const onError = () => {
    if (!failed && fallback) setFailed(true);
  };

  if (!className || !className.includes("object-cover")) {
    return (
      // eslint-disable-next-line @next/next/no-img-element
      <img src={shown} alt={alt} className={className} loading="lazy" onError={onError} />
    );
  }

  const box = className.replace(/\bobject-cover\b/g, "").trim();
  return (
    <div className={`relative overflow-hidden bg-white ${box}`}>
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={shown}
        alt=""
        aria-hidden="true"
        className="absolute inset-0 h-full w-full scale-125 object-cover opacity-90 blur-xl"
        loading="lazy"
      />
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={shown} alt={alt} className="relative h-full w-full object-contain" loading="lazy" onError={onError} />
    </div>
  );
}
