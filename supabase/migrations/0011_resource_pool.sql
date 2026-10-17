-- The resource pool: every page the crawler has ever fetched, keyed by a hash of its normalised text.
-- A page whose hash is already `extracted` or `loaded` is never sent to a model again, whichever province or city
-- search found it. Extraction records every place on a page, so one pass serves all units.
create table if not exists resource_pool (
  hash          text primary key,                 -- sha256 of the normalised text (see mlm/pool.py)
  url           text not null,
  title         text,
  kind          text,
  lang          text,
  source_name   text,
  found_for     text[] not null default '{}',     -- units / cities whose search found it
  text          text,
  chars         integer,
  status        text not null default 'new' check (status in ('new', 'extracted', 'loaded', 'failed')),
  locales       text[] not null default '{}',     -- provinces / cities the extraction found sites in
  first_seen    timestamptz not null default now(),
  extracted_at  timestamptz
);
create index if not exists resource_pool_url_idx on resource_pool (url);
create index if not exists resource_pool_status_idx on resource_pool (status);
-- Service-role only: no public policy, so the publishable key can't read it.
alter table resource_pool enable row level security;
