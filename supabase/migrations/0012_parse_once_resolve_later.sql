-- Finding, parsing and entity resolution are separate stages that meet only in these two tables.
--   resource_pool  what was found (any unit's search), and which parser version has read it.
--   extractions    what the parser found on a page: EVERY place on it, whatever unit it belongs to.
-- A page is parsed once per parser version. Its places are resolved (matched to known sites, filed under the unit of their
-- country/region, pinned) whenever a unit for them exists; places with no unit yet wait in `unplaced`, never re-parsed.
alter table resource_pool add column if not exists parsed_version integer;     -- null = never parsed
-- Pages read before this migration were parsed with a prompt scoped to the unit being worked on (version 1).
update resource_pool set parsed_version = 1 where status in ('extracted', 'loaded') and parsed_version is null;

create table if not exists extractions (
  hash           text primary key references resource_pool(hash) on delete cascade,
  version        integer not null,                       -- parser version that produced `data`
  data           jsonb not null,                         -- the Extraction (sites, persons, events, evidence)
  extracted_at   timestamptz not null default now(),
  resolved_at    timestamptz,                            -- null = not yet through entity resolution
  unplaced       text[] not null default '{}',           -- "country|region" keys of places with no unit yet
  unplaced_count integer not null default 0
);
create index if not exists extractions_pending_idx on extractions (hash) where resolved_at is null;
create index if not exists extractions_unplaced_idx on extractions (hash) where unplaced_count > 0;
alter table extractions enable row level security;   -- service role only
