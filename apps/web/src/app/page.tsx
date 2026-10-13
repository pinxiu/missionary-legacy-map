import MapApp from "@/components/MapApp";
import { AREAS, getSites, REGIONS } from "@/lib/data";

// Refreshed right after each publish (/api/revalidate); this daily refresh is only a backup.
export const revalidate = 86400;

export default async function Page() {
  const { sites, live } = await getSites();
  return <MapApp sites={sites} regions={REGIONS} areas={AREAS} live={live} />;
}
