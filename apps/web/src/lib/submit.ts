"use client";

import { createClient } from "@supabase/supabase-js";

const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
const key = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY;

export const canSubmit = Boolean(url && key);

export function client() {
  if (!url || !key) throw new Error("Supabase is not configured");
  return createClient(url, key, { auth: { persistSession: false } });
}

export type SubmissionKind = "new_site" | "correction" | "new_detail" | "photo" | "feedback";

/** Fire-and-forget from the visitor's view: a background worker turns it into a GitHub issue. */
export async function submit(kind: SubmissionKind, opts: {
  siteId?: string | null; photoId?: string | null; lang: "en" | "zh"; payload: Record<string, unknown>; contact?: string;
}) {
  const { error } = await client().from("submissions").insert({
    kind, site_id: opts.siteId ?? null, photo_id: opts.photoId ?? null, lang: opts.lang,
    payload: { ...opts.payload, page: typeof location !== "undefined" ? location.href : null },
    contact: opts.contact?.slice(0, 200) || null,
  });
  if (error) throw error;
}
