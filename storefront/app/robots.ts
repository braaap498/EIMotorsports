import type { MetadataRoute } from "next";
import { ALLOW_INDEXING, SITE_URL } from "../lib/site";

// Before launch (ALLOW_INDEXING not "true") everything is closed to crawlers.
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
    sitemap: `${SITE_URL}/sitemap.xml`,
  };
}
