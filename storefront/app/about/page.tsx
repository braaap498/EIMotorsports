"use client";

import { useEffect, useState } from "react";
import AboutBody from "../../components/site/AboutBody";
import CustomerFeedback from "../../components/site/CustomerFeedback";
import { storeApi } from "../../lib/storeApi";

type About = {
  title: string;
  body: string;
  ebay_feedback_url: string | null;
  ebay_store_url: string | null;
  image_url: string | null;
  // Last saved eBay numbers (a snapshot, not a live reading). null until the first one is saved.
  ebay_feedback: { percent: number; score: number | null; as_of: string | null } | null;
};

const wrap = "mx-auto max-w-3xl px-4 py-10";

export default function AboutPage() {
  const [about, setAbout] = useState<About | null>(null);
  const [failed, setFailed] = useState(false);
  const [feedbackOpen, setFeedbackOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    storeApi<About>("/about")
      .then((a) => !cancelled && setAbout(a))
      .catch(() => !cancelled && setFailed(true));
    return () => {
      cancelled = true;
    };
  }, []);

  if (failed) {
    return (
      <main className={wrap}>
        <h1 className="text-3xl font-extrabold text-gray-900">About us</h1>
        <p className="mt-3 text-gray-600">This page could not be loaded right now. Please try again in a moment.</p>
      </main>
    );
  }
  if (!about) return <main className={`${wrap} text-gray-600`}>Loading…</main>;

  const btn = "rounded px-5 py-2.5 text-sm font-bold hover:brightness-110";
  return (
    <main className={wrap}>
      <h1 className="text-3xl font-extrabold text-gray-900">{about.title}</h1>
      <div className={about.image_url ? "mt-5 grid gap-6 md:grid-cols-[18rem_1fr] md:items-start" : "mt-5"}>
        {about.image_url && (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={about.image_url} alt={about.title} className="w-full rounded-lg border border-gray-200 object-cover" />
        )}
        <div>
          {about.body.trim() ? <AboutBody text={about.body} /> : <p className="text-gray-600">Check back soon.</p>}
        </div>
      </div>

      <section className="mt-8 rounded-lg border border-gray-200 bg-white p-5">
        <h2 className="text-lg font-bold text-gray-900">See what customers say</h2>
        {about.ebay_feedback ? (
          <div className="mt-2">
            <p className="text-gray-900">
              <span className="text-3xl font-extrabold">{about.ebay_feedback.percent}%</span>{" "}
              <span className="font-semibold">positive feedback on eBay</span>
              {about.ebay_feedback.score != null && (
                <span className="text-gray-600"> · {about.ebay_feedback.score.toLocaleString("en-US")} ratings</span>
              )}
            </p>
            {about.ebay_feedback.as_of && (
              <p className="mt-0.5 text-xs text-gray-500">
                As of {new Date(about.ebay_feedback.as_of).toLocaleDateString("en-US", { month: "short", year: "numeric" })}
              </p>
            )}
          </div>
        ) : (
          <p className="mt-1 text-sm text-gray-600">Read what buyers say about us on eBay and on this site.</p>
        )}
        <div className="mt-4 flex flex-wrap gap-3">
          <button
            type="button"
            aria-expanded={feedbackOpen}
            aria-controls="customer-feedback"
            onClick={() => setFeedbackOpen((o) => !o)}
            className={`${btn} bg-[#06bde7] text-[#04202b]`}
          >
            {feedbackOpen ? "Hide customer feedback" : "Read customer feedback"}
          </button>
          {about.ebay_store_url && (
            <a href={about.ebay_store_url} target="_blank" rel="noopener noreferrer" className={`${btn} border border-gray-300 text-gray-900`}>
              Visit our eBay store
            </a>
          )}
        </div>
        <CustomerFeedback id="customer-feedback" open={feedbackOpen} feedbackUrl={about.ebay_feedback_url} />
      </section>
    </main>
  );
}
