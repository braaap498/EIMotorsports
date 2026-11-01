"use client";

import { useEffect, useState } from "react";
import PartsCleanupPage from "../cleanup/page";
import NewEbayListingsPage from "../ebay-new/page";

/* "Merge and link products": the two existing tools as tabs on one page.
   The merge tool and the link tool are the unchanged pages from /admin/cleanup and /admin/ebay-new. */

type Tab = "merge" | "link";

const TABS: { id: Tab; label: string }[] = [
  { id: "merge", label: "Merge existing products" },
  { id: "link", label: "Link new eBay listings to products" },
];

export default function MergeAndLinkPage() {
  const [tab, setTab] = useState<Tab>("merge");
  // A tab is built the first time it is opened, then kept (hidden) so work in progress is not lost when switching.
  const [opened, setOpened] = useState<Record<Tab, boolean>>({ merge: true, link: false });

  useEffect(() => {
    if (window.location.hash === "#link") pick("link", false);
  }, []);

  function pick(t: Tab, writeHash = true) {
    setTab(t);
    setOpened((o) => ({ ...o, [t]: true }));
    if (writeHash) window.history.replaceState(null, "", t === "link" ? "#link" : window.location.pathname);
  }

  return (
    <div>
      <div className="mb-4 flex gap-2" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            onClick={() => pick(t.id)}
            className={`rounded-full border px-4 py-1.5 text-sm font-medium ${
              tab === t.id ? "border-black bg-black text-white" : "border-gray-300 bg-white text-gray-700 hover:border-gray-500"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div hidden={tab !== "merge"}>{opened.merge && <PartsCleanupPage />}</div>
      <div hidden={tab !== "link"}>{opened.link && <NewEbayListingsPage />}</div>
    </div>
  );
}
