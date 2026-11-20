// Calls to the API's /store/seo/* endpoints, shared by the vehicle pages and their generateMetadata.
// Answers are remembered for an hour (see cachedApi.ts).
import { getJson } from "./cachedApi";

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
  const { data } = await getJson<VehicleIndexMake[]>("/store/seo/vehicles", { ttl: "long" });
  return data;
}

export async function getVehicle(
  make: string,
  model: string,
): Promise<{ status: number; vehicle: VehicleInfo | null }> {
  const { status, data } = await getJson<VehicleInfo>(
    `/store/seo/vehicle/${encodeURIComponent(make)}/${encodeURIComponent(model)}`,
    { ttl: "long" },
  );
  return { status, vehicle: data };
}
