"use client";

import { useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Field, Notice, btnCls, inputCls } from "../../../components/admin/ui";
import AboutBody from "../../../components/site/AboutBody";
import { shrink } from "../../../components/admin/PhotosPanel";

type About = {
  title: string;
  body: string;
  ebay_feedback_url: string | null;
  ebay_store_url: string | null;
  image_url: string | null;
  updated_at: string | null;
  ebay_feedback: { percent: number; score: number | null; as_of: string | null } | null;
  ebay_status: { configured: boolean; seller: string; last_attempt_at: string | null; last_error: string | null };
};

export default function AdminAboutPage() {
  const [saved, setSaved] = useState<About | null>(null);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [feedbackUrl, setFeedbackUrl] = useState("");
  const [storeUrl, setStoreUrl] = useState("");
  const [imageUrl, setImageUrl] = useState<string | null>(null);
  const [photoBusy, setPhotoBusy] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const [ebay, setEbay] = useState<Pick<About, "ebay_feedback" | "ebay_status"> | null>(null);
  const [ebayBusy, setEbayBusy] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  function apply(a: About) {
    setSaved(a);
    setTitle(a.title);
    setBody(a.body);
    setFeedbackUrl(a.ebay_feedback_url ?? "");
    setStoreUrl(a.ebay_store_url ?? "");
    setImageUrl(a.image_url);
    setEbay({ ebay_feedback: a.ebay_feedback, ebay_status: a.ebay_status });
  }

  // Asks eBay now. The saved numbers only change if eBay answers; a failure is shown below.
  async function refreshEbay() {
    setEbayBusy(true);
    setError("");
    setNotice("");
    try {
      const a = await api<About>("/about/ebay-refresh", { method: "POST" });
      setEbay({ ebay_feedback: a.ebay_feedback, ebay_status: a.ebay_status });
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setEbayBusy(false);
    }
  }

  // The photo saves on its own (it does not wait for the Save button) and leaves any
  // text you are in the middle of typing alone.
  async function photoCall(path: string, body: unknown, done: string) {
    setPhotoBusy(true);
    setError("");
    setNotice("");
    try {
      const a = await api<About>(path, { method: "POST", body });
      setImageUrl(a.image_url);
      setNotice(done);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setPhotoBusy(false);
    }
  }
  async function pickPhoto(files: FileList | null) {
    const f = files?.[0];
    if (fileInput.current) fileInput.current.value = "";
    if (!f) return;
    try {
      const data = await shrink(f);
      await photoCall("/about/photo", { data }, "Photo saved. It is now on the About page.");
    } catch (e) {
      setError(errMsg(e));
    }
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
          <Field label="Photo (optional)">
            <div className="flex flex-wrap items-center gap-3">
              {imageUrl ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={imageUrl} alt="" className="h-24 w-24 rounded border border-gray-200 object-cover" />
              ) : (
                <span className="text-sm text-gray-500">No photo. The page shows just the text.</span>
              )}
              <button type="button" className={btnCls} disabled={photoBusy} onClick={() => fileInput.current?.click()}>
                {photoBusy ? "Working…" : imageUrl ? "Replace photo" : "Add photo"}
              </button>
              {imageUrl && (
                <button
                  type="button"
                  className="rounded border border-gray-300 bg-white px-3 py-2 text-sm font-medium text-gray-800 hover:bg-gray-50 disabled:opacity-40"
                  disabled={photoBusy}
                  onClick={() => void photoCall("/about/photo/remove", undefined, "Photo removed from the About page.")}
                >
                  Remove photo
                </button>
              )}
            </div>
            <input ref={fileInput} type="file" accept="image/jpeg,image/png,image/webp" hidden onChange={(e) => void pickPhoto(e.target.files)} />
          </Field>
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
          <div className="rounded-lg border border-gray-200 bg-white p-4 text-sm">
            <p className="font-medium text-gray-800">eBay feedback on the page</p>
            {ebay?.ebay_feedback ? (
              <p className="mt-1 text-gray-700">
                {ebay.ebay_feedback.percent}% positive
                {ebay.ebay_feedback.score != null ? `, ${ebay.ebay_feedback.score.toLocaleString("en-US")} ratings` : ""}
                {ebay.ebay_feedback.as_of ? ` (saved ${new Date(ebay.ebay_feedback.as_of).toLocaleDateString("en-US")})` : ""}
              </p>
            ) : (
              <p className="mt-1 text-gray-500">No numbers saved yet, so nothing shows on the page.</p>
            )}
            {ebay && !ebay.ebay_status.configured && (
              <p className="mt-1 text-amber-700">
                eBay keys are not set on the API yet (EBAY_CLIENT_ID and EBAY_CLIENT_SECRET), so it cannot read feedback.
              </p>
            )}
            {ebay?.ebay_status.last_error && (
              <p className="mt-1 text-amber-700">
                Last try{ebay.ebay_status.last_attempt_at ? ` (${new Date(ebay.ebay_status.last_attempt_at).toLocaleString("en-US")})` : ""} did not work: {ebay.ebay_status.last_error} The saved numbers above are unchanged.
              </p>
            )}
            <button
              type="button"
              className="mt-2 rounded border border-gray-300 bg-white px-3 py-1.5 text-xs font-medium text-gray-800 hover:bg-gray-50 disabled:opacity-40"
              disabled={ebayBusy || !ebay?.ebay_status.configured}
              onClick={() => void refreshEbay()}
            >
              {ebayBusy ? "Checking eBay…" : `Refresh from eBay (${ebay?.ebay_status.seller ?? "seller"})`}
            </button>
            <p className="mt-1 text-xs text-gray-500">It also refreshes by itself about once a day when someone views the About page.</p>
          </div>
          <button type="button" className={btnCls} disabled={busy || !changed || !saved} onClick={save}>
            {busy ? "Saving…" : "Save"}
          </button>
          {changed && <span className="ml-3 text-xs text-amber-700">You have unsaved changes.</span>}
        </div>

        <div>
          <p className="mb-2 text-sm font-medium text-gray-700">Preview</p>
          <div className="rounded-lg border border-gray-200 bg-white p-5">
            <h2 className="mb-4 text-2xl font-extrabold text-gray-900">{title || "About us"}</h2>
            {imageUrl && (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={imageUrl} alt="" className="mb-4 w-full max-w-xs rounded-lg border border-gray-200 object-cover" />
            )}
            {body.trim() ? <AboutBody text={body} /> : <p className="text-sm text-gray-500">Nothing written yet.</p>}
          </div>
        </div>
      </div>
    </div>
  );
}
