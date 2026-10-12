-- Verification gate and audit log for coverage units.
-- Pipeline records move pending -> verified (trusted verifier) -> approved (owner approves the unit batch).

alter type review_status add value if not exists 'verified';

alter table sites   add column if not exists review_note text, add column if not exists unit text;
alter table events  add column if not exists review_note text;
alter table persons add column if not exists review_note text;
alter table claims  add column if not exists review_note text, add column if not exists verified_at timestamptz;

create index if not exists sites_unit_idx on sites (unit);

-- Independent audit results (service role only).
create table if not exists audits (
  id          uuid primary key default gen_random_uuid(),
  unit        text not null,
  subject_kind subject_kind not null,
  subject_id  uuid not null,
  agree       boolean not null,
  detail      jsonb,
  model       text,
  created_at  timestamptz not null default now()
);
create index if not exists audits_unit_idx on audits (unit);
alter table audits enable row level security;
