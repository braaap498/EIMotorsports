"use client";

import { useCallback, useEffect, useState } from "react";
import { useAuth } from "../../components/site/AuthProvider";
import { storeApi } from "../../lib/storeApi";

type Kind = "shipping" | "billing";
type Address = {
  id: number;
  kind: Kind;
  full_name: string;
  line1: string;
  line2: string | null;
  city: string;
  region: string;
  postal_code: string;
  country: string;
  phone: string | null;
  is_default: boolean;
};
type Draft = Omit<Address, "id" | "line2" | "phone"> & { id?: number; line2: string; phone: string };

const input =
  "mt-1 w-full rounded border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 outline-none focus:border-[#06bde7] focus:ring-2 focus:ring-[#06bde7]/30";
const primary = "rounded bg-[#06bde7] px-4 py-2 text-sm font-bold text-[#04202b] hover:brightness-110 disabled:opacity-50";
const secondary = "rounded border border-gray-300 px-4 py-2 text-sm font-semibold text-gray-900 hover:bg-gray-50 disabled:opacity-50";
const COUNTRIES = [
  { code: "US", name: "United States" },
  { code: "CA", name: "Canada" },
];

const blank = (kind: Kind, name: string): Draft => ({
  kind,
  full_name: name,
  line1: "",
  line2: "",
  city: "",
  region: "",
  postal_code: "",
  country: "US",
  phone: "",
  is_default: false,
});

const msg = (e: unknown) => (e instanceof Error ? e.message : "Something went wrong");

