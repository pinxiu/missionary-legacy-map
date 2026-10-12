# Missionary Legacy Map

A map of historical Christian mission sites, built from crawled literature. Every fact is cited.
The pilot region is Hangzhou. Expansion order: Hangzhou → Zhejiang → China → world.

## Layout
- `apps/web` — Next.js map app (MapLibre + OpenFreeMap tiles). Reads Supabase with the publishable key and falls back to `data/seed/*.json`.
- `pipeline/mlm` — Python pipeline: `crawl` → `extract` (Claude tool-use into `models.Extraction`) → `load` (pending rows + claims) → `review` (auto-approve on 2+ sources).
- `supabase/migrations` — schema. `claims` is the source of truth; `sites`/`persons`/`events` are the resolved view.
- `data/seed` — hand-curated, cited records. `data/gazetteer` — historical place names → coordinates.

## Rules that never bend
1. **Cite everything.** No fact without a source URL. Never fill gaps from model knowledge.
2. **Historical only.** Nothing after 1952. No living people. No present-day religious workers or activity;
   a neutral current-use note ("now a hospital") is the most that is allowed. `pipeline/mlm/policy.py` enforces this.
3. **Keep disagreements visible.** When sources conflict, store both and mark the site `conflict`.
4. **Be honest about location.** Use `geo_precision`; never pretend a city-centre pin is exact.
5. **Crawl politely.** Respect robots.txt, keep `max_pages` modest, and store only short excerpts publicly.

## Commands
- `cd pipeline && pip install -e ".[dev]" && pytest -q && ruff check .`
- `mlm run --region hangzhou --stage crawl|extract|load|review|all`
- `mlm seed --region hangzhou --sql` prints idempotent SQL for the seed.
- `cd apps/web && npm ci && npm run build`

## Adding a region
Add it to `regions` (migration), `pipeline/mlm/sources.yaml`, `data/gazetteer/<region>.yaml`, and `REGION_CENTROIDS`.
