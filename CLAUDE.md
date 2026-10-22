# Missionary Legacy Map 宣教遗迹地图

A bilingual (English / Simplified Chinese) map of the history of Christianity: missions of every tradition, indigenous
and independent movements, revivals, schools, hospitals, presses, cemeteries and memorials. Every fact is cited.
Expansion: Zhejiang (done first) → all of China → the world. Progress: STATUS.md and the `unit` issues.

## Layout
- `apps/web`: Next.js map app (MapLibre + OpenFreeMap). Reads Supabase with the publishable key; falls back to `data/seed`.
  Visitors contribute through in-app cards (edit, add site with location pin, photo, feedback) into `submissions`.
- `pipeline/mlm`: Python. `crawl → extract → translate → load → verify → audit` per coverage unit, plus `seed`,
  `submissions`, `apply`, `coverage` commands (see `mlm --help`).
- `coverage/catalog.yaml`: units of work and the themes each unit is researched for; gates for "done".
- `data/seed`: hand-curated, cited, bilingual records (`hangzhou.json`, `zhejiang.json`). Published by seed-sync.
- `docs/contributions.md`: the visitor contribution flow, labels, and the proposal JSON format.
- `supabase/migrations`: schema. `claims` hold the evidence; `sites`/`persons`/`events` are the resolved view.

## Rules that never bend
1. **Cite everything.** No fact without a source. Never fill gaps from model knowledge.
2. **Historical only.** Nothing after 1952. No living people. No present-day religious workers or activity;
   a neutral current-use note ("now a hospital") is the most that is allowed. `pipeline/mlm/policy.py` enforces this.
3. **Keep disagreements visible.** When sources conflict, store both and mark the site `conflict`.
4. **Be honest about location.** Use `geo_precision`; never pretend a city-centre pin is exact.
5. **Bilingual and honest about it.** Every name, summary and event has English and Simplified Chinese. A Chinese name
   is `cited` only if a source writes it; otherwise `translated` (shown with 译). Never invent a "historical" name.
6. **Approval before anything goes live.** Visitor changes publish only after the owner labels the issue `approved`.
   Agent-found records publish when the trusted verifier and the independent audit pass the unit's gates (audit agreement
   ≥ 80%, `done_when` in the catalog; the owner delegated unit approval to these gates), or when the owner labels the unit
   `approved`. A record the auditor disagreed with is held back until the owner rules on it.
7. **Agents never grade their own work.** Verification, audits, gates and publishing are trusted code, not agents.
8. **Crawl politely.** Respect robots.txt, keep `max_pages` modest, store only short excerpts publicly.
9. **Correctness over speed.** One coverage unit at a time; reject what can't be verified.
10. **Follow the owner's rulings.** `coverage/rulings.md` holds general decisions from `needs-decision` issues; they
    override your own judgement in every extraction, verification and audit.

## How a unit is researched (find → parse → resolve)
The map is for the whole world, so the three jobs are separate stages that meet only in the database (`pool.py`):
1. **Find** (`crawl.py`, `discover.py`): the only stage that knows the unit. Searches the province, then each city in it
   (Wikipedia in English and Chinese), plus the curated sources in `sources.yaml`, and adds every page to `resource_pool`
   under a hash of its normalised text. Nothing is read by a model here.
2. **Parse** (`extract.py`): reads every pool page that the current `PARSER_VERSION` hasn't read, whichever unit's search found
   it, and records EVERY place on it (`extractions`: country, first-level region, city, evidence). The prompt names no region,
   so the result is the same whoever asked and a page is paid for once for the whole world. Pages read by an older, narrower
   parser are read again; bump `PARSER_VERSION` when the prompt or schema changes. A backlog is worked off
   `MLM_PARSE_MAX_PAGES` (100) pages per run, the active unit's pages first.
3. **Resolve** (`load.py`): matches each parsed place to a known site or person (`resolve.py`), files it under the unit of its
   country/region, pins it (gazetteer, then `locate.py`: Amap in China (needs `AMAP_KEY`), OpenStreetMap and Wikidata
   everywhere else; `street` precision with a cited `location` claim, else the city/region centre). A place whose
   country/region has no unit yet waits in the extraction's `unplaced` list and is resolved, with no new parse, once a unit
   for it is added to the catalog.
4. Translate, verify, audit and publish per unit as below; the unit being worked on is still one at a time.

## Model costs
Every model call goes through `pipeline/mlm/llm.py`: results are cached (`.mlm-work/llm-cache` and the `llm_cache` table),
uncached Claude requests go out as half-price Message Batches, and each stage prints a cost line (`llm[stage]: … ~$`).
Extraction runs on Haiku and escalates a page to Sonnet only when local checks fail. Translation uses the seed glossary,
then Wikidata (people), then Haiku (Gemini's free tier only if `MLM_TRANSLATE_MODEL` selects it after a benchmark). The verifier (Sonnet) and the auditor (Opus)
keep their models: they are the correctness gates. Claude Code steps use `CLAUDE_CODE_OAUTH_TOKEN` (the owner's
subscription) when it is set. Never add a model call that bypasses `llm.py`.
The verifier's excerpt check is script-insensitive (Traditional = Simplified) and accepts an elided quote ("A … B") only if
every piece is verbatim, in order, within one passage; extraction snaps near-verbatim quotes to the page's own text. Cached
results unused for 180 days are pruned daily (`mlm cache prune`).

## Status lifecycle
`pending` (loaded) → `verified` (trusted verifier) → `approved`/`conflict` (owner approved unit or submission) — public.
`rejected` records stay private with a `review_note`.

## Commands
- `cd pipeline && pip install -e ".[dev]" && pytest -q && ruff check .`
- `mlm run --unit CN-ZJ [--stage find|parse|translate|resolve|verify|audit|all] [--max-pages N]` (old names crawl/extract/load work)
- `mlm coverage sync|status|next|publish --unit X|report --unit X`
- `mlm seed` (publish data/seed), `mlm submissions`, `mlm apply --issue N`
- `cd apps/web && npm ci && npm run build`

## Adding a unit
Add it to `coverage/catalog.yaml`. The coverage agents scout sources into `pipeline/mlm/sources.yaml` when it becomes active.
