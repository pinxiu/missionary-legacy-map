import "server-only";
import { createClient } from "@supabase/supabase-js";
import hangzhou from "@/data/hangzhou.json";
import zhejiang from "@/data/zhejiang.json";
import type { Area, LegacyEvent, Person, Photo, Region, Site, Source } from "./types";

const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
const key = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY;

/** Cities (filter chips). `parent` is the containing area in AREAS. */
export const REGIONS: Region[] = [
  { slug: "hangzhou", nameEn: "Hangzhou", nameZh: "杭州", parent: "zhejiang" },
  { slug: "ningbo", nameEn: "Ningbo", nameZh: "宁波", parent: "zhejiang" },
  { slug: "wenzhou", nameEn: "Wenzhou", nameZh: "温州", parent: "zhejiang" },
  { slug: "shaoxing", nameEn: "Shaoxing", nameZh: "绍兴", parent: "zhejiang" },
  { slug: "huzhou", nameEn: "Huzhou", nameZh: "湖州", parent: "zhejiang" },
  { slug: "jiaxing", nameEn: "Jiaxing", nameZh: "嘉兴", parent: "zhejiang" },
  { slug: "taizhou", nameEn: "Taizhou", nameZh: "台州", parent: "zhejiang" },
  { slug: "jinhua", nameEn: "Jinhua", nameZh: "金华", parent: "zhejiang" },
  { slug: "lishui", nameEn: "Lishui", nameZh: "丽水", parent: "zhejiang" },
  { slug: "quzhou", nameEn: "Quzhou", nameZh: "衢州", parent: "zhejiang" },
  { slug: "zhoushan", nameEn: "Zhoushan", nameZh: "舟山", parent: "zhejiang" },
];

/** Map extents of areas, [west, south, east, north]. An area without an entry is fitted to its sites. */
const AREA_BBOX: Record<string, [number, number, number, number]> = {
  zhejiang: [118.0, 27.0, 122.95, 31.2],
  jiangsu: [116.3, 30.7, 122.0, 35.2],
  china: [73.5, 18.1, 134.8, 53.6],
  world: [-170, -56, 180, 72],
};

/** Offline fallback for the area tree; online it comes from the `regions` table (getRegions). */
export const AREAS: Area[] = [
  { slug: "zhejiang", nameEn: "Zhejiang", nameZh: "浙江", parent: "china", bbox: AREA_BBOX.zhejiang },
  { slug: "china", nameEn: "China", nameZh: "中国", parent: "world", bbox: AREA_BBOX.china },
  { slug: "world", nameEn: "World", nameZh: "世界", parent: null, bbox: AREA_BBOX.world },
];

/**
 * Provinces/countries (areas) and their cities (regions), from the `regions` table, so a new province and its cities
 * appear on the map as soon as they exist in the database. Anything directly under China counts as an area, even a
 * municipality with no cities of its own.
 */
export async function getRegions(): Promise<{ regions: Region[]; areas: Area[] }> {
  if (url && key) {
    try {
      const { data, error } = await createClient(url, key).from("regions").select("slug,name_en,name_zh,parent");
      if (error) throw error;
      const rows = data ?? [];
      const parents = new Set(rows.map((r) => r.parent).filter(Boolean));
      const isArea = (r: { slug: string; parent: string | null }) => parents.has(r.slug) || r.parent === "china";
      return {
        areas: rows.filter(isArea).map((r) => ({ slug: r.slug, nameEn: r.name_en, nameZh: r.name_zh, parent: r.parent, bbox: AREA_BBOX[r.slug] })),
        regions: rows.filter((r) => !isArea(r)).map((r) => ({ slug: r.slug, nameEn: r.name_en, nameZh: r.name_zh, parent: r.parent ?? undefined })),
      };
    } catch (err) {
      console.error("Regions unavailable, using the built-in list:", err);
    }
  }
  return { regions: REGIONS, areas: AREAS };
}

/* ---------- offline seed (used when Supabase is not configured or unreachable) ---------- */

type SeedSource = { key: string; url: string; title: string; title_zh?: string; publisher?: string; publisher_zh?: string };
type SeedPerson = { slug: string; name_en: string; name_zh?: string; name_zh_status?: string };
type SeedEvent = {
  type: string; year_start?: number; year_end?: number; description?: string; description_zh?: string;
  person?: string; sources: string[]; conflict_with?: string;
};
type SeedPhoto = { image_url: string; caption?: string; caption_zh?: string; credit?: string; license?: string; source_url?: string; year_taken?: number };
type SeedSite = {
  slug: string; region?: string; name_en: string; name_zh?: string; name_zh_status?: string; alt_names?: string[];
  site_type: string; lat: number; lng: number; geo_precision: string; current_status?: string; current_status_zh?: string;
  summary?: string; summary_zh?: string; disputed?: boolean; events: SeedEvent[]; photos?: SeedPhoto[];
};
type SeedFile = { region: string; sources: SeedSource[]; persons: SeedPerson[]; sites: SeedSite[] };

