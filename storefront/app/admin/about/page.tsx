"use client";

import { useEffect, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Field, Notice, btnCls, inputCls } from "../../../components/admin/ui";
import AboutBody from "../../../components/site/AboutBody";

type About = {
  title: string;
  body: string;
  ebay_feedback_url: string | null;
  ebay_store_url: string | null;
  updated_at: string | null;
};

export default function AdminAboutPage() {
  const [saved, setSaved] = useState<About | null>(null);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [feedbackUrl, setFeedbackUrl] = useState("");
  const [storeUrl, setStoreUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  function apply(a: About) {
    setSaved(a);
    setTitle(a.title);
    setBody(a.body);
    setFeedbackUrl(a.ebay_feedback_url ?? "");
    setStoreUrl(a.ebay_store_url ?? "");
  }

  useEffect(() => {
    api<About>("/about")
      .then(apply)
      .catch((e: unknown) => setError(errMsg(e)));
  }, []);

  const changed =
    !!saved &&
    (title !== saved.title ||
      body !== saved.body ||
      feedbackUrl.trim() !== (saved.ebay_feedback_url ?? "") ||
      storeUrl.trim() !== (saved.ebay_store_url ?? ""));

  async function save() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const a = await api<About>("/about", {
        method: "PUT",
        body: { title, body, ebay_feedback_url: feedbackUrl.trim() || null, ebay_store_url: storeUrl.trim() || null },
      });
      apply(a);
      setNotice("Saved. The About page on the site is updated.");
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">About page</h1>
        <p className="mt-1 text-sm text-gray-600">
          This is the text on the site&apos;s About page (<code className="rounded bg-gray-100 px-1">/about</code>). Changes show right away once you save.
        </p>
      </div>

      {notice && <Notice kind="success">{notice}</Notice>}
      {error && <Notice kind="error">{error}</Notice>}

      <div className="grid gap-6 lg:grid-cols-2">
        <div className="space-y-4">
          <Field label="Page title">
            <input className={inputCls} value={title} maxLength={120} onChange={(e) => setTitle(e.target.value)} />
          </Field>
          <Field label="Page text">
            <textarea
              className={`${inputCls} h-80 font-mono text-sm`}
              value={body}
              maxLength={20000}
              onChange={(e) => setBody(e.target.value)}
              placeholder={"## Who we are\nWe are a powersports parts shop...\n\n## Why shop with us\n- Fast shipping\n- Parts that fit"}
            />
            <p className="mt-1 text-xs text-gray-500">
              Start a line with <code className="rounded bg-gray-100 px-1">## </code> for a heading and <code className="rounded bg-gray-100 px-1">- </code> for a bullet. A blank line starts a new paragraph.
            </p>
          </Field>
          <Field label="Link to your eBay feedback page (optional)">
            <input className={inputCls} value={feedbackUrl} maxLength={500} onChange={(e) => setFeedbackUrl(e.target.value)} placeholder="https://www.ebay.com/fdbk/feedback_profile/your-ebay-name" />
          </Field>
          <Field label="Link to your eBay store (optional)">
            <input className={inputCls} value={storeUrl} maxLength={500} onChange={(e) => setStoreUrl(e.target.value)} placeholder="https://www.ebay.com/str/your-store" />
          </Field>
          <button type="button" className={btnCls} disabled={busy || !changed || !saved} onClick={save}>
            {busy ? "Saving…" : "Save"}
          </button>
          {changed && <span className="ml-3 text-xs text-amber-700">You have unsaved changes.</span>}
        </div>

        <div>
          <p className="mb-2 text-sm font-medium text-gray-700">Preview</p>
          <div className="rounded-lg border border-gray-200 bg-white p-5">
            <h2 className="mb-4 text-2xl font-extrabold text-gray-900">{title || "About us"}</h2>
            {body.trim() ? <AboutBody text={body} /> : <p className="text-sm text-gray-500">Nothing written yet.</p>}
          </div>
        </div>
      </div>
    </div>
  );
}
