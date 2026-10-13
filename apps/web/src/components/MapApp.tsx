"use client";

import { LngLatBounds, Map as MLMap, NavigationControl, setWorkerUrl, type ExpressionSpecification, type GeoJSONSource } from "maplibre-gl";
import { useEffect, useMemo, useRef, useState } from "react";
import ContributeCard, { type CardMode } from "./ContributeCard";
import PhotoUpload from "./PhotoUpload";
import { eventLabel, type Lang, pick, precisionLabel, t, TYPE_COLOR, typeLabel } from "@/lib/i18n";
import type { Region, Site, SiteType, Source } from "@/lib/types";

const STYLE_URL = "https://tiles.openfreemap.org/styles/positron";
const REPO = "https://github.com/pinxiu/missionary-legacy-map";

const COLOR_EXPR = [
  "match", ["get", "type"], ...Object.entries(TYPE_COLOR).flatMap(([k, v]) => [k, v]), "#555",
] as unknown as ExpressionSpecification;

function km(a: [number, number], b: [number, number]) {
  const R = 6371, rad = Math.PI / 180;
  const dLat = (b[0] - a[0]) * rad, dLng = (b[1] - a[1]) * rad;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a[0] * rad) * Math.cos(b[0] * rad) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}
const fmtKm = (d: number) => (d < 1 ? `${Math.round(d * 1000)} m` : d < 100 ? `${d.toFixed(1)} km` : `${Math.round(d).toLocaleString()} km`);
const years = (a: number | null, b: number | null) => (a == null ? "" : b && b !== a ? `${a}–${b}` : String(a));

/** Unpinned (city-precision) sites share a centroid; fan them out on a small ring so each stays tappable. */
function displayCoords(sites: Site[]): Map<string, [number, number]> {
  const out = new Map<string, [number, number]>();
  const groups = new Map<string, Site[]>();
  for (const s of sites) {
    if (s.precision === "city" || s.precision === "province") {
      const k = `${s.lat.toFixed(3)},${s.lng.toFixed(3)}`;
      groups.set(k, [...(groups.get(k) ?? []), s]);
    } else out.set(s.slug, [s.lng, s.lat]);
  }
  for (const g of groups.values()) {
    g.forEach((s, i) => {
      const angle = (2 * Math.PI * i) / g.length, r = g.length > 1 ? 0.012 : 0;
      out.set(s.slug, [s.lng + r * Math.cos(angle), s.lat + r * Math.sin(angle) * 0.85]);
    });
  }
  return out;
}

const sourceLabel = (lang: Lang, s: Source) => pick(lang, s.publisher, s.publisherZh) || new URL(s.url).hostname;

