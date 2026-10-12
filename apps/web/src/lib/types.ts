export type SiteType =
  | "church" | "hospital" | "school" | "residence" | "cemetery"
  | "mission_station" | "press" | "orphanage" | "other";

export type Source = { url: string; title: string | null; titleZh: string | null; publisher: string | null; publisherZh: string | null };

export type Person = { nameEn: string; nameZh: string | null; nameZhTranslated: boolean };

export type LegacyEvent = {
  type: string;
  yearStart: number | null;
  yearEnd: number | null;
  description: string | null;
  descriptionZh: string | null;
  person: Person | null;
  disputed: boolean;
  sources: Source[];
};

export type Photo = {
  id: string;
  url: string;
  kind: "historical" | "visitor";
  caption: string | null;
  captionZh: string | null;
  credit: string | null;
  license: string | null;
  sourceUrl: string | null;
  yearTaken: number | null;
};

export type Region = { slug: string; nameEn: string; nameZh: string };

export type Site = {
  id: string | null;
  slug: string;
  region: string;
  nameEn: string;
  nameZh: string | null;
  nameZhTranslated: boolean;
  altNames: string[];
  type: SiteType;
  lat: number;
  lng: number;
  precision: "exact" | "street" | "district" | "city" | "province";
  currentStatus: string | null;
  currentStatusZh: string | null;
  summary: string | null;
  summaryZh: string | null;
  disputed: boolean;
  earliestYear: number | null;
  events: LegacyEvent[];
  sources: Source[];
  photos: Photo[];
};
