-- Bilingual (English + Simplified Chinese) fields and Zhejiang city regions.
-- name_zh_status: 'cited' = Chinese name appears in a cited source; 'translated' = our translation, shown with a 译 mark.

create type zh_status as enum ('cited', 'translated');

alter table sites
  add column name_zh_status   zh_status,
  add column summary_zh       text,
  add column current_status_zh text;

alter table persons
  add column name_zh_status    zh_status,
  add column mission_society_zh text,
  add column bio_zh            text;

alter table events
  add column description_zh text;

alter table sources
  add column title_zh     text,
  add column publisher_zh text;

update sites   set name_zh_status = 'cited' where name_zh is not null;
update persons set name_zh_status = 'cited' where name_zh is not null;

insert into regions (slug, name_en, name_zh, parent) values
  ('ningbo',   'Ningbo',   '宁波', 'zhejiang'),
  ('wenzhou',  'Wenzhou',  '温州', 'zhejiang'),
  ('shaoxing', 'Shaoxing', '绍兴', 'zhejiang'),
  ('huzhou',   'Huzhou',   '湖州', 'zhejiang'),
  ('jiaxing',  'Jiaxing',  '嘉兴', 'zhejiang'),
  ('taizhou',  'Taizhou',  '台州', 'zhejiang'),
  ('jinhua',   'Jinhua',   '金华', 'zhejiang'),
  ('lishui',   'Lishui',   '丽水', 'zhejiang'),
  ('quzhou',   'Quzhou',   '衢州', 'zhejiang'),
  ('zhoushan', 'Zhoushan', '舟山', 'zhejiang')
on conflict (slug) do nothing;

create or replace view public_sites with (security_invoker = true) as
select
  s.id, s.slug, s.region, s.name_en, s.name_zh, s.alt_names, s.site_type,
  extensions.st_y(s.location::extensions.geometry) as lat,
  extensions.st_x(s.location::extensions.geometry) as lng,
  s.geo_precision, s.modern_address, s.current_status, s.summary, s.status,
  (select min(e.year_start) from events e where e.site_id = s.id) as earliest_year,
  (select count(distinct c.source_id) from claims c where c.subject_kind = 'site' and c.subject_id = s.id) as source_count,
  s.name_zh_status, s.summary_zh, s.current_status_zh,
  r.name_en as region_en, r.name_zh as region_zh
from sites s
left join regions r on r.slug = s.region;
