import Link from "next/link";

const TYPES = [
  { slug: "atv", name: "ATV" },
  { slug: "utv", name: "UTV" },
  { slug: "snowmobile", name: "Snowmobile" },
  { slug: "dirt-bike", name: "Dirt Bike" },
  { slug: "motorcycle", name: "Motorcycle" },
  { slug: "pwc", name: "PWC" },
];

export default function Home() {
  return (
    <main className="mx-auto max-w-6xl px-4 py-10">
      <h1 className="text-3xl font-extrabold tracking-tight text-gray-900 sm:text-4xl">
        Parts that fit your ride
      </h1>
      <p className="mt-2 max-w-2xl text-gray-600">
        Choose your machine in the bar above, or start with a machine type below, and we&apos;ll show
        only the parts that fit.
      </p>

      <div className="mt-8 grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
        {TYPES.map((t) => (
          <Link
            key={t.slug}
            href={`/?type=${t.slug}`}
            className="rounded-lg border-2 border-gray-200 px-4 py-6 text-center text-lg font-bold text-gray-900 transition hover:border-[#06bde7] hover:shadow-md"
          >
            {t.name}
          </Link>
        ))}
      </div>
    </main>
  );
}
