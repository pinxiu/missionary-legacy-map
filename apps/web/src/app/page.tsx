import MapApp from "@/components/MapApp";
import { getSites } from "@/lib/data";

export const revalidate = 3600;

export default async function Page() {
  const { sites, live } = await getSites();
  return <MapApp sites={sites} live={live} />;
}
