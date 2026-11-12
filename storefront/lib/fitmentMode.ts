// The four ways a product can be matched to vehicles, built from three database flags.
// Keep this in step with FITMENT_MODES in backend/app/admin_catalog.py.

export type FitmentFlags = {
  is_universal: boolean;
  universal_in_vehicle_search: boolean;
  fitment_exempt: boolean;
};

export type FitmentMode = "vehicle" | "universal_search" | "universal_category" | "not_vehicle";

export const MODE_FLAGS: Record<FitmentMode, FitmentFlags> = {
  vehicle: { is_universal: false, universal_in_vehicle_search: true, fitment_exempt: false },
  universal_search: { is_universal: true, universal_in_vehicle_search: true, fitment_exempt: false },
  universal_category: { is_universal: true, universal_in_vehicle_search: false, fitment_exempt: false },
  not_vehicle: { is_universal: false, universal_in_vehicle_search: true, fitment_exempt: true },
};

export function modeOf(f: FitmentFlags): FitmentMode {
  if (f.fitment_exempt) return "not_vehicle";
  if (f.is_universal) return f.universal_in_vehicle_search ? "universal_search" : "universal_category";
  return "vehicle";
}

export const MODE_OPTIONS: { value: FitmentMode; label: string; short: string; help: string }[] = [
  {
    value: "vehicle",
    label: "Vehicle-specific",
    short: "Vehicle-specific",
    help: "Fits only the machines you add below. This is the default.",
  },
  {
    value: "universal_search",
    label: "Universal – show in vehicle search",
    short: "Universal (vehicle search)",
    help: "Fits everything, so it appears in every Year/Make/Model search and in its category.",
  },
  {
    value: "universal_category",
    label: "Universal – category only",
    short: "Universal (category only)",
    help: "Fits everything, but stays out of the vehicle search. Customers find it under its category. You can still add specific machines below (oil, for example).",
  },
  {
    value: "not_vehicle",
    label: "Not vehicle-based",
    short: "Not vehicle-based",
    help: "Helmets, apparel, gear. Never appears in a vehicle search and never needs fitment.",
  },
];

// What the products list shows for each status the server sends.
export type FitmentStatus = "needs_fitment" | "has_fitment" | "universal_search" | "universal_category" | "not_vehicle";

export const STATUS_LABEL: Record<FitmentStatus, string> = {
  needs_fitment: "Needs fitment",
  has_fitment: "Has fitment",
  universal_search: "Universal (vehicle search)",
  universal_category: "Universal (category only)",
  not_vehicle: "Not vehicle-based",
};
