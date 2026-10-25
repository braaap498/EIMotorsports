/** Customer-facing wording for a backordered part's arrival date. Dates arrive as "YYYY-MM-DD". */
export function backorderText(expectedDate?: string | null, daysUntil?: number | null): string {
  if (!expectedDate) return "We don’t have an arrival date yet. Check back soon.";
  const [y, m, d] = expectedDate.split("-").map(Number);
  const when = new Date(y, (m ?? 1) - 1, d ?? 1).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
  if (daysUntil == null) return `Expected around ${when}.`;
  if (daysUntil < 0) return `Was expected ${when}. We’re still waiting on the shipment.`;
  if (daysUntil === 0) return `Expected today (${when}).`;
  return `Expected ${when}, about ${daysUntil} day${daysUntil === 1 ? "" : "s"} from now.`;
}
