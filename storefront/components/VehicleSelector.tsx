"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

const API = process.env.NEXT_PUBLIC_API_URL ?? "";

type Make = { id: number; name: string };
type Model = { id: number; name: string; products: number };

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API}/store/fitment${path}`);
  if (!res.ok) throw new Error(`Request failed: ${res.status}`);
  return res.json();
}

export default function VehicleSelector() {
  const router = useRouter();
  const [years, setYears] = useState<number[]>([]);
  const [makes, setMakes] = useState<Make[]>([]);
  const [models, setModels] = useState<Model[]>([]);
  const [year, setYear] = useState("");
  const [makeId, setMakeId] = useState("");
  const [modelId, setModelId] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    get<number[]>("/years").then(setYears).catch(() => setError("Could not load years"));
  }, []);

  useEffect(() => {
    setMakeId("");
    setModelId("");
    setMakes([]);
    setModels([]);
    if (!year) return;
    get<Make[]>(`/makes?year=${year}`).then(setMakes).catch(() => setError("Could not load makes"));
  }, [year]);

  useEffect(() => {
    setModelId("");
    setModels([]);
    if (!year || !makeId) return;
    get<Model[]>(`/models?year=${year}&make_id=${makeId}`)
      .then(setModels)
      .catch(() => setError("Could not load models"));
  }, [year, makeId]);

  const selectClass =
    "w-full rounded border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 disabled:bg-gray-100 disabled:text-gray-400";

  return (
    <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
      <label className="flex-1 text-sm font-medium">
        Year
        <select className={selectClass} value={year} onChange={(e) => setYear(e.target.value)}>
          <option value="">Select year</option>
          {years.map((y) => (
            <option key={y} value={y}>{y}</option>
          ))}
        </select>
      </label>

      <label className="flex-1 text-sm font-medium">
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

      <label className="flex-1 text-sm font-medium">
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

      {error && <p className="text-sm text-red-600">{error}</p>}
    </div>
  );
}
