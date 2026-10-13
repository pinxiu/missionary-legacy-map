-- Owner review of rejected records: the needs-decision issue opened when the owner asks to look at one again.
alter table sites  add column if not exists reconsider_issue int;
alter table events add column if not exists reconsider_issue int;
