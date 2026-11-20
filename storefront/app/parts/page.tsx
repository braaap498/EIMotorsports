import { Suspense } from "react";
import Link from "next/link";
import type { Metadata } from "next";
import { robotsMeta } from "../../lib/site";
import { getVehicleIndex } from "../../lib/seoApi";

export const metadata: Metadata = {
  title: "Shop parts by vehicle",
  description: "Find parts for your ATV, UTV, snowmobile, dirt bike, motorcycle or PWC. Pick your make and model.",
  alternates: { canonical: "/parts" },
  robots: robotsMeta(true),
};

async function Makes() {
  const makes = await getVehicleIndex();
  if (!makes) return <p className="text-red-600">Could not load vehicles right now. Please try again.</p>;
  if (makes.length === 0) return <p className="text-gray-600">No vehicles to show yet.</p>;
  return (
    <div className="grid gap-8 sm:grid-cols-2 lg:grid-cols-3">
      {makes.map((mk) => (
        <section key={mk.slug}>
          <h2 className="text-lg font-bold">
            <Link href={`/parts/${mk.slug}`} className="hover:text-[#0489ad]">
              {mk.name}
            </Link>
          </h2>
          <ul className="mt-2 space-y-1 text-sm">
            {mk.models.map((m) => (
              <li key={m.slug}>
                <Link href={`/parts/${mk.slug}/${m.slug}`} className="text-gray-700 underline">
                  {m.name}
                </Link>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

export default function PartsIndex() {
  return (
    <main className="mx-auto max-w-6xl p-6">
      <h1 className="mb-1 mt-2 text-2xl font-bold">Shop parts by vehicle</h1>
      <p className="mb-6 text-sm text-gray-600">Choose your make and model to see the parts that fit.</p>
      <Suspense fallback={<p className="text-gray-600">Loading…</p>}>
        <Makes />
      </Suspense>
    </main>
  );
}
