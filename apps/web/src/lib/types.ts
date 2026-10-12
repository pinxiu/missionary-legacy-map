export type SiteType =
  | "church" | "hospital" | "school" | "residence" | "cemetery"
  | "mission_station" | "press" | "orphanage" | "other";

export type Source = { url: string; title: string | null; publisher: string | null };

export type LegacyEvent = {
  type: string;
  yearStart: number | null;
  yearEnd: number | null;
  description: string | null;
  person: { name_en: string; name_zh: string | null } | null;
  disputed: boolean;
  sources: Source[];
};

export type Site = {
  slug: string;
  region: string;
  nameEn: string;
  nameZh: string | null;
  altNames: string[];
  type: SiteType;
  lat: number;
  lng: number;
  precision: "exact" | "street" | "district" | "city" | "province";
  currentStatus: string | null;
  summary: string | null;
  disputed: boolean;
  earliestYear: number | null;
  events: LegacyEvent[];
  sources: Source[];
};
