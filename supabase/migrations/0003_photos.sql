-- Photos: openly licensed historical images (added by the pipeline/editors) and visitor uploads.
-- Visitor uploads land in the private `photo-uploads` bucket with status 'pending'.
-- The moderation job copies approved files to the public `site-photos` bucket and flips status.

create type photo_kind as enum ('historical', 'visitor');

create table photos (
  id               uuid primary key default gen_random_uuid(),
  site_id          uuid not null references sites(id) on delete cascade,
  kind             photo_kind not null,
  upload_path      text,                 -- object in photo-uploads (visitor uploads)
  public_path      text,                 -- object in site-photos once approved
  image_url        text,                 -- external openly licensed image (historical)
  caption          text check (char_length(caption) <= 500),
  caption_zh       text check (char_length(caption_zh) <= 500),
  credit           text check (char_length(credit) <= 200),
  license          text check (char_length(license) <= 100),
  source_url       text,
  year_taken       int check (year_taken between 1800 and 2100),
  uploader_name    text check (char_length(uploader_name) <= 80),
  status           review_status not null default 'pending',
  moderation_note  text,
  created_at       timestamptz not null default now()
);
create index photos_site_idx on photos (site_id);
create index photos_status_idx on photos (status);

alter table photos enable row level security;

create policy "public read approved photos" on photos
  for select to anon, authenticated using (status = 'approved');

-- Visitors may only add a pending upload for a published site, under pending/, with a CC BY licence.
create policy "visitors add pending uploads" on photos
  for insert to anon, authenticated
  with check (
    kind = 'visitor' and status = 'pending'
    and upload_path like 'pending/%' and public_path is null and image_url is null
    and moderation_note is null and license = 'CC BY 4.0'
    and exists (select 1 from sites s where s.id = site_id)
  );

-- Buckets: private intake, public gallery.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types) values
  ('photo-uploads', 'photo-uploads', false, 5242880, array['image/jpeg', 'image/png', 'image/webp']),
  ('site-photos',   'site-photos',   true,  5242880, array['image/jpeg', 'image/png', 'image/webp'])
on conflict (id) do nothing;

create policy "visitors upload to pending" on storage.objects
  for insert to anon, authenticated
  with check (bucket_id = 'photo-uploads' and (storage.foldername(name))[1] = 'pending');

-- site-photos is a public bucket: files are served by URL without a select policy, so it cannot be listed.
