"use client";

import { useEffect, useState, type KeyboardEvent } from "react";
import { storeApi } from "../../lib/storeApi";

type HelpPage = { key: string; label: string; title: string; body: string; updated_at: string | null };

const wrap = "mx-auto max-w-3xl px-4 py-10";

// The open tab is kept in the address (/help#returns) so a link can open a specific tab.
const hashKey = () => (typeof window === "undefined" ? "" : window.location.hash.replace(/^#/, ""));

export default function HelpPage() {
  const [pages, setPages] = useState<HelpPage[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [active, setActive] = useState("");

  useEffect(() => {
    let cancelled = false;
    storeApi<{ pages: HelpPage[] }>("/help")
      .then((r) => !cancelled && setPages(r.pages))
      .catch(() => !cancelled && setFailed(true));
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const read = () => setActive(hashKey());
    read();
    window.addEventListener("hashchange", read);
    window.addEventListener("popstate", read);
    return () => {
      window.removeEventListener("hashchange", read);
      window.removeEventListener("popstate", read);
    };
  }, []);

  if (failed) {
    return (
      <main className={wrap}>
        <h1 className="text-3xl font-extrabold text-gray-900">Help</h1>
        <p className="mt-3 text-gray-600">This page could not be loaded right now. Please try again in a moment.</p>
      </main>
    );
  }
  if (!pages) return <main className={`${wrap} text-gray-600`}>Loading…</main>;

  const current = pages.find((p) => p.key === active) ?? pages[0];

  function open(key: string) {
    setActive(key);
    window.history.replaceState(null, "", `#${key}`);
  }

  // Left/Right arrows move between tabs, as people expect from tabs.
  function onKeyDown(e: KeyboardEvent<HTMLButtonElement>, i: number) {
    if (!pages || (e.key !== "ArrowRight" && e.key !== "ArrowLeft")) return;
    e.preventDefault();
    const next = pages[(i + (e.key === "ArrowRight" ? 1 : pages.length - 1)) % pages.length];
    open(next.key);
    document.getElementById(`help-tab-${next.key}`)?.focus();
  }

  return (
    <main className={wrap}>
      <h1 className="text-3xl font-extrabold text-gray-900">Help</h1>

      <div role="tablist" aria-label="Help topics" className="mt-5 flex gap-1 overflow-x-auto border-b border-gray-200">
        {pages.map((p, i) => {
          const on = p.key === current.key;
          return (
            <button
              key={p.key}
              type="button"
              role="tab"
              id={`help-tab-${p.key}`}
              aria-selected={on}
              aria-controls="help-panel"
              tabIndex={on ? 0 : -1}
              onClick={() => open(p.key)}
              onKeyDown={(e) => onKeyDown(e, i)}
              className={`-mb-px shrink-0 whitespace-nowrap border-b-2 px-4 py-2.5 text-sm font-semibold ${
                on ? "border-[#06bde7] text-gray-900" : "border-transparent text-gray-600 hover:text-gray-900"
              }`}
            >
              {p.label}
            </button>
          );
        })}
      </div>

      <section role="tabpanel" id="help-panel" aria-labelledby={`help-tab-${current.key}`} className="mt-6">
        <h2 className="text-2xl font-extrabold text-gray-900">{current.title}</h2>
        <div className="mt-4">
          {current.body.trim() ? (
            // The server sends cleaned HTML (the same kind product descriptions use).
            <div className="page-content text-gray-700" dangerouslySetInnerHTML={{ __html: current.body }} />
          ) : (
            <p className="text-gray-600">Check back soon.</p>
          )}
        </div>
      </section>
    </main>
  );
}
