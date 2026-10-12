// Copies the repo's editorial seed into the app so the map works before (or without) Supabase.
import { mkdirSync, readdirSync, copyFileSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = join(here, "../../../data/seed");
const dest = join(here, "../src/data");
mkdirSync(dest, { recursive: true });
if (existsSync(src)) {
  for (const f of readdirSync(src)) if (f.endsWith(".json")) copyFileSync(join(src, f), join(dest, f));
  console.log("seed copied");
} else {
  console.log("seed directory not found; using committed copy");
}
