import { Suspense } from "react";
import type { Metadata } from "next";
import CustomerFeedback from "../../components/site/CustomerFeedback";
import { plainText, robotsMeta } from "../../lib/site";

const API = process.env.NEXT_PUBLIC_API_URL ?? "";

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

// Fetched on the server, so the text is in the page's HTML for search engines.
// generateMetadata and the page both call this with the same URL and options,
// so Next sends one request, not two.
async function getAbout(): Promise<About | null> {
  try {
    const res = await fetch(`${API}/store/about`, { cache: "no-store" });
    if (!res.ok) return null;
    return (await res.json()) as About;
  } catch {
    return null;
  }
}

export async function generateMetadata(): Promise<Metadata> {
  const about = await getAbout();
  const description = plainText(about?.body, 155);
  return {
    title: about?.title || "About us",
    ...(description ? { description } : {}),
    alternates: { canonical: "/about" },
    robots: robotsMeta(true),
  };
}

// The fetch sits inside <Suspense> (like the other pages) because the project builds with Cache Components,
// which does not allow uncached data outside a Suspense boundary. The text is still part of the HTML the server sends.
async function AboutContent() {
  const about = await getAbout();

  if (!about) {
    return (
      <>
        <h1 className="text-3xl font-extrabold text-gray-900">About us</h1>
        <p className="mt-3 text-gray-600">This page could not be loaded right now. Please try again in a moment.</p>
      </>
    );
  }

  return (
    <>
      <h1 className="text-3xl font-extrabold text-gray-900">{about.title}</h1>
      <div className={about.image_url ? "mt-5 grid gap-6 md:grid-cols-[18rem_1fr] md:items-start" : "mt-5"}>
        {about.image_url && (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={about.image_url} alt={about.title} className="w-full rounded-lg border border-gray-200 object-cover" />
        )}
        <div>
          {about.body.trim() ? (
            // The server sends cleaned HTML (the same kind product descriptions use).
            <div className="page-content text-gray-700" dangerouslySetInnerHTML={{ __html: about.body }} />
          ) : (
            <p className="text-gray-600">Check back soon.</p>
          )}
        </div>
      </div>

      <section className="mt-8 rounded-lg border border-gray-200 bg-white p-5">
        <h2 className="mb-4 text-lg font-bold text-gray-900">See what customers say</h2>
        <CustomerFeedback
          ebayStats={about.ebay_feedback}
          feedbackUrl={about.ebay_feedback_url}
          storeUrl={about.ebay_store_url}
        />
      </section>
    </>
  );
}

export default function AboutPage() {
  return (
    <main className={wrap}>
      <Suspense fallback={<p className="text-gray-600">Loading…</p>}>
        <AboutContent />
      </Suspense>
    </main>
  );
}
