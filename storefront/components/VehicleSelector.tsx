"use client";

import { useEffect, useState } from "react";
import { useRouter, usePathname, useSearchParams } from "next/navigation";

const API = process.env.NEXT_PUBLIC_API_URL ?? "";

type Make = { id: number; name: string };
type Model = { id: number; name: string; products: number };

// Slugs match vehicle_types.slug in the database.
const MACHINE_TYPES = [
  { slug: "snowmobile", name: "Snowmobile" },
  { slug: "atv", name: "ATV" },
  { slug: "utv", name: "UTV" },
  { slug: "dirt-bike", name: "Dirt Bike" },
  { slug: "motorcycle", name: "Motorcycle" },
  { slug: "pwc", name: "PWC" },
];
const VALID_TYPES = new Set(MACHINE_TYPES.map((t) => t.slug));

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API}/store/fitment${path}`);
  if (!res.ok) throw new Error(`Request failed: ${res.status}`);
  return res.json();
}

export default function VehicleSelector() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  // The URL is the source of truth for the machine type. An unknown value
  // (e.g. ?type=boat) falls back to "all types".
  const rawType = searchParams.get("type") ?? "";
  const type = VALID_TYPES.has(rawType) ? rawType : "";

  const [years, setYears] = useState<number[]>([]);
  const [makes, setMakes] = useState<Make[]>([]);
  const [models, setModels] = useState<Model[]>([]);
  const [year, setYear] = useState("");
  const [makeId, setMakeId] = useState("");
  const [modelId, setModelId] = useState("");
  const [error, setError] = useState("");

  const typeParam = type ? `&type=${encodeURIComponent(type)}` : "";

  function onTypeChange(next: string) {
    const params = new URLSearchParams(searchParams.toString());
    if (next) params.set("type", next);
    else params.delete("type");
    const qs = params.toString();
    router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    // The old make/model may not exist under the new type, so clear them.
    setMakeId("");
    setModelId("");
    setModels([]);
  }

  useEffect(() => {
    get<number[]>("/years").then(setYears).catch(() => setError("Could not load years"));
  }, []);

  useEffect(() => {
    let cancelled = false;
    setMakeId("");
    setModelId("");
    setMakes([]);
    setModels([]);
    if (!year) return;
    get<Make[]>(`/makes?year=${year}${typeParam}`)
      .then((d) => !cancelled && setMakes(d))
      .catch(() => !cancelled && setError("Could not load makes"));
    return () => {
      cancelled = true;
    };
  }, [year, typeParam]);

  useEffect(() => {
    let cancelled = false;
    setModelId("");
    setModels([]);
    if (!year || !makeId) return;
    get<Model[]>(`/models?year=${year}&make_id=${makeId}${typeParam}`)
      .then((d) => !cancelled && setModels(d))
      .catch(() => !cancelled && setError("Could not load models"));
    return () => {
      cancelled = true;
    };
  }, [year, makeId, typeParam]);

  // Fixed widths on larger screens: min-w-0 + truncate stop long option text
  // from stretching a select. On mobile everything stacks full width.
  const selectClass =
    "w-full min-w-0 truncate rounded border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 disabled:bg-gray-100 disabled:text-gray-400";

  return (
    <div className="flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-end">
      <label className="text-sm font-medium sm:w-40 sm:shrink-0">
        Machine type
        <select
          className={selectClass}
          value={type}
          onChange={(e) => onTypeChange(e.target.value)}
        >
          <option value="">All types</option>
          {MACHINE_TYPES.map((t) => (
            <option key={t.slug} value={t.slug}>{t.name}</option>
          ))}
        </select>
      </label>

      <label className="text-sm font-medium sm:w-28 sm:shrink-0">
        Year
        <select className={selectClass} value={year} onChange={(e) => setYear(e.target.value)}>
          <option value="">Select year</option>
          {years.map((y) => (
            <option key={y} value={y}>{y}</option>
          ))}
        </select>
      </label>

      <label className="text-sm font-medium sm:w-56 sm:shrink-0">
        Make
        <select
          className={selectClass}
          value={makeId}
          disabled={!year}
          onChange={(e) => setMakeId(e.target.value)}
        >
          <option value="">Select make</option>
          {makes.map((m) => (
            <option key={m.id} value={m.id}>{m.name}</option>
          ))}
        </select>
      </label>

      <label className="text-sm font-medium sm:w-72 sm:shrink-0">
        Model
        <select
          className={selectClass}
          value={modelId}
          disabled={!makeId}
          onChange={(e) => setModelId(e.target.value)}
        >
          <option value="">Select model</option>
          {models.map((m) => (
            <option key={m.id} value={m.id}>{m.name}</option>
          ))}
        </select>
      </label>

      <button
        type="button"
        disabled={!modelId}
        onClick={() => router.push(`/shop?year=${year}&model_id=${modelId}`)}
        className="rounded bg-black px-5 py-2 text-sm font-semibold text-white disabled:opacity-40"
      >
        Find parts
      </button>

      {error && <p className="w-full text-sm text-red-600">{error}</p>}
    </div>
  );
}
