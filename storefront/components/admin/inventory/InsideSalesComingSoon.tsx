"use client";

import { useState } from "react";

/* Inventory > Pick & ship > Inside sales: placeholder until in-store (walk-in / phone) sales are built.

   The picture is  public/inside-sales.jpg  in the storefront project. It already carries its own
   "E&I Motorsports Coming Soon!!" banner, so nothing is drawn on top of it.
   If that file is ever missing, a plain dark panel with the store name and COMING SOON shows instead. */
export default function InsideSalesComingSoon() {
  const [noPhoto, setNoPhoto] = useState(false);
  return (
    <div className="w-full space-y-3">
      <div className="relative aspect-video max-h-[80vh] min-h-[24rem] w-full overflow-hidden rounded-xl border border-gray-300 bg-gradient-to-b from-gray-700 to-gray-950 shadow-lg">
        {!noPhoto ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src="/inside-sales.jpg"
            alt="E&I Motorsports showroom, coming soon"
            className="h-full w-full object-cover"
            onError={() => setNoPhoto(true)}
          />
        ) : (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-6 text-center">
            <div className="text-2xl font-extrabold tracking-[0.35em] text-orange-400 sm:text-4xl">{"E&I MOTORSPORTS"}</div>
            <div className="-rotate-3 rounded-lg border-4 border-white bg-red-600 px-6 py-3 text-3xl font-extrabold tracking-wider text-white shadow-2xl sm:px-12 sm:py-6 sm:text-6xl">
              COMING SOON!!!!
            </div>
          </div>
        )}
      </div>
      <p className="text-center text-sm text-gray-500">Walk-in and phone sales will be recorded here.</p>
    </div>
  );
}
