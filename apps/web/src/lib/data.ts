import "server-only";
import { createClient } from "@supabase/supabase-js";
import seed from "@/data/hangzhou.json";
import type { LegacyEvent, Site, Source } from "./types";

const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
const key = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY;

type SeedSource = { key: string; url: string; title: string; publisher: string };
type SeedEvent = {
  type: string; year_start?: number; year_end?: number; description?: string;
  person?: string; sources: string[]; conflict_with?: string;
};

function fromSeed(): Site[] {
  const src = new Map<string, Source>(
    (seed.sources as SeedSource[]).map((s) => [s.key, { url: s.url, title: s.title, publisher: s.publisher }]),
  );
  const people = new Map(seed.persons.map((p) => [p.slug, { name_en: p.name_en, name_zh: (p as { name_zh?: string }).name_zh ?? null }]));
  return seed.sites.map((s) => {
    const raw = s.events as SeedEvent[];
    const disputed = raw.some((e) => e.conflict_with);
    const events: LegacyEvent[] = raw.map((e) => ({
      type: e.type,
      yearStart: e.year_start ?? null,
      yearEnd: e.year_end ?? null,
      description: e.description ?? null,
      person: e.person ? people.get(e.person) ?? null : null,
      disputed: disputed && e.type === "founded",
      sources: e.sources.map((k) => src.get(k)!).filter(Boolean),
    }));
    const years = events.map((e) => e.yearStart).filter((y): y is number => y != null);
    const sources = [...new Map(events.flatMap((e) => e.sources).map((x) => [x.url, x])).values()];
    const site = s as typeof s & { name_zh?: string; alt_names?: string[]; current_status?: string };
    return {
      slug: s.slug, region: seed.region, nameEn: s.name_en, nameZh: site.name_zh ?? null,
      altNames: site.alt_names ?? [], type: s.site_type as Site["type"], lat: s.lat, lng: s.lng,
      precision: s.geo_precision as Site["precision"], currentStatus: site.current_status ?? null,
      summary: s.summary ?? null, disputed, earliestYear: years.length ? Math.min(...years) : null,
      events, sources,
    };
  });
}

async function fromSupabase(): Promise<Site[]> {
  const db = createClient(url!, key!, { auth: { persistSession: false } });
  const [sites, events, claims] = await Promise.all([
    db.from("public_sites").select("*"),
    db.from("events").select("id,site_id,event_type,year_start,year_end,description,status,persons(name_en,name_zh)"),
    db.from("claims").select("subject_kind,subject_id,sources(url,title,publisher)"),
  ]);
  if (sites.error || events.error || claims.error) {
    throw new Error(sites.error?.message ?? events.error?.message ?? claims.error?.message);
  }
  const srcBy = new Map<string, Source[]>();
  for (const c of claims.data ?? []) {
    const s = c.sources as unknown as Source | null;
    if (!s) continue;
    const list = srcBy.get(c.subject_id) ?? [];
    if (!list.some((x) => x.url === s.url)) list.push(s);
    srcBy.set(c.subject_id, list);
  }
  const evBy = new Map<string, LegacyEvent[]>();
  for (const e of events.data ?? []) {
    const list = evBy.get(e.site_id) ?? [];
    list.push({
      type: e.event_type, yearStart: e.year_start, yearEnd: e.year_end, description: e.description,
      person: (e.persons as unknown as LegacyEvent["person"]) ?? null,
      disputed: e.status === "conflict", sources: srcBy.get(e.id) ?? [],
    });
    evBy.set(e.site_id, list);
  }
  return (sites.data ?? []).filter((s) => s.lat != null).map((s) => {
    const evs = evBy.get(s.id) ?? [];
    const sources = [...new Map([...(srcBy.get(s.id) ?? []), ...evs.flatMap((e) => e.sources)].map((x) => [x.url, x])).values()];
    return {
      slug: s.slug, region: s.region, nameEn: s.name_en, nameZh: s.name_zh, altNames: s.alt_names ?? [],
      type: s.site_type, lat: s.lat, lng: s.lng, precision: s.geo_precision, currentStatus: s.current_status,
      summary: s.summary, disputed: s.status === "conflict", earliestYear: s.earliest_year, events: evs, sources,
    };
  });
}

export async function getSites(): Promise<{ sites: Site[]; live: boolean }> {
  if (url && key) {
    try {
      return { sites: await fromSupabase(), live: true };
    } catch (err) {
      console.error("Supabase unavailable, using seed:", err);
    }
  }
  return { sites: fromSeed(), live: false };
}