export default function MapApp({ sites, regions, live }: { sites: Site[]; regions: Region[]; live: boolean }) {
  const mapEl = useRef<HTMLDivElement>(null);
  const map = useRef<MLMap | null>(null);
  const [lang, setLang] = useState<Lang>("en");
  const [selected, setSelected] = useState<string | null>(null);
  const panelEl = useRef<HTMLElement>(null);
  const listScroll = useRef(0);
  const selectedRef = useRef<string | null>(null);
  /** Open a site card; remember the list position first so "back" returns to it. */
  const openSite = (slug: string) => {
    if (!selectedRef.current) listScroll.current = panelEl.current?.scrollTop ?? 0;
    setSelected(slug);
  };
  const [types, setTypes] = useState<Set<SiteType>>(new Set());
  const [city, setCity] = useState<string | null>(null);
  const [me, setMe] = useState<[number, number] | null>(null);
  const [locating, setLocating] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [lightbox, setLightbox] = useState<string | null>(null);
  const [card, setCard] = useState<CardMode | null>(null);
  const mapCenter = (): [number, number] => { const c = map.current?.getCenter(); return c ? [c.lng, c.lat] : [120.155, 30.27]; };

  // Language: saved choice, else the browser's preference.
  useEffect(() => {
    let saved: string | null = null;
    try { saved = localStorage.getItem("lang"); } catch { /* storage unavailable */ }
    const initial = (saved === "zh" || saved === "en") ? saved : navigator.language.toLowerCase().startsWith("zh") ? "zh" : "en";
    setLang(initial);
  }, []);
  useEffect(() => {
    document.documentElement.lang = lang === "zh" ? "zh-Hans" : "en";
    try { localStorage.setItem("lang", lang); } catch { /* storage unavailable */ }
  }, [lang]);

  const coords = useMemo(() => displayCoords(sites), [sites]);
  const presentTypes = useMemo(() => [...new Set(sites.map((s) => s.type))], [sites]);
  const presentCities = useMemo(() => regions.filter((r) => sites.some((s) => s.region === r.slug)), [regions, sites]);
  const visible = useMemo(() => {
    let list = types.size ? sites.filter((s) => types.has(s.type)) : sites;
    if (city) list = list.filter((s) => s.region === city);
    return me
      ? [...list].sort((a, b) => km(me, [a.lat, a.lng]) - km(me, [b.lat, b.lng]))
      : [...list].sort((a, b) => (a.earliestYear ?? 9999) - (b.earliestYear ?? 9999));
  }, [sites, types, city, me]);
  const site = sites.find((s) => s.slug === selected) ?? null;
  const needPhotos = sites.filter((s) => !s.photos.length).length;
  const regionName = (slug: string) => { const r = regions.find((x) => x.slug === slug); return r ? pick(lang, r.nameEn, r.nameZh) : slug; };

  const geojson = useMemo(() => ({
    type: "FeatureCollection" as const,
    features: visible.map((s) => ({
      type: "Feature" as const,
      geometry: { type: "Point" as const, coordinates: coords.get(s.slug) ?? [s.lng, s.lat] },
      properties: { slug: s.slug, type: s.type, approx: s.precision === "city" || s.precision === "province" ? 1 : 0 },
    })),
  }), [visible, coords]);

  const padding = () => (window.innerWidth >= 820
    ? { top: 60, bottom: 60, left: 430, right: 60 }
    : { top: 70, bottom: window.innerHeight * 0.55, left: 40, right: 40 });

  function fit(list: Site[], animate = true) {
    if (!map.current || !list.length) return;
    const b = new LngLatBounds();
    list.forEach((s) => b.extend(coords.get(s.slug) ?? [s.lng, s.lat]));
    map.current.fitBounds(b, { padding: padding(), maxZoom: 14, duration: animate ? 800 : 0 });
  }

  useEffect(() => {
    if (!mapEl.current || map.current) return;
    setWorkerUrl("/maplibre-gl-worker.mjs");
    const m = new MLMap({ container: mapEl.current, style: STYLE_URL, center: [120.4, 29.4], zoom: 7, attributionControl: { compact: true } });
    m.on("error", (e) => console.error("map error", e.error ?? e));
    m.addControl(new NavigationControl({ showCompass: false }), "top-right");
    m.on("load", () => {
      m.addSource("sites", { type: "geojson", data: geojson });
      m.addLayer({ id: "sites-halo", type: "circle", source: "sites", paint: { "circle-radius": 13, "circle-color": "#fff", "circle-opacity": 0.85 } });
      m.addLayer({
        id: "sites", type: "circle", source: "sites",
        paint: {
          "circle-radius": 8, "circle-color": COLOR_EXPR, "circle-stroke-width": 2, "circle-stroke-color": COLOR_EXPR,
          "circle-opacity": ["case", ["==", ["get", "approx"], 1], 0.35, 1],
        },
      });
      fit(sites, false);
      m.on("click", "sites", (e) => {
        const slug = e.features?.[0]?.properties?.slug as string | undefined;
        if (slug) { openSite(slug); setUploading(false); setCard(null); }
      });
      m.on("mouseenter", "sites", () => (m.getCanvas().style.cursor = "pointer"));
      m.on("mouseleave", "sites", () => (m.getCanvas().style.cursor = ""));
    });
    map.current = m;
    return () => { m.remove(); map.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => { (map.current?.getSource("sites") as GeoJSONSource | undefined)?.setData(geojson); }, [geojson]);

  useEffect(() => {
    if (site && map.current) {
      map.current.flyTo({ center: coords.get(site.slug) ?? [site.lng, site.lat], zoom: Math.max(map.current.getZoom(), 13), padding: padding() });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [site]);

  // Opening a site always starts at the top of its card; going back restores where you were in the list.
  useEffect(() => {
    selectedRef.current = selected;
    const el = panelEl.current;
    if (!el) return;
    el.scrollTop = selected ? 0 : listScroll.current;
  }, [selected]);

  function locate() {
    if (!navigator.geolocation) return;
    setLocating(true);
    navigator.geolocation.getCurrentPosition(
      (p) => { setMe([p.coords.latitude, p.coords.longitude]); setLocating(false); },
      () => setLocating(false),
      { enableHighAccuracy: false, timeout: 10000 },
    );
  }

  function chooseCity(slug: string | null) {
    setCity(slug);
    setSelected(null);
    fit(slug ? sites.filter((s) => s.region === slug) : sites);
  }

  const nearestKm = me && visible[0] ? km(me, [visible[0].lat, visible[0].lng]) : null;
  const other = (en: string | null | undefined, zh: string | null | undefined) => (lang === "zh" ? en : zh);

  return (
    <main className="app">
      <div ref={mapEl} className="map" aria-label={t(lang, "title")} />

      <section className="panel" aria-live="polite" ref={panelEl}>
        <header className="panel-head">
          <div>
            <h1>{t(lang, "title")}</h1>
            <p className="sub">{t(lang, "subtitle", { n: sites.length })}{live ? "" : t(lang, "offline")}</p>
          </div>
          <div className="actions">
            <button className="lang" onClick={() => setLang(lang === "zh" ? "en" : "zh")} aria-label="Switch language / 切换语言">
              {lang === "zh" ? "EN" : "中文"}
            </button>
            <button onClick={locate} disabled={locating}>{locating ? t(lang, "locating") : t(lang, "nearMe")}</button>
          </div>
        </header>

        {site ? (
          <article className="detail">
            <button className="back" onClick={() => { setSelected(null); setUploading(false); setCard(null); }}>{t(lang, "back")}</button>
            <div className="chips">
              <span className="chip" style={{ background: TYPE_COLOR[site.type] }}>{typeLabel(lang, site.type)}</span>
              <span className="chip ghost">{regionName(site.region)}</span>
              {site.disputed && <span className="chip warn">{t(lang, "disagree")}</span>}
            </div>
            <h2>
              {pick(lang, site.nameEn, site.nameZh)}
              {lang === "zh" && site.nameZhTranslated && <abbr className="yi" title={t(lang, "translated")}>译</abbr>}
            </h2>
            {other(site.nameEn, site.nameZh) && (
              <p className="second">
                {other(site.nameEn, site.nameZh)}
                {lang === "en" && site.nameZhTranslated && <abbr className="yi" title={t(lang, "translated")}>译</abbr>}
              </p>
            )}
            {site.altNames.length > 0 && <p className="alt">{t(lang, "also")}: {site.altNames.join(" · ")}</p>}

            {site.photos.length > 0 && (
              <div className="gallery">
                {site.photos.map((p) => (
                  <figure key={p.id}>
                    <button className="thumb" onClick={() => setLightbox(p.url)}>
                      <img src={p.url} alt={pick(lang, p.caption, p.captionZh) || pick(lang, site.nameEn, site.nameZh)} loading="lazy" />
                    </button>
                    <figcaption>
                      {pick(lang, p.caption, p.captionZh)}{p.yearTaken ? ` (${p.yearTaken})` : ""}
                      <span className="credit">
                        {p.credit ? ` ${t(lang, "credit")}: ${p.credit}` : ""}{p.license ? ` · ${p.license}` : ""}
                        {p.sourceUrl && <> · <a href={p.sourceUrl} target="_blank" rel="noreferrer">↗</a></>}
                      </span>
                    </figcaption>
                  </figure>
                ))}
              </div>
            )}

            {uploading ? (
              <PhotoUpload siteId={site.id} lang={lang} onClose={() => setUploading(false)} />
            ) : site.photos.length === 0 ? (
              <button className="photo-cta" onClick={() => setUploading(true)}>
                <span className="cam" aria-hidden>📷</span>
                <span><strong>{t(lang, "addPhoto")}</strong><br />{t(lang, "firstPhoto")}</span>
              </button>
            ) : (
              <button className="add-photo" onClick={() => setUploading(true)}>📷 {t(lang, "addPhoto")}</button>
            )}

            {pick(lang, site.summary, site.summaryZh) && <p>{pick(lang, site.summary, site.summaryZh)}</p>}
            {pick(lang, site.currentStatus, site.currentStatusZh) && (
              <p className="now"><strong>{t(lang, "today")}:</strong> {pick(lang, site.currentStatus, site.currentStatusZh)}</p>
            )}
            <p className="precision">{precisionLabel(lang, site.precision)}{me ? ` · ${t(lang, "fromYou")} ${fmtKm(km(me, [site.lat, site.lng]))}` : ""}</p>

            <h3>{t(lang, "timeline")}</h3>
            <ol className="timeline">
              {[...site.events].sort((a, b) => (a.yearStart ?? 9999) - (b.yearStart ?? 9999)).map((e, i) => (
                <li key={i} className={e.disputed ? "disputed" : ""}>
                  <span className="year">{years(e.yearStart, e.yearEnd) || "—"}</span>
                  <div>
                    <span className="etype">{eventLabel(lang, e.type)}</span>
                    {e.person && (
                      <span className="person"> · {pick(lang, e.person.nameEn, e.person.nameZh)} {other(e.person.nameEn, e.person.nameZh) ?? ""}
                        {e.person.nameZhTranslated && <abbr className="yi" title={t(lang, "translated")}>译</abbr>}
                      </span>
                    )}
                    {pick(lang, e.description, e.descriptionZh) && <p>{pick(lang, e.description, e.descriptionZh)}</p>}
                    <p className="cites">{e.sources.map((s, j) => <a key={j} href={s.url} target="_blank" rel="noreferrer">[{sourceLabel(lang, s)}]</a>)}</p>
                  </div>
                </li>
              ))}
            </ol>

            <h3>{t(lang, "sources")}</h3>
            <ul className="sources">
              {site.sources.map((s) => (
                <li key={s.url}>
                  <a href={s.url} target="_blank" rel="noreferrer">{pick(lang, s.title, s.titleZh) || s.url}</a>
                  {pick(lang, s.publisher, s.publisherZh) ? ` — ${pick(lang, s.publisher, s.publisherZh)}` : ""}
                </li>
              ))}
            </ul>
            {card === "edit"
              ? <ContributeCard mode="edit" site={site} lang={lang} center={mapCenter()} onClose={() => setCard(null)} />
              : <button className="add-photo" onClick={() => setCard("edit")}>✎ {t(lang, "correct")}</button>}
          </article>
        ) : (
          <>
            <div className="filters cities" role="group" aria-label="City">
              <button aria-pressed={city === null} onClick={() => chooseCity(null)}>{t(lang, "allCities")}</button>
              {presentCities.map((r) => (
                <button key={r.slug} aria-pressed={city === r.slug} onClick={() => chooseCity(city === r.slug ? null : r.slug)}>
                  {pick(lang, r.nameEn, r.nameZh)}
                </button>
              ))}
            </div>
            <div className="filters" role="group" aria-label="Type">
              {presentTypes.map((ty) => (
                <button key={ty} aria-pressed={types.has(ty)}
                  onClick={() => setTypes((prev) => { const n = new Set(prev); if (n.has(ty)) n.delete(ty); else n.add(ty); return n; })}>
                  <span className="dot" style={{ background: TYPE_COLOR[ty] }} />{typeLabel(lang, ty)}
                </button>
              ))}
            </div>
            {card === "new_site" || card === "feedback" ? (
              <ContributeCard mode={card} lang={lang} center={mapCenter()} onClose={() => setCard(null)} />
            ) : (
              <div className="contribute">
                <button onClick={() => setCard("new_site")}>📍 {t(lang, "addSite")}</button>
                <button onClick={() => setCard("feedback")}>💬 {t(lang, "feedback")}</button>
              </div>
            )}
            {needPhotos > 0 && <p className="photo-banner">📷 {t(lang, "photoCta", { n: needPhotos })}</p>}
            {nearestKm != null && nearestKm > 50 && <p className="far">{t(lang, "far", { d: fmtKm(nearestKm) })}</p>}
            <ul className="list">
              {visible.map((s) => (
                <li key={s.slug}>
                  <button onClick={() => { openSite(s.slug); setCard(null); }}>
                    {s.photos[0]
                      ? <img className="mini" src={s.photos[0].url} alt="" loading="lazy" />
                      : <span className="mini empty" style={{ borderColor: TYPE_COLOR[s.type] }} title={t(lang, "needsPhoto")}>📷</span>}
                    <span className="name">
                      {pick(lang, s.nameEn, s.nameZh)}
                      <span className="sub-name">{other(s.nameEn, s.nameZh) ?? ""} · {regionName(s.region)}</span>
                    </span>
                    <span className="meta">{me ? fmtKm(km(me, [s.lat, s.lng])) : s.earliestYear ?? ""}</span>
                  </button>
                </li>
              ))}
            </ul>
            <p className="foot">{t(lang, "foot")} <a href={REPO} target="_blank" rel="noreferrer">{t(lang, "how")}</a></p>
          </>
        )}
      </section>

      {lightbox && (
        <button className="lightbox" onClick={() => setLightbox(null)} aria-label="Close">
          <img src={lightbox} alt="" />
        </button>
      )}
    </main>
  );
}
