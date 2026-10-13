-- Owner decisions on audit disagreements (resolved through GitHub `needs-decision` issues).
alter table audits add column if not exists decision_issue int, add column if not exists resolution text, add column if not exists resolved_at timestamptz;
