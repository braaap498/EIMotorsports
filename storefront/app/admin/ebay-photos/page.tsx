"use client";

import { useState } from "react";
import { Notice, inputCls } from "../../../components/admin/ui";

export default function EbayPhotoImportPage() {
  const [store, setStore] = useState("");

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-semibold">eBay photo import</h1>
        <p className="mt-1 max-w-3xl text-sm text-gray-600">
          Downloads listing photos from eBay into the website&rsquo;s photo storage. Two imports will live here: one for
          listings that are not products yet, and one that fills in any missing photo on every listing.
        </p>
      </div>

      <Notice>
        The import buttons are not connected yet. They are the next piece to build, so for now this tab only reserves the
        spot.
      </Notice>

      <div className="space-y-4 rounded-lg border border-gray-200 bg-white p-4">
        <label className="block max-w-sm text-sm">
          <span className="mb-1 block font-medium">eBay store name</span>
          <input
            className={inputCls}
            value={store}
            onChange={(e) => setStore(e.target.value)}
            placeholder="genuine-oem-parts"
          />
        </label>
        <div className="flex flex-wrap gap-3">
          <button type="button" className="rounded border border-gray-300 px-4 py-2 text-sm text-gray-400" disabled>
            Import photos for listings that are not products yet
          </button>
          <button type="button" className="rounded border border-gray-300 px-4 py-2 text-sm text-gray-400" disabled>
            Import any missing photo for all listings
          </button>
        </div>
      </div>
    </div>
  );
}
