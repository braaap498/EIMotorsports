"use client";

import { useEffect, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Field, Notice, btnCls, inputCls } from "../../../components/admin/ui";
import DescriptionEditor from "../../../components/admin/DescriptionEditor";
import { sanitizeHtml } from "../../../components/admin/sanitizeHtml";

type Page = { key: string; label: string; title: string; body: string; updated_at: string | null };
type Draft = { title: string; body: string };

export default function AdminHelpPage() {
  const [saved, setSaved] = useState<Page[] | null>(null);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [tab, setTab] = useState("shipping");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    api<{ pages: Page[] }>("/help")
      .then((r) => {
        setSaved(r.pages);
        setDrafts(Object.fromEntries(r.pages.map((p) => [p.key, { title: p.title, body: p.body }])));
        setTab((t) => (r.pages.some((p) => p.key === t) ? t : r.pages[0]?.key ?? t));
      })
      .catch((e: unknown) => setError(errMsg(e)));
  }, []);

  const isChanged = (key: string) => {
    const p = saved?.find((x) => x.key === key);
    const d = drafts[key];
    return !!p && !!d && (d.title !== p.title || d.body !== p.body);
  };

  const page = saved?.find((p) => p.key === tab);
  const draft = drafts[tab];

  function edit(patch: Partial<Draft>) {
    setNotice("");
    setDrafts((d) => ({ ...d, [tab]: { ...d[tab], ...patch } }));
  }

  async function save() {
    if (!page || !draft) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const r = await api<{ pages: Page[] }>(`/help/${page.key}`, { method: "PUT", body: draft });
      setSaved(r.pages);
      // Reset only the tab that was saved, so unsaved edits on the other tab are kept.
      const fresh = r.pages.find((p) => p.key === page.key);
      if (fresh) setDrafts((d) => ({ ...d, [page.key]: { title: fresh.title, body: fresh.body } }));
      setNotice(`Saved. The ${page.label} tab on the Help page is updated.`);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Help pages</h1>
        <p className="mt-1 text-sm text-gray-600">
          This is the text on the site&apos;s Help page (<code className="rounded bg-gray-100 px-1">/help</code>), which has a tab for each page below. Changes show right away once you save.
        </p>
      </div>

      {notice && <Notice kind="success">{notice}</Notice>}
      {error && <Notice kind="error">{error}</Notice>}

      {saved && (
        <div role="tablist" aria-label="Help pages" className="flex gap-1 overflow-x-auto border-b border-gray-200">
          {saved.map((p) => (
            <button
              key={p.key}
              type="button"
              role="tab"
              aria-selected={p.key === tab}
              onClick={() => {
                setTab(p.key);
                setNotice("");
              }}
              className={`-mb-px shrink-0 whitespace-nowrap border-b-2 px-4 py-2 text-sm font-medium ${
                p.key === tab ? "border-gray-900 text-gray-900" : "border-transparent text-gray-600 hover:text-gray-900"
              }`}
            >
              {p.label}
              {isChanged(p.key) && <span className="ml-1 text-amber-600" title="Unsaved changes">•</span>}
            </button>
          ))}
        </div>
      )}

      {page && draft && (
        <div className="grid gap-6 lg:grid-cols-2">
          <div className="space-y-4">
            <Field label="Heading">
              <input className={inputCls} value={draft.title} maxLength={120} onChange={(e) => edit({ title: e.target.value })} />
            </Field>
            <div>
              <DescriptionEditor
                key={page.key}
                label="Page text"
                value={draft.body}
                onChange={(html) => edit({ body: html })}
                contentClass="page-content min-h-64 text-base text-gray-700"
              />
              <p className="mt-1 text-xs text-gray-500">Use the style menu for headings, and the buttons for bold, lists and links.</p>
            </div>
            <button type="button" className={btnCls} disabled={busy || !isChanged(tab)} onClick={save}>
              {busy ? "Saving…" : "Save"}
            </button>
            {isChanged(tab) && <span className="ml-3 text-xs text-amber-700">You have unsaved changes.</span>}
            {page.updated_at && !isChanged(tab) && (
              <span className="ml-3 text-xs text-gray-500">Last saved {new Date(page.updated_at).toLocaleString("en-US")}</span>
            )}
          </div>

          <div>
            <p className="mb-2 text-sm font-medium text-gray-700">Preview</p>
            <div className="rounded-lg border border-gray-200 bg-white p-5">
              <h2 className="mb-4 text-2xl font-extrabold text-gray-900">{draft.title || page.label}</h2>
              {draft.body.trim() ? (
                <div className="page-content text-gray-700" dangerouslySetInnerHTML={{ __html: sanitizeHtml(draft.body) }} />
              ) : (
                <p className="text-sm text-gray-500">Nothing written yet. The site shows &ldquo;Check back soon.&rdquo;</p>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
