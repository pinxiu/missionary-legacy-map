"use client";

import { LngLatBounds, Map as MLMap, NavigationControl, setWorkerUrl, type ExpressionSpecification, type GeoJSONSource } from "maplibre-gl";
import { useEffect, useMemo, useRef, useState } from "react";
import type { Site, SiteType } from "@/lib/types";

const STYLE_URL = "https://tiles.openfreemap.org/styles/positron";
const REPO = "https://github.com/pinxiu/missionary-legacy-map";

const TYPE_META: Record<SiteType, { label: string; color: string }> = {
  church: { label: "Church", color: "#8a3b2a" },
  hospital: { label: "Hospital", color: "#2f6f6a" },
  school: { label: "School", color: "#3d5a99" },
  cemetery: { label: "Cemetery", color: "#6b6258" },
  mission_station: { label: "Mission station", color: "#a6782b" },
  residence: { label: "Residence", color: "#7a5a8c" },
  press: { label: "Press", color: "#4f6b3a" },
  orphanage: { label: "Orphanage", color: "#b05a7a" },
  other: { label: "Other", color: "#555" },
};

const TYPE_COLOR = [
  "match", ["get", "type"], ...Object.entries(TYPE_META).flatMap(([k, v]) => [k, v.color]), "#555",
] as unknown as ExpressionSpecification;

const PRECISION_NOTE: Record<Site["precision"], string> = {
  exact: "Exact location",
  street: "Location accurate to the street",
  district: "Approximate location (district)",
  city: "Location not yet pinned; shown at city centre",
  province: "Location not yet pinned; shown at province centre",
};

function km(a: [number, number], b: [number, number]) {
  const R = 6371, rad = Math.PI / 180;
  const dLat = (b[0] - a[0]) * rad, dLng = (b[1] - a[1]) * rad;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a[0] * rad) * Math.cos(b[0] * rad) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

function fmtKm(d: number) {
  return d < 1 ? `${Math.round(d * 1000)} m` : d < 100 ? `${d.toFixed(1)} km` : `${Math.round(d).toLocaleString()} km`;
}

function years(a: number | null, b: number | null) {
  if (a == null) return "";
  return b && b !== a ? `${a}–${b}` : String(a);
}

