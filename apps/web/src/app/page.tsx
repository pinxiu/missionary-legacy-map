import MapApp from "@/components/MapApp";
import { AREAS, getSites, REGIONS } from "@/lib/data";

export const revalidate = 600;

export default async function Page() {
  const { sites, live } = await getSites();
  return <MapApp sites={sites} regions={REGIONS} areas={AREAS} live={live} />;
}
