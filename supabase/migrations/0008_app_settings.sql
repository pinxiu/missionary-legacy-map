-- Server-side settings shared between the pipeline and the web app (e.g. the map refresh token).
-- RLS on with no policies: only the secret key can read or write.
create table if not exists app_settings (key text primary key, value text not null, updated_at timestamptz not null default now());
alter table app_settings enable row level security;

-- The web app (publishable key only) checks a refresh request's token without being able to read it.
create or replace function check_revalidate_token(t text) returns boolean
language sql stable security definer set search_path = public as $$
  select coalesce((select value = t from app_settings where key = 'revalidate_token'), false)
$$;
revoke all on function check_revalidate_token(text) from public;
grant execute on function check_revalidate_token(text) to anon, authenticated;
-- Values (revalidate_token, site_url) are inserted once by hand and are not in the repo.
