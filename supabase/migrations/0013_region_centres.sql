-- City/province centres for regions created at run time (cities that are not in the built-in gazetteer),
-- so every stage pins and searches around the same centre.
alter table regions add column if not exists lat double precision;
alter table regions add column if not exists lng double precision;
