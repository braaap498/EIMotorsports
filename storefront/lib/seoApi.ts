// Calls to the API's /store/seo/* endpoints, shared by the vehicle pages and their generateMetadata.
// Each call uses the same URL and options everywhere, so Next sends it once per request.
const API = process.env.NEXT_PUBLIC_API_URL ?? "";

export type VehicleIndexModel = { name: string; slug: string; vehicle_type: string | null; products: number };
export type VehicleIndexMake = { name: string; slug: string; models: VehicleIndexModel[] };

export type VehicleInfo = {
  make: { id: number; name: string; slug: string };
  model: { id: number; name: string; slug: string; vehicle_type: string | null };
  canonical_model_slug: string;
  is_submodel: boolean;
  years: number[];
  products: number;
};

// Every make with its models that have published parts. null means the API could not be reached.
export async function getVehicleIndex(): Promise<VehicleIndexMake[] | null> {
  try {
    const res = await fetch(`${API}/store/seo/vehicles`, { cache: "no-store" });
    if (!res.ok) return null;
    return (await res.json()) as VehicleIndexMake[];
  } catch {
    return null;
  }
}

export async function getVehicle(
  make: string,
  model: string,
): Promise<{ status: number; vehicle: VehicleInfo | null }> {
  try {
    const res = await fetch(
      `${API}/store/seo/vehicle/${encodeURIComponent(make)}/${encodeURIComponent(model)}`,
      { cache: "no-store" },
    );
    if (!res.ok) return { status: res.status, vehicle: null };
    return { status: res.status, vehicle: (await res.json()) as VehicleInfo };
  } catch {
    return { status: 0, vehicle: null };
  }
}
