-- When a cached model result was last used, so `mlm cache prune` removes only results nobody has needed for months
-- (keeps the table well inside the free tier as coverage grows, without re-paying for results still in use).
alter table public.llm_cache add column if not exists last_used timestamptz not null default now();
create index if not exists llm_cache_last_used_idx on public.llm_cache (last_used);
