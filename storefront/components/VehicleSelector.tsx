"use client";

import { Suspense, useEffect, useState, type ReactNode } from "react";
import { useRouter, usePathname, useSearchParams } from "next/navigation";
import { useAuth } from "./site/AuthProvider";

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

// The selection is remembered per browser tab so going back from a product
// page puts the dropdowns back the way they were.
const STORAGE_KEY = "ei-vehicle-selector";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API}/store/fitment${path}`);
  if (!res.ok) throw new Error(`Request failed: ${res.status}`);
  return res.json();
}

// One numbered step of the "Select Your Ride" bar.
function Step({ n, label, children }: { n: number; label: string; children: ReactNode }) {
  return (
    <label className="flex min-w-0 items-center gap-3 rounded bg-white px-3 py-1.5 shadow-sm focus-within:ring-2 focus-within:ring-[#06bde7]">
      <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-[#0b2b3a] text-sm font-bold text-white">
        {n}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block text-[10px] font-bold uppercase tracking-wider text-gray-500">{label}</span>
        {children}
      </span>
    </label>
  );
}

function VehicleSelectorInner() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const { user, garage, openAuth, addToGarage, pending, clearPending } = useAuth();

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
  const [restored, setRestored] = useState(false);
  const [saving, setSaving] = useState(false);

  const typeParam = type ? `&type=${encodeURIComponent(type)}` : "";

  // Restore the last selection once, on first load in this tab.
  useEffect(() => {
    try {
      const raw = sessionStorage.getItem(STORAGE_KEY);
      if (raw) {
        const saved = JSON.parse(raw);
        // Coming back via a plain "/" link drops ?type= from the URL, so put
        // the remembered type back. A type already in the URL wins.
        let effectiveType = type;
        if (!type && VALID_TYPES.has(saved.type)) {
          effectiveType = saved.type;
          setTypeInUrl(saved.type);
        }
        if (saved.year) setYear(String(saved.year));
        // Make and model only carry over if the machine type is unchanged.
        if ((saved.type ?? "") === effectiveType) {
          if (saved.makeId) setMakeId(String(saved.makeId));
          if (saved.modelId) setModelId(String(saved.modelId));
        }
      }
    } catch {
      /* storage unavailable or unreadable: start empty */
    }
    setRestored(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Save the selection after every change (but not before the restore ran,
  // or the empty starting state would overwrite what was saved).
  useEffect(() => {
    if (!restored) return;
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ type, year, makeId, modelId }));
    } catch {
      /* ignore */
    }
  }, [restored, type, year, makeId, modelId]);

  // A machine chosen from My Garage: fill in the dropdowns. (The garage menu
  // also navigates with ?type= in the URL, so the machine type follows.)
  useEffect(() => {
    if (!pending) return;
    setYear(String(pending.year));
    setMakeId(String(pending.make_id));
    setModelId(String(pending.model_id));
    clearPending();
  }, [pending, clearPending]);

  function setTypeInUrl(next: string) {
    const params = new URLSearchParams(searchParams.toString());
    if (next) params.set("type", next);
    else params.delete("type");
    const qs = params.toString();
    router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
  }

  function onTypeChange(next: string) {
    setTypeInUrl(next);
    // The old make/model may not exist under the new type, so clear them.
    setMakeId("");
    setModelId("");
    setModels([]);
  }

  function onYearChange(next: string) {
    setYear(next);
    setMakeId("");
    setModelId("");
    setModels([]);
  }

  function onMakeChange(next: string) {
    setMakeId(next);
    setModelId("");
    setModels([]);
  }

  useEffect(() => {
    get<number[]>("/years").then(setYears).catch(() => setError("Could not load years"));
  }, []);

  useEffect(() => {
    let cancelled = false;
    if (!year) {
      setMakes([]);
      return;
    }
    get<Make[]>(`/makes?year=${year}${typeParam}`)
      .then((d) => {
        if (cancelled) return;
        setMakes(d);
        // Drop a remembered make that isn't in this list.
        setMakeId((cur) => (d.some((m) => String(m.id) === cur) ? cur : ""));
      })
      .catch(() => !cancelled && setError("Could not load makes"));
    return () => {
      cancelled = true;
    };
  }, [year, typeParam]);

  useEffect(() => {
    let cancelled = false;
    if (!year || !makeId) {
      setModels([]);
      return;
    }
    get<Model[]>(`/models?year=${year}&make_id=${makeId}${typeParam}`)
      .then((d) => {
        if (cancelled) return;
        setModels(d);
        // Drop a remembered model that isn't in this list.
        setModelId((cur) => (d.some((m) => String(m.id) === cur) ? cur : ""));
      })
      .catch(() => !cancelled && setError("Could not load models"));
    return () => {
      cancelled = true;
    };
  }, [year, makeId, typeParam]);

  const inGarage = !!modelId && garage.some((g) => g.model_id === Number(modelId) && g.year === Number(year));

  async function saveToGarage() {
    if (!user) return openAuth("signin");
    setSaving(true);
    setError("");
    try {
      await addToGarage(Number(year), Number(modelId));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save to your garage");
    } finally {
      setSaving(false);
    }
  }

  const selectClass =
    "block w-full min-w-0 truncate bg-transparent text-sm font-semibold text-gray-900 outline-none disabled:text-gray-400";

  return (
    // Four equal columns on larger screens (minmax(0,1fr) keeps them equal no
    // matter how long the option text is), then the buttons. Stacked on mobile.
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-[repeat(4,minmax(0,1fr))_auto_auto] lg:items-stretch">
      <Step n={1} label="Ride type">
        <select className={selectClass} value={type} onChange={(e) => onTypeChange(e.target.value)}>
          <option value="">All types</option>
          {MACHINE_TYPES.map((t) => (
            <option key={t.slug} value={t.slug}>{t.name}</option>
          ))}
        </select>
      </Step>

      <Step n={2} label="Year">
        <select className={selectClass} value={year} onChange={(e) => onYearChange(e.target.value)}>
          <option value="">Select year</option>
          {years.map((y) => (
            <option key={y} value={y}>{y}</option>
          ))}
        </select>
      </Step>

      <Step n={3} label="Make">
        <select className={selectClass} value={makeId} disabled={!year} onChange={(e) => onMakeChange(e.target.value)}>
          <option value="">Select make</option>
          {makes.map((m) => (
            <option key={m.id} value={m.id}>{m.name}</option>
          ))}
        </select>
      </Step>

      <Step n={4} label="Model">
        <select className={selectClass} value={modelId} disabled={!makeId} onChange={(e) => setModelId(e.target.value)}>
          <option value="">Select model</option>
          {models.map((m) => (
            <option key={m.id} value={m.id}>{m.name}</option>
          ))}
        </select>
      </Step>

      <button
        type="button"
        disabled={!modelId}
        onClick={() => router.push(`/shop?year=${year}&model_id=${modelId}${typeParam}`)}
        className="rounded bg-[#06bde7] px-6 py-3 text-sm font-extrabold uppercase tracking-wide text-[#04202b] hover:brightness-110 disabled:opacity-40"
      >
        Find parts
      </button>

      <button
        type="button"
        disabled={!modelId || saving || inGarage}
        onClick={saveToGarage}
        className="rounded border border-white/40 px-4 py-3 text-sm font-bold text-white hover:bg-white/10 disabled:opacity-40"
      >
        {inGarage ? "In your garage ✓" : saving ? "Saving…" : "+ Save to garage"}
      </button>

      {error && <p className="text-sm text-red-300 sm:col-span-full">{error}</p>}
    </div>
  );
}

// useSearchParams() must sit inside a Suspense boundary or the static build
// fails, so the boundary lives here and pages can render <VehicleSelector />
// without wrapping it themselves.
export default function VehicleSelector() {
  return (
    <Suspense fallback={null}>
      <VehicleSelectorInner />
    </Suspense>
  );
}
