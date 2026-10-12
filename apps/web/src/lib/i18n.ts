import type { Site, SiteType } from "./types";

export type Lang = "en" | "zh";

export const TYPE_COLOR: Record<SiteType, string> = {
  church: "#8a3b2a", hospital: "#2f6f6a", school: "#3d5a99", cemetery: "#6b6258", mission_station: "#a6782b",
  residence: "#7a5a8c", press: "#4f6b3a", orphanage: "#b05a7a", other: "#555555",
};

const TYPE_LABEL: Record<SiteType, [string, string]> = {
  church: ["Church", "教堂"], hospital: ["Hospital", "医院"], school: ["School", "学校"], cemetery: ["Cemetery", "墓地"],
  mission_station: ["Mission station", "宣教站"], residence: ["Residence", "故居"], press: ["Press", "印书馆"],
  orphanage: ["Orphanage", "孤儿院"], other: ["Other", "其他"],
};

const EVENT_LABEL: Record<string, [string, string]> = {
  founded: ["Founded", "创立"], built: ["Built", "建成"], rebuilt: ["Rebuilt", "重建"], moved: ["Moved", "迁址"],
  served: ["Served", "服事"], closed: ["Closed", "停办"], buried: ["Buried", "安葬"], baptism: ["Baptism", "洗礼"],
  renamed: ["Renamed", "更名"], opened: ["Opened", "开办"],
};

const PRECISION: Record<Site["precision"], [string, string]> = {
  exact: ["Exact location", "精确位置"],
  street: ["Location accurate to the street", "位置精确到街道"],
  district: ["Approximate location (district)", "大致位置（城区）"],
  city: ["Location not yet pinned; shown near the city centre", "具体位置待定，暂标于市中心附近"],
  province: ["Location not yet pinned; shown at province centre", "具体位置待定，暂标于省中心"],
};

const UI = {
  title: ["Legacy Map", "宣教遗迹地图"],
  subtitle: ["Zhejiang · {n} sites, every fact cited", "浙江 · {n} 处遗迹，每条史实均有出处"],
  offline: [" · offline seed", " · 离线数据"],
  nearMe: ["Near me", "附近"], locating: ["Locating…", "定位中…"], all: ["All", "全部"],
  allCities: ["All cities", "全部城市"], back: ["← All sites", "← 全部遗迹"],
  disagree: ["Sources disagree", "来源记载不一"], also: ["Also", "别名"], today: ["Today", "现状"],
  timeline: ["Timeline", "历史沿革"], sources: ["Sources", "资料来源"], photos: ["Photos", "照片"],
  fromYou: ["from you", "距您"], correct: ["Suggest an edit", "建议修改"],
  far: ["The nearest recorded site is {d} away. All sites stay on the map wherever you are.",
        "最近的遗迹距您 {d}。无论您在哪里，所有遗迹都会显示在地图上。"],
  foot: ["Historical records only, up to 1952.", "仅收录1952年以前的历史记录。"], how: ["How this is built", "项目说明"],
  translated: ["Chinese name translated by us, not yet found in a source", "中文名为本站翻译，尚未见于史料"],
  addPhoto: ["Add a photo", "添加照片"],
  firstPhoto: ["No photos yet. If you have visited this place, be the first to add one.", "还没有照片。如果您到过这里，欢迎成为第一位上传照片的人。"],
  needsPhoto: ["needs a photo", "待补照片"],
  photoCta: ["{n} sites still need photos. Visiting one? Add yours.", "还有 {n} 处遗迹缺少照片。路过时，欢迎拍一张上传。"],
  credit: ["Credit", "来源"],
  addSite: ["Add a site", "添加遗迹"], feedback: ["Feedback", "意见反馈"],
} as const;

export type UIKey = keyof typeof UI;

export function t(lang: Lang, key: UIKey, vars: Record<string, string | number> = {}) {
  let s: string = UI[key][lang === "zh" ? 1 : 0];
  for (const [k, v] of Object.entries(vars)) s = s.replace(`{${k}}`, String(v));
  return s;
}

export const typeLabel = (lang: Lang, ty: SiteType) => TYPE_LABEL[ty][lang === "zh" ? 1 : 0];
export const eventLabel = (lang: Lang, ev: string) => (EVENT_LABEL[ev] ?? [ev, ev])[lang === "zh" ? 1 : 0];
export const precisionLabel = (lang: Lang, p: Site["precision"]) => PRECISION[p][lang === "zh" ? 1 : 0];

/** Pick the string for the current language, falling back to the other one. */
export const pick = (lang: Lang, en: string | null | undefined, zh: string | null | undefined) =>
  (lang === "zh" ? zh || en : en || zh) ?? "";
