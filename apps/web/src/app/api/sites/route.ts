import { getSites } from "@/lib/data";

export const revalidate = 3600;

// Public GeoJSON feed. The future native app and any partner can read from here.
export async function GET() {
  const { sites, live } = await getSites();
  return Response.json({
    type: "FeatureCollection",
    meta: { live, count: sites.length },
    features: sites.map(({ lat, lng, ...props }) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [lng, lat] },
      properties: props,
    })),
  });
}
