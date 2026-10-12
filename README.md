# Legacy Map

A mobile-first map of historical Christian mission sites: churches, hospitals, schools, cemeteries and mission stations. It shows every site wherever you are, and **every fact links to its source**.

Pilot: **Hangzhou**. Next: Zhejiang → China → worldwide.

## How it works

```
sources.yaml ─▶ crawl ─▶ extract (Claude, tool-use) ─▶ policy filter ─▶ load (pending + claims)
                                                                            │
                         public map ◀── approved/conflict rows ◀── review agent (2+ sources)
                                                                            │
                                                     "Review queue" issue ◀─┘  (for humans)
```

| Piece | Where | Runs |
|---|---|---|
| Map app (PWA, installable on phones) | `apps/web` | Vercel project `missionary-legacy-map`, deploys on every push to `main` |
| Database (Postgres + PostGIS, RLS) | `supabase/` | Supabase project `missionary-legacy-map` |
| Pipeline | `pipeline/mlm` | GitHub Actions, Mondays 09:17 UTC |
| Source-scout agent | `.github/workflows/source-scout.yml` | Wednesdays, opens PRs |
| Correction triage / `@claude` | `.github/workflows/claude.yml` | On issues and comments |

## Publication policy
Historical records only (to 1952), no living people, and no present-day religious activity. Conflicting sources are shown side by side, and approximate locations are labeled. See `CLAUDE.md` and `pipeline/mlm/policy.py`.

## Setup secrets (one time)
GitHub → Settings → Secrets and variables → Actions:
- Secret `ANTHROPIC_API_KEY` — extraction and agents
- Secret `SUPABASE_SERVICE_ROLE_KEY` — Supabase → Project Settings → API keys → `service_role`
(`SUPABASE_URL` is set in the workflow; it is not secret.)

## Local development
```bash
cd pipeline && pip install -e ".[dev]" && pytest -q
cd apps/web && npm ci && npm run dev
```
