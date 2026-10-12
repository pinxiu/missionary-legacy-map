"use client";

import { Map as MLMap, Marker, setWorkerUrl } from "maplibre-gl";
import { useEffect, useRef, useState } from "react";
import type { Lang } from "@/lib/i18n";

const STYLE_URL = "https://tiles.openfreemap.org/styles/positron";

type Hit = { display_name: string; lat: string; lon: string };
export type Picked = { lat: number; lng: number; how: "gps" | "search" | "pin"; label?: string };

const TXT = {
  hint: ["Drag the pin or tap the map to mark the exact spot.", "拖动图钉或点击地图，标出确切位置。"],
  mine: ["Use my location", "使用我的位置"], locating: ["Locating…", "定位中…"],
  search: ["Search a place or address", "搜索地点或地址"], go: ["Search", "搜索"], none: ["No results", "没有结果"],
} as const;

export default function LocationPicker({ lang, center, onChange }: { lang: Lang; center: [number, number]; onChange: (p: Picked) => void }) {
  const L = (k: keyof typeof TXT) => TXT[k][lang === "zh" ? 1 : 0];
  const el = useRef<HTMLDivElement>(null);
  const map = useRef<MLMap | null>(null);
  const marker = useRef<Marker | null>(null);
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [locating, setLocating] = useState(false);

  function place(lng: number, lat: number, how: Picked["how"], label?: string, fly = true) {
    marker.current?.setLngLat([lng, lat]);
    if (fly) map.current?.flyTo({ center: [lng, lat], zoom: Math.max(map.current.getZoom(), 15) });
    onChange({ lat: +lat.toFixed(6), lng: +lng.toFixed(6), how, label });
  }

  useEffect(() => {
    if (!el.current || map.current) return;
    setWorkerUrl("/maplibre-gl-worker.mjs");
    const m = new MLMap({ container: el.current, style: STYLE_URL, center, zoom: 12, attributionControl: { compact: true } });
    const mk = new Marker({ draggable: true, color: "#8a3b2a" }).setLngLat(center).addTo(m);
    mk.on("dragend", () => { const p = mk.getLngLat(); place(p.lng, p.lat, "pin", undefined, false); });
    m.on("click", (e) => place(e.lngLat.lng, e.lngLat.lat, "pin", undefined, false));
    map.current = m; marker.current = mk;
    onChange({ lat: center[1], lng: center[0], how: "pin" });
    return () => { m.remove(); map.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function useMine() {
    if (!navigator.geolocation) return;
    setLocating(true);
    navigator.geolocation.getCurrentPosition(
      (p) => { place(p.coords.longitude, p.coords.latitude, "gps"); setLocating(false); },
      () => setLocating(false),
      { enableHighAccuracy: true, timeout: 15000 },
    );
  }

  async function search() {
    if (!q.trim()) return;
    // OpenStreetMap Nominatim: free, light use only (one request per explicit search).
    const r = await fetch(`https://nominatim.openstreetmap.org/search?format=jsonv2&limit=5&accept-language=${lang === "zh" ? "zh-CN" : "en"}&q=${encodeURIComponent(q)}`);
    setHits(r.ok ? await r.json() : []);
  }

  return (
    <div className="picker">
      <div className="row">
        <input value={q} placeholder={L("search")} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && (e.preventDefault(), search())} />
        <button type="button" onClick={search}>{L("go")}</button>
        <button type="button" onClick={useMine} disabled={locating}>{locating ? L("locating") : `📍 ${L("mine")}`}</button>
      </div>
      {hits && (
        <ul className="hits">
          {hits.length === 0 && <li>{L("none")}</li>}
          {hits.map((h, i) => (
            <li key={i}><button type="button" onClick={() => { place(+h.lon, +h.lat, "search", h.display_name); setHits(null); }}>{h.display_name}</button></li>
          ))}
        </ul>
      )}
      <div ref={el} className="picker-map" />
      <p className="hint">{L("hint")}</p>
    </div>
  );
}
