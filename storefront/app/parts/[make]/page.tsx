import { Suspense } from "react";
import Link from "next/link";
import { notFound } from "next/navigation";
import type { Metadata } from "next";
import { robotsMeta } from "../../../lib/site";
import { getVehicleIndex } from "../../../lib/seoApi";

type Params = Promise<{ make: string }>;

export async function generateMetadata({ params }: { params: Params }): Promise<Metadata> {
  const { make } = await params;
  const mk = (await getVehicleIndex())?.find((m) => m.slug === make);
  if (!mk) return { title: "Parts", robots: { index: false, follow: false } };
  return {
    title: `${mk.name} Parts`,
    description: `Shop ${mk.name} parts by model. Choose your ${mk.name} to see the parts that fit.`,
    alternates: { canonical: `/parts/${mk.slug}` },
    robots: robotsMeta(true),
  };
}

async function MakeModels({ params }: { params: Params }) {
  const { make } = await params;
  const makes = await getVehicleIndex();
  if (!makes) return <p className="text-red-600">Could not load vehicles right now. Please try again.</p>;
  const mk = makes.find((m) => m.slug === make);
  if (!mk) notFound();

  return (
    <>
      <nav aria-label="Breadcrumb" className="text-sm text-gray-600">
        <Link href="/parts" className="underline">Parts</Link> / {mk.name}
      </nav>
      <h1 className="mb-1 mt-2 text-2xl font-bold">{mk.name} Parts</h1>
      <p className="mb-6 text-sm text-gray-600">Choose your {mk.name} model to see the parts that fit.</p>
      <ul className="grid gap-x-8 gap-y-2 text-sm sm:grid-cols-2 lg:grid-cols-3">
        {mk.models.map((m) => (
          <li key={m.slug}>
            <Link href={`/parts/${mk.slug}/${m.slug}`} className="text-gray-800 underline">
              {mk.name} {m.name}
            </Link>
            <span className="text-gray-500"> ({m.products} part{m.products === 1 ? "" : "s"})</span>
          </li>
        ))}
      </ul>
    </>
  );
}

export default function MakePage({ params }: { params: Params }) {
  return (
    <main className="mx-auto max-w-6xl p-6">
      <Suspense fallback={<p className="text-gray-600">Loading…</p>}>
        <MakeModels params={params} />
      </Suspense>
    </main>
  );
}