function AddressForm({
  initial,
  onSaved,
  onCancel,
}: {
  initial: Draft;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const [d, setD] = useState<Draft>(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const set = (k: keyof Draft, v: string | boolean) => setD((cur) => ({ ...cur, [k]: v }));

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const body = { ...d, line2: d.line2 || null, phone: d.phone || null };
      await storeApi(d.id ? `/addresses/${d.id}` : "/addresses", { method: "POST", body });
      onSaved();
    } catch (err) {
      setError(msg(err));
      setBusy(false);
    }
  }

  return (
    <form onSubmit={save} className="space-y-3 rounded border border-gray-200 bg-gray-50 p-4">
      <label className="block text-sm font-medium">
        Full name
        <input className={input} value={d.full_name} onChange={(e) => set("full_name", e.target.value)} autoComplete="name" required />
      </label>
      <label className="block text-sm font-medium">
        Address
        <input className={input} value={d.line1} onChange={(e) => set("line1", e.target.value)} autoComplete="address-line1" required />
      </label>
      <label className="block text-sm font-medium">
        Apartment, suite, etc. (optional)
        <input className={input} value={d.line2} onChange={(e) => set("line2", e.target.value)} autoComplete="address-line2" />
      </label>
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="block text-sm font-medium">
          City
          <input className={input} value={d.city} onChange={(e) => set("city", e.target.value)} autoComplete="address-level2" required />
        </label>
        <label className="block text-sm font-medium">
          State / Province
          <input className={input} value={d.region} onChange={(e) => set("region", e.target.value)} autoComplete="address-level1" required />
        </label>
        <label className="block text-sm font-medium">
          ZIP / Postal code
          <input className={input} value={d.postal_code} onChange={(e) => set("postal_code", e.target.value)} autoComplete="postal-code" required />
        </label>
        <label className="block text-sm font-medium">
          Country
          <select className={input} value={d.country} onChange={(e) => set("country", e.target.value)} autoComplete="country">
            {COUNTRIES.map((c) => (
              <option key={c.code} value={c.code}>{c.name}</option>
            ))}
          </select>
        </label>
      </div>
      <label className="block text-sm font-medium">
        Phone (optional)
        <input className={input} type="tel" value={d.phone} onChange={(e) => set("phone", e.target.value)} autoComplete="tel" />
      </label>
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={d.is_default} onChange={(e) => set("is_default", e.target.checked)} />
        Use as my default {d.kind} address
      </label>
      {error && <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">{error}</p>}
      <div className="flex gap-2">
        <button className={primary} disabled={busy}>{busy ? "Saving…" : "Save address"}</button>
        <button type="button" className={secondary} onClick={onCancel} disabled={busy}>Cancel</button>
      </div>
    </form>
  );
}

function AddressSection({
  kind,
  title,
  hint,
  addresses,
  fullName,
  onChanged,
  copyFrom,
}: {
  kind: Kind;
  title: string;
  hint: string;
  addresses: Address[];
  fullName: string;
  onChanged: () => void;
  copyFrom?: Address; // a shipping address that can be reused as billing
}) {
  const [editing, setEditing] = useState<Draft | null>(null);
  const [removing, setRemoving] = useState<number | null>(null);
  const [error, setError] = useState("");

  async function run(fn: () => Promise<unknown>) {
    setError("");
    try {
      await fn();
      onChanged();
    } catch (e) {
      setError(msg(e));
    }
  }

  const toDraft = (a: Address): Draft => ({ ...a, line2: a.line2 ?? "", phone: a.phone ?? "" });

  return (
    <section className="rounded-lg border border-gray-200 bg-white p-5 shadow-sm">
      <h2 className="text-lg font-bold text-gray-900">{title}</h2>
      <p className="mb-4 text-sm text-gray-600">{hint}</p>

      {error && <p className="mb-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">{error}</p>}

      <ul className="space-y-3">
        {addresses.map((a) => (
          <li key={a.id} className="rounded border border-gray-200 p-3 text-sm">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <p className="font-semibold">
                  {a.full_name}
                  {a.is_default && <span className="ml-2 rounded bg-[#06bde7]/20 px-2 py-0.5 text-xs font-bold text-[#04566b]">Default</span>}
                </p>
                <p>{a.line1}</p>
                {a.line2 && <p>{a.line2}</p>}
                <p>{a.city}, {a.region} {a.postal_code}</p>
                <p>{COUNTRIES.find((c) => c.code === a.country)?.name ?? a.country}</p>
                {a.phone && <p className="text-gray-600">{a.phone}</p>}
              </div>
              <div className="flex flex-wrap gap-3 text-sm">
                {!a.is_default && (
                  <button type="button" className="underline" onClick={() => void run(() => storeApi(`/addresses/${a.id}/default`, { method: "POST", body: {} }))}>
                    Make default
                  </button>
                )}
                <button type="button" className="underline" onClick={() => setEditing(toDraft(a))}>Edit</button>
                {removing === a.id ? (
                  <span className="flex items-center gap-2">
                    <span className="text-gray-700">Remove it?</span>
                    <button type="button" className="font-semibold text-red-700 underline" onClick={() => { setRemoving(null); void run(() => storeApi(`/addresses/${a.id}`, { method: "DELETE" })); }}>
                      Yes, remove
                    </button>
                    <button type="button" className="underline" onClick={() => setRemoving(null)}>Keep</button>
                  </span>
                ) : (
                  <button type="button" className="text-red-700 underline" onClick={() => setRemoving(a.id)}>Remove</button>
                )}
              </div>
            </div>
          </li>
        ))}
        {addresses.length === 0 && !editing && <li className="text-sm text-gray-500">No {kind} address saved yet.</li>}
      </ul>

      {editing ? (
        <div className="mt-4">
          <AddressForm
            key={editing.id ?? "new"}
            initial={editing}
            onCancel={() => setEditing(null)}
            onSaved={() => {
              setEditing(null);
              onChanged();
            }}
          />
        </div>
      ) : (
        <div className="mt-4 flex flex-wrap gap-2">
          <button type="button" className={primary} onClick={() => setEditing(blank(kind, fullName))}>
            Add a {kind} address
          </button>
          {kind === "billing" && addresses.length === 0 && copyFrom && (
            <button
              type="button"
              className={secondary}
              onClick={() =>
                void run(() =>
                  storeApi("/addresses", {
                    method: "POST",
                    body: { ...copyFrom, kind: "billing", is_default: true },
                  })
                )
              }
            >
              Same as my shipping address
            </button>
          )}
        </div>
      )}
    </section>
  );
}

export default function AccountPage() {
  const { user, ready, openAuth } = useAuth();
  const [addresses, setAddresses] = useState<Address[] | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    storeApi<Address[]>("/addresses")
      .then((a) => {
        setAddresses(a);
        setError("");
      })
      .catch((e) => setError(msg(e)));
  }, []);

  useEffect(() => {
    if (user) load();
  }, [user, load]);

  const meta = user?.user_metadata as { full_name?: string } | undefined;
  const fullName = meta?.full_name ?? "";

  if (!ready) return <main className="mx-auto max-w-3xl px-4 py-10 text-gray-600">Loading…</main>;

  if (!user) {
    return (
      <main className="mx-auto max-w-3xl px-4 py-10">
        <h1 className="text-3xl font-extrabold text-gray-900">My account</h1>
        <p className="mt-3 text-gray-600">Sign in to manage your addresses.</p>
        <div className="mt-5 flex gap-3">
          <button type="button" className={primary} onClick={() => openAuth("signin")}>Sign in</button>
          <button type="button" className={secondary} onClick={() => openAuth("signup")}>Create account</button>
        </div>
      </main>
    );
  }

  const shipping = (addresses ?? []).filter((a) => a.kind === "shipping");
  const billing = (addresses ?? []).filter((a) => a.kind === "billing");

  return (
    <main className="mx-auto max-w-3xl space-y-6 px-4 py-10">
      <div>
        <h1 className="text-3xl font-extrabold text-gray-900">My account</h1>
        <p className="mt-1 text-gray-600">
          {fullName ? `${fullName} · ` : ""}{user.email}
        </p>
      </div>

      {error && <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">{error}</p>}
      {addresses === null && !error && <p className="text-gray-600">Loading your addresses…</p>}

      {addresses !== null && (
        <>
          <AddressSection
            kind="shipping"
            title="Shipping addresses"
            hint="Where we send your orders. Your default is filled in at checkout."
            addresses={shipping}
            fullName={fullName}
            onChanged={load}
          />
          <AddressSection
            kind="billing"
            title="Billing addresses"
            hint="The address that matches your payment card."
            addresses={billing}
            fullName={fullName}
            onChanged={load}
            copyFrom={shipping.find((a) => a.is_default) ?? shipping[0]}
          />
        </>
      )}

      <section className="rounded-lg border border-gray-200 bg-white p-5 shadow-sm">
        <h2 className="text-lg font-bold text-gray-900">Payment methods</h2>
        <p className="mt-2 text-sm text-gray-600">
          Saving a card will be available when checkout opens. Card numbers are typed into our payment provider&apos;s secure
          form and are never stored on our own servers.
        </p>
      </section>
    </main>
  );
}
