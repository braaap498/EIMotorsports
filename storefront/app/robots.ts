import type { MetadataRoute } from "next";
import { ALLOW_INDEXING } from "../lib/site";

// Before launch (ALLOW_INDEXING not "true") everything is closed to crawlers.
// Add a `sitemap:` line here once /sitemap.xml exists.
export default function robots(): MetadataRoute.Robots {
  if (!ALLOW_INDEXING) {
    return { rules: { userAgent: "*", disallow: "/" } };
  }
  return {
    rules: {
      userAgent: "*",
      allow: "/",
      disallow: ["/admin/", "/cart", "/checkout", "/account", "/track-order", "/auth/"],
    },
  };
}
