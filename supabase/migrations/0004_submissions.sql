-- Visitor contributions from in-app cards. Anonymous visitors can insert; nobody but the
-- service (secret key) can read. A worker turns each row into a GitHub issue for AI first pass
-- and owner approval. Photos are no longer approved automatically: they follow the same flow.

create type submission_kind as enum ('new_site', 'correction', 'new_detail', 'photo', 'feedback');

create table submissions (
  id            uuid primary key default gen_random_uuid(),
  kind          submission_kind not null,
  site_id       uuid references sites(id) on delete set null,
  photo_id      uuid references photos(id) on delete set null,
  lang          text check (lang in ('en', 'zh')),
  payload       jsonb not null default '{}'::jsonb,   -- free text, proposed name, sources, lat/lng, ...
  contact       text check (char_length(contact) <= 200),
  issue_number  int,
  created_at    timestamptz not null default now(),
  constraint payload_size check (pg_column_size(payload) <= 20000)
);
create index submissions_unsynced_idx on submissions (created_at) where issue_number is null;
create index submissions_site_idx on submissions (site_id);
create index submissions_photo_idx on submissions (photo_id);

alter table submissions enable row level security;

create policy "visitors submit" on submissions
  for insert to anon, authenticated
  with check (issue_number is null);

-- Visitors pre-generate the photo id so the photo and its submission can be linked without read access.
-- (photos insert policy from 0003 already allows a client-supplied id.)