function fromSeed(): Site[] {
  const files = [hangzhou, zhejiang] as unknown as SeedFile[];
  const src = new Map<string, Source>();
  const people = new Map<string, Person>();
  for (const f of files) {
    f.sources.forEach((s) => src.set(s.key, { url: s.url, title: s.title, titleZh: s.title_zh ?? null, publisher: s.publisher ?? null, publisherZh: s.publisher_zh ?? null }));
    f.persons.forEach((p) => people.set(p.slug, { nameEn: p.name_en, nameZh: p.name_zh ?? null, nameZhTranslated: p.name_zh_status === "translated" }));
  }
  return files.flatMap((f) => f.sites.map((s): Site => {
    const disputed = !!s.disputed || s.events.some((e) => e.conflict_with);
    const events: LegacyEvent[] = s.events.map((e) => ({
      type: e.type, yearStart: e.year_start ?? null, yearEnd: e.year_end ?? null,
      description: e.description ?? null, descriptionZh: e.description_zh ?? null,
      person: e.person ? people.get(e.person) ?? null : null,
      disputed: disputed && (!!e.conflict_with || e.type === "founded" || e.type === "built"),
      sources: e.sources.map((k) => src.get(k)).filter((x): x is Source => !!x),
    }));
    const years = events.map((e) => e.yearStart).filter((y): y is number => y != null);
    return {
      id: null, slug: s.slug, region: s.region ?? f.region, nameEn: s.name_en, nameZh: s.name_zh ?? null,
      nameZhTranslated: s.name_zh_status === "translated", altNames: s.alt_names ?? [], type: s.site_type as Site["type"],
      lat: s.lat, lng: s.lng, precision: s.geo_precision as Site["precision"],
      currentStatus: s.current_status ?? null, currentStatusZh: s.current_status_zh ?? null,
      summary: s.summary ?? null, summaryZh: s.summary_zh ?? null, disputed,
      earliestYear: years.length ? Math.min(...years) : null, events,
      sources: [...new Map(events.flatMap((e) => e.sources).map((x) => [x.url, x])).values()],
      photos: (s.photos ?? []).map((p, i) => ({
        id: `${s.slug}-${i}`, url: p.image_url, kind: "historical", caption: p.caption ?? null, captionZh: p.caption_zh ?? null,
        credit: p.credit ?? null, license: p.license ?? null, sourceUrl: p.source_url ?? null, yearTaken: p.year_taken ?? null,
      })),
    };
  }));
}

/* ---------- live data ---------- */

type SourceRow = { url: string; title: string | null; title_zh: string | null; publisher: string | null; publisher_zh: string | null };
const toSource = (s: SourceRow): Source => ({ url: s.url, title: s.title, titleZh: s.title_zh, publisher: s.publisher, publisherZh: s.publisher_zh });

async function fromSupabase(): Promise<Site[]> {
  const db = createClient(url!, key!, { auth: { persistSession: false } });
  const [sites, events, claims, photos] = await Promise.all([
    db.from("public_sites").select("*"),
    db.from("events").select("id,site_id,event_type,year_start,year_end,description,description_zh,status,persons(name_en,name_zh,name_zh_status)"),
    db.from("claims").select("subject_kind,subject_id,sources(url,title,title_zh,publisher,publisher_zh)"),
    db.from("photos").select("id,site_id,kind,public_path,image_url,caption,caption_zh,credit,license,source_url,year_taken,created_at").order("created_at"),
  ]);
  const err = sites.error ?? events.error ?? claims.error ?? photos.error;
  if (err) throw new Error(err.message);

  const srcBy = new Map<string, Source[]>();
  for (const c of claims.data ?? []) {
    const s = c.sources as unknown as SourceRow | null;
    if (!s) continue;
    const list = srcBy.get(c.subject_id) ?? [];
    if (!list.some((x) => x.url === s.url)) list.push(toSource(s));
    srcBy.set(c.subject_id, list);
  }
  const evBy = new Map<string, LegacyEvent[]>();
  for (const e of events.data ?? []) {
    const p = e.persons as unknown as { name_en: string; name_zh: string | null; name_zh_status: string | null } | null;
    const list = evBy.get(e.site_id) ?? [];
    list.push({
      type: e.event_type, yearStart: e.year_start, yearEnd: e.year_end, description: e.description, descriptionZh: e.description_zh,
      person: p ? { nameEn: p.name_en, nameZh: p.name_zh, nameZhTranslated: p.name_zh_status === "translated" } : null,
      disputed: e.status === "conflict", sources: srcBy.get(e.id) ?? [],
    });
    evBy.set(e.site_id, list);
  }
  const photoBy = new Map<string, Photo[]>();
  for (const p of photos.data ?? []) {
    const src = p.public_path ? `${url}/storage/v1/object/public/site-photos/${p.public_path}` : p.image_url;
    if (!src) continue;
    const list = photoBy.get(p.site_id) ?? [];
    list.push({ id: p.id, url: src, kind: p.kind, caption: p.caption, captionZh: p.caption_zh, credit: p.credit,
      license: p.license, sourceUrl: p.source_url, yearTaken: p.year_taken });
    photoBy.set(p.site_id, list);
  }
  return (sites.data ?? []).filter((s) => s.lat != null).map((s): Site => {
    const evs = evBy.get(s.id) ?? [];
    return {
      id: s.id, slug: s.slug, region: s.region, nameEn: s.name_en, nameZh: s.name_zh,
      nameZhTranslated: s.name_zh_status === "translated", altNames: s.alt_names ?? [], type: s.site_type,
      lat: s.lat, lng: s.lng, precision: s.geo_precision, currentStatus: s.current_status, currentStatusZh: s.current_status_zh,
      summary: s.summary, summaryZh: s.summary_zh, disputed: s.status === "conflict", earliestYear: s.earliest_year, events: evs,
      sources: [...new Map([...(srcBy.get(s.id) ?? []), ...evs.flatMap((e) => e.sources)].map((x) => [x.url, x])).values()],
      photos: photoBy.get(s.id) ?? [],
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
