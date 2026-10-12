-- Missionary Legacy Map: core schema
-- Every published fact is traceable to a source via `claims`.
-- Publication policy: only historical records (see `publishable` checks and pipeline/mlm/policy.py).

create extension if not exists postgis with schema extensions;

create type review_status as enum ('pending', 'approved', 'rejected', 'conflict');
create type site_type as enum ('church', 'hospital', 'school', 'residence', 'cemetery', 'mission_station', 'press', 'orphanage', 'other');
create type geo_precision as enum ('exact', 'street', 'district', 'city', 'province');
create type subject_kind as enum ('site', 'person', 'event');

create table sources (
  id          uuid primary key default gen_random_uuid(),
  url         text unique not null,
  title       text,
  publisher   text,
  kind        text not null default 'web',      -- web | archive | book | journal | gazetteer
  language    text,                             -- en, zh, ...
  license     text,
  added_at    timestamptz not null default now()
);

create table documents (
  id           uuid primary key default gen_random_uuid(),
  source_id    uuid references sources(id) on delete cascade,
  url          text unique not null,
  title        text,
  lang         text,
  content_hash text not null,
  fetched_at   timestamptz not null default now(),
  text         text
);

create table regions (
  slug     text primary key,                    -- 'hangzhou', 'zhejiang', 'china', ...
  name_en  text not null,
  name_zh  text,
  parent   text references regions(slug),
  bbox     extensions.geometry(Polygon, 4326)
);

create table persons (
  id              uuid primary key default gen_random_uuid(),
  slug            text unique not null,
  name_en         text not null,
  name_zh         text,
  alt_names       text[] not null default '{}',
  birth_year      int,
  death_year      int,
  mission_society text,
  bio             text,
  status          review_status not null default 'pending',
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

create table sites (
  id              uuid primary key default gen_random_uuid(),
  slug            text unique not null,
  region          text references regions(slug),
  name_en         text not null,
  name_zh         text,
  alt_names       text[] not null default '{}',   -- historical spellings: Hangchow, Kwang-Chi, ...
  site_type       site_type not null default 'other',
  location        extensions.geography(Point, 4326),
  geo_precision   geo_precision not null default 'city',
  modern_address  text,
  current_status  text,                           -- 'active church', 'now Second Affiliated Hospital', 'demolished'
  summary         text,
  status          review_status not null default 'pending',
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);
create index sites_location_idx on sites using gist (location);
create index sites_region_idx on sites (region);

create table events (
  id           uuid primary key default gen_random_uuid(),
  site_id      uuid references sites(id) on delete cascade,
  person_id    uuid references persons(id) on delete set null,
  event_type   text not null,                     -- founded | built | rebuilt | moved | served | closed | buried | baptism | renamed
  year_start   int,
  year_end     int,
  description  text,
  status       review_status not null default 'pending',
  created_at   timestamptz not null default now(),
  -- Historical-only policy: nothing after the 1952 reorganisation is published as a legacy event.
  constraint events_historical check (year_start is null or year_start <= 1952)
);
create index events_site_idx on events (site_id);
create index events_person_idx on events (person_id);

-- Atomic, cited facts. Sites/persons/events are the resolved view of these.
create table claims (
  id            uuid primary key default gen_random_uuid(),
  subject_kind  subject_kind not null,
  subject_id    uuid not null,
  field         text not null,                    -- 'founded_year', 'name_zh', 'location', ...
  value         jsonb not null,
  source_id     uuid references sources(id) on delete cascade,
  document_id   uuid references documents(id) on delete set null,
  excerpt       text check (char_length(excerpt) <= 300),
  confidence    numeric(3,2) check (confidence between 0 and 1),
  status        review_status not null default 'pending',
  extracted_by  text,                             -- 'seed', 'claude-sonnet-5-5', 'human:pk'
  created_at    timestamptz not null default now()
);
create index claims_subject_idx on claims (subject_kind, subject_id);
create index claims_source_idx on claims (source_id);
create index claims_document_idx on claims (document_id);
create index documents_source_idx on documents (source_id);
create index regions_parent_idx on regions (parent);

-- Row level security: public can read approved records and sources only.
alter table sources   enable row level security;
alter table documents enable row level security;
alter table regions   enable row level security;
alter table persons   enable row level security;
alter table sites     enable row level security;
alter table events    enable row level security;
alter table claims    enable row level security;

create policy "public read sources"  on sources  for select to anon, authenticated using (true);
create policy "public read regions"  on regions  for select to anon, authenticated using (true);
create policy "public read persons"  on persons  for select to anon, authenticated using (status = 'approved');
create policy "public read sites"    on sites    for select to anon, authenticated using (status in ('approved', 'conflict'));
create policy "public read events"   on events   for select to anon, authenticated using (status in ('approved', 'conflict'));
create policy "public read claims"   on claims   for select to anon, authenticated using (status in ('approved', 'conflict'));
-- documents: service role only (raw crawled text is not republished).

-- GeoJSON feed for the map, filtered by the caller's RLS.
create or replace view public_sites with (security_invoker = true) as
select
  s.id, s.slug, s.region, s.name_en, s.name_zh, s.alt_names, s.site_type,
  extensions.st_y(s.location::extensions.geometry) as lat,
  extensions.st_x(s.location::extensions.geometry) as lng,
  s.geo_precision, s.modern_address, s.current_status, s.summary, s.status,
  coalesce((select min(e.year_start) from events e where e.site_id = s.id), null) as earliest_year,
  (select count(distinct c.source_id) from claims c where c.subject_kind = 'site' and c.subject_id = s.id) as source_count
from sites s;

grant select on public_sites to anon, authenticated;

-- Nearest legacy sites to a point (the "near me" list). Distances in metres.
create or replace function nearest_sites(lat double precision, lng double precision, max_results int default 20)
returns table (slug text, name_en text, name_zh text, site_type site_type, distance_m double precision)
language sql stable security invoker set search_path = public, extensions as $$
  select s.slug, s.name_en, s.name_zh, s.site_type,
         extensions.st_distance(s.location, extensions.st_makepoint(lng, lat)::extensions.geography) as distance_m
  from sites s
  where s.location is not null
  order by s.location operator(extensions.<->) extensions.st_makepoint(lng, lat)::extensions.geography
  limit least(max_results, 100);
$$;

grant execute on function nearest_sites to anon, authenticated;

insert into regions (slug, name_en, name_zh, parent) values
  ('world',    'World',    '世界', null),
  ('china',    'China',    '中国', 'world'),
  ('zhejiang', 'Zhejiang', '浙江', 'china'),
  ('hangzhou', 'Hangzhou', '杭州', 'zhejiang');
