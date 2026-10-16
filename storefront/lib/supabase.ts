import { createClient, type SupabaseClient } from "@supabase/supabase-js";

// Created on first use (not at import time) so a build without the env vars
// set doesn't fail while Next.js pre-renders pages.
let client: SupabaseClient | null = null;

export function getSupabase(): SupabaseClient {
  if (!client) {
    const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
    const key = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
    if (!url || !key) {
      throw new Error(
        "NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY must be set"
      );
    }
    client = createClient(url, key);
  }
  return client;
}
