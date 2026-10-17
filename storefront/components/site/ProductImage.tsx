"use client";

import { useEffect, useState } from "react";

/**
 * A product photo that falls back to a second address if the first one
 * fails to load (for example the old eBay link while a file is missing).
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

  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={failed && fallback ? fallback : src}
      alt={alt}
      className={className}
      loading="lazy"
      onError={() => {
        if (!failed && fallback) setFailed(true);
      }}
    />
  );
}
