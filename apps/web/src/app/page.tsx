import MapApp from "@/components/MapApp";
import { getSites, REGIONS } from "@/lib/data";

export const revalidate = 600;

export default async function Page() {
  const { sites, live } = await getSites();
  return <MapApp sites={sites} regions={REGIONS} live={live} />;
}
