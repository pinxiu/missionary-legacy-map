-- Durable cache of model results (pipeline/mlm/llm.py). Keys are hashes of the full request, so a re-run, a lost
-- Actions cache or an unchanged re-crawled page never pays twice. Rows starting with `batch:` mark Message Batches
-- still in flight. Server-side only: RLS on with no policies, so only the secret key can read or write it.
create table if not exists public.llm_cache (
  key text primary key,
  model text not null default '',
  purpose text not null default '',
  result jsonb not null,
  created_at timestamptz not null default now()
);

alter table public.llm_cache enable row level security;

create index if not exists llm_cache_purpose_idx on public.llm_cache (purpose, created_at);
