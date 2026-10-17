"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { useAuth, type GarageItem } from "./AuthProvider";
import { ChevronIcon, GarageIcon } from "./icons";

export default function GarageMenu() {
  const router = useRouter();
  const { user, ready, garage, openAuth, selectMachine, removeFromGarage } = useAuth();
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");

  function pick(g: GarageItem) {
    selectMachine({ year: g.year, make_id: g.make_id, model_id: g.model_id });
    setOpen(false);
    const type = g.type ? `&type=${encodeURIComponent(g.type)}` : "";
    router.push(`/shop?year=${g.year}&model_id=${g.model_id}${type}`);
  }

  async function remove(id: number) {
    setError("");
    try {
      await removeFromGarage(id);
    } catch {
      setError("Could not remove that machine. Try again.");
    }
  }

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-2 rounded px-2 py-1 text-sm font-semibold text-white hover:bg-white/10"
        aria-expanded={open}
      >
        <GarageIcon className="h-5 w-5 text-[#06bde7]" />
        My Garage
        {garage.length > 0 && (
          <span className="rounded-full bg-[#06bde7] px-1.5 text-xs font-bold text-[#04202b]">{garage.length}</span>
        )}
        <ChevronIcon className="h-3.5 w-3.5" />
      </button>

      {open && (
        <>
          <button type="button" aria-label="Close menu" className="fixed inset-0 z-40 cursor-default" onClick={() => setOpen(false)} />
          <div className="absolute left-0 z-50 mt-1 w-72 rounded-lg bg-white p-3 text-gray-900 shadow-xl">
            {!ready ? null : !user ? (
              <div className="space-y-2 text-sm">
                <p>Sign in to save your machines and find parts faster next time.</p>
                <button
                  type="button"
                  onClick={() => {
                    setOpen(false);
                    openAuth("signin");
                  }}
                  className="rounded bg-[#06bde7] px-3 py-1.5 font-bold text-[#04202b]"
                >
                  Sign in or create account
                </button>
              </div>
            ) : garage.length === 0 ? (
              <p className="text-sm text-gray-600">
                Nothing saved yet. Pick your year, make and model, then press <strong>+Save To Garage</strong>.
              </p>
            ) : (
              <ul className="divide-y divide-gray-100">
                {garage.map((g) => (
                  <li key={g.id} className="flex items-center gap-2 py-2">
                    <button type="button" onClick={() => pick(g)} className="min-w-0 flex-1 text-left text-sm font-semibold hover:text-[#0489ad]">
                      <span className="block truncate">{g.year} {g.make} {g.model}</span>
                      <span className="text-xs font-normal text-gray-500">Shop parts for this machine</span>
                    </button>
                    <button type="button" onClick={() => remove(g.id)} aria-label={`Remove ${g.year} ${g.make} ${g.model}`} className="text-lg text-gray-400 hover:text-red-600">
                      ×
                    </button>
                  </li>
                ))}
              </ul>
            )}
            {error && <p className="mt-2 text-xs text-red-600">{error}</p>}
          </div>
        </>
      )}
    </div>
  );
}