export default function MapApp({ sites, live }: { sites: Site[]; live: boolean }) {
  const mapEl = useRef<HTMLDivElement>(null);
  const map = useRef<MLMap | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [types, setTypes] = useState<Set<SiteType>>(new Set());
  const [me, setMe] = useState<[number, number] | null>(null);
  const [locating, setLocating] = useState(false);

  const presentTypes = useMemo(() => [...new Set(sites.map((s) => s.type))], [sites]);
  const visible = useMemo(() => {
    const list = types.size ? sites.filter((s) => types.has(s.type)) : sites;
    return me
      ? [...list].sort((a, b) => km(me, [a.lat, a.lng]) - km(me, [b.lat, b.lng]))
      : [...list].sort((a, b) => (a.earliestYear ?? 9999) - (b.earliestYear ?? 9999));
  }, [sites, types, me]);
  const site = sites.find((s) => s.slug === selected) ?? null;

  const geojson = useMemo(() => ({
    type: "FeatureCollection" as const,
    features: visible.map((s) => ({
      type: "Feature" as const,
      geometry: { type: "Point" as const, coordinates: [s.lng, s.lat] },
      properties: { slug: s.slug, type: s.type, approx: s.precision === "city" || s.precision === "province" ? 1 : 0, name: s.nameEn },
    })),
  }), [visible]);

  useEffect(() => {
    if (!mapEl.current || map.current) return;
    setWorkerUrl("/maplibre-gl-worker.mjs");
    const m = new MLMap({
      container: mapEl.current,
      style: STYLE_URL,
      center: [120.155, 30.25],
      zoom: 11.5,
      attributionControl: { compact: true },
    });
    m.on("error", (e) => console.error("map error", e.error ?? e));
    m.addControl(new NavigationControl({ showCompass: false }), "top-right");
    m.on("load", () => {
      m.addSource("sites", { type: "geojson", data: geojson });
      m.addLayer({
        id: "sites-halo", type: "circle", source: "sites",
        paint: { "circle-radius": 14, "circle-color": "#fff", "circle-opacity": 0.85 },
      });
      m.addLayer({
        id: "sites", type: "circle", source: "sites",
        paint: {
          "circle-radius": 8,
          "circle-color": TYPE_COLOR,
          "circle-opacity": ["case", ["==", ["get", "approx"], 1], 0.35, 1],
          "circle-stroke-width": 2,
          "circle-stroke-color": TYPE_COLOR,
        },
      });
      // Frame every site in the part of the map not covered by the panel.
      const desktop = window.innerWidth >= 820;
      const b = new LngLatBounds();
      sites.forEach((s) => b.extend([s.lng, s.lat]));
      if (!b.isEmpty()) {
        m.fitBounds(b, {
          padding: desktop ? { top: 60, bottom: 60, left: 420, right: 60 } : { top: 70, bottom: window.innerHeight * 0.55, left: 40, right: 40 },
          maxZoom: 14, duration: 0,
        });
      }
      m.on("click", "sites", (e) => {
        const slug = e.features?.[0]?.properties?.slug as string | undefined;
        if (slug) setSelected(slug);
      });
      m.on("mouseenter", "sites", () => (m.getCanvas().style.cursor = "pointer"));
      m.on("mouseleave", "sites", () => (m.getCanvas().style.cursor = ""));
    });
    map.current = m;
    return () => { m.remove(); map.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const src = map.current?.getSource("sites") as GeoJSONSource | undefined;
    src?.setData(geojson);
  }, [geojson]);

  useEffect(() => {
    if (site && map.current) map.current.flyTo({ center: [site.lng, site.lat], zoom: Math.max(map.current.getZoom(), 13.5), padding: window.innerWidth >= 820 ? { left: 400, top: 0, right: 0, bottom: 0 } : { bottom: window.innerHeight * 0.55, left: 0, right: 0, top: 0 } });
  }, [site]);

  function locate() {
    if (!navigator.geolocation) return;
    setLocating(true);
    navigator.geolocation.getCurrentPosition(
      (p) => { setMe([p.coords.latitude, p.coords.longitude]); setLocating(false); },
      () => setLocating(false),
      { enableHighAccuracy: false, timeout: 10000 },
    );
  }

  function fitAll() {
    if (!map.current || !visible.length) return;
    const b = new LngLatBounds();
    visible.forEach((s) => b.extend([s.lng, s.lat]));
    map.current.fitBounds(b, { padding: window.innerWidth >= 820 ? { top: 60, bottom: 60, left: 420, right: 60 } : { top: 70, bottom: window.innerHeight * 0.55, left: 40, right: 40 }, maxZoom: 14 });
    setSelected(null);
  }

  const nearestKm = me && visible[0] ? km(me, [visible[0].lat, visible[0].lng]) : null;

  return (
    <main className="app">
      <div ref={mapEl} className="map" aria-label="Map of legacy sites" />

      <section className="panel" aria-live="polite">
        <header className="panel-head">
          <div>
            <h1>Legacy Map</h1>
            <p className="sub">Hangzhou · {sites.length} sites, every fact cited{live ? "" : " · offline seed"}</p>
          </div>
          <div className="actions">
            <button onClick={locate} disabled={locating}>{locating ? "Locating…" : "Near me"}</button>
            <button onClick={fitAll}>All</button>
          </div>
        </header>

        {site ? (
          <article className="detail">
            <button className="back" onClick={() => setSelected(null)}>← All sites</button>
            <div className="chips">
              <span className="chip" style={{ background: TYPE_META[site.type].color }}>{TYPE_META[site.type].label}</span>
              {site.disputed && <span className="chip warn">Sources disagree</span>}
            </div>
            <h2>{site.nameEn}</h2>
            {site.nameZh && <p className="zh">{site.nameZh}</p>}
            {site.altNames.length > 0 && <p className="alt">Also: {site.altNames.join(" · ")}</p>}
            {site.summary && <p>{site.summary}</p>}
            {site.currentStatus && <p className="now"><strong>Today:</strong> {site.currentStatus}</p>}
            <p className="precision">{PRECISION_NOTE[site.precision]}{me ? ` · ${fmtKm(km(me, [site.lat, site.lng]))} from you` : ""}</p>

            <h3>Timeline</h3>
            <ol className="timeline">
              {[...site.events].sort((a, b) => (a.yearStart ?? 9999) - (b.yearStart ?? 9999)).map((e, i) => (
                <li key={i} className={e.disputed ? "disputed" : ""}>
                  <span className="year">{years(e.yearStart, e.yearEnd) || "—"}</span>
                  <div>
                    <span className="etype">{e.type}</span>
                    {e.person && <span className="person"> · {e.person.name_en}{e.person.name_zh ? ` ${e.person.name_zh}` : ""}</span>}
                    {e.description && <p>{e.description}</p>}
                    <p className="cites">
                      {e.sources.map((s, j) => (
                        <a key={j} href={s.url} target="_blank" rel="noreferrer">[{s.publisher ?? new URL(s.url).hostname}]</a>
                      ))}
                    </p>
                  </div>
                </li>
              ))}
            </ol>

            <h3>Sources</h3>
            <ul className="sources">
              {site.sources.map((s) => (
                <li key={s.url}><a href={s.url} target="_blank" rel="noreferrer">{s.title ?? s.url}</a>{s.publisher ? ` — ${s.publisher}` : ""}</li>
              ))}
            </ul>
            <a className="correct" target="_blank" rel="noreferrer"
               href={`${REPO}/issues/new?template=correction.yml&title=${encodeURIComponent(`Correction: ${site.nameEn}`)}&labels=correction`}>
              Suggest a correction or source
            </a>
          </article>
        ) : (
          <>
            <div className="filters" role="group" aria-label="Filter by type">
              {presentTypes.map((t) => (
                <button key={t} aria-pressed={types.has(t)}
                  onClick={() => setTypes((prev) => { const n = new Set(prev); if (n.has(t)) n.delete(t); else n.add(t); return n; })}>
                  <span className="dot" style={{ background: TYPE_META[t].color }} />{TYPE_META[t].label}
                </button>
              ))}
            </div>
            {nearestKm != null && nearestKm > 50 && (
              <p className="far">The nearest recorded site is {fmtKm(nearestKm)} away. All sites stay on the map wherever you are.</p>
            )}
            <ul className="list">
              {visible.map((s) => (
                <li key={s.slug}>
                  <button onClick={() => setSelected(s.slug)}>
                    <span className="dot" style={{ background: TYPE_META[s.type].color }} />
                    <span className="name">{s.nameEn}{s.nameZh ? <span className="zh-inline"> {s.nameZh}</span> : null}</span>
                    <span className="meta">{me ? fmtKm(km(me, [s.lat, s.lng])) : s.earliestYear ?? ""}</span>
                  </button>
                </li>
              ))}
            </ul>
            <p className="foot">Historical records only, up to 1952. <a href={REPO} target="_blank" rel="noreferrer">How this is built</a></p>
          </>
        )}
      </section>
    </main>
  );
}
