import { createClient } from "@supabase/supabase-js";
import { revalidatePath } from "next/cache";

/**
 * Refresh the map right after something is published. Called by the publish workflows with the token stored in
 * Supabase `app_settings`; the token is checked by a database function, so it never has to live in this app.
 */
export async function POST(req: Request) {
  const token = req.headers.get("x-revalidate-token");
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY;
  if (!token || !url || !key) return Response.json({ ok: false }, { status: 401 });
  const { data, error } = await createClient(url, key).rpc("check_revalidate_token", { t: token });
  if (error || data !== true) return Response.json({ ok: false }, { status: 401 });
  revalidatePath("/");
  return Response.json({ ok: true, revalidated: "/" });
}
