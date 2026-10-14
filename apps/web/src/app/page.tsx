import MapApp from "@/components/MapApp";
import { getRegions, getSites } from "@/lib/data";

// Refreshed right after each publish (/api/revalidate); this daily refresh is only a backup.
export const revalidate = 86400;

export default async function Page() {
  const [{ sites, live }, { regions, areas }] = await Promise.all([getSites(), getRegions()]);
  return <MapApp sites={sites} regions={regions} areas={areas} live={live} />;
}
