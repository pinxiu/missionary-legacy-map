# Legacy Map 宣教遗迹地图

A mobile-first, bilingual map of the history of Christianity: missions of every tradition, indigenous and independent
churches, revivals, schools, hospitals, presses, cemeteries and memorials. It shows every site wherever you are, and
**every fact links to its source**. Live: https://missionary-legacy-map-taupe.vercel.app

Now: **Zhejiang 浙江**. Next: all of China, then the world. Progress: [STATUS.md](STATUS.md).

## How data gets in (and why you can trust it)

```
 coverage unit (catalog) ─▶ source scout ─▶ crawl ─▶ extract (Claude) ─▶ translate (Haiku) ─▶ load [pending]
                                                                                       │
                       trusted verifier: excerpt really in the source + supports the claim ─▶ [verified]
                       independent audit (different model) on a random sample ─▶ gates in STATUS.md
                                                                                       │
                                gates met (audit agreement ≥ 80%) ─▶ published [approved]
                                (the owner's `approved` label also publishes a unit; disputed records are held back)

 visitor card ─▶ submissions ─▶ GitHub issue ─▶ AI first pass (polish, translate, verify, cite, proposal)
                                                     ─▶ owner adds `approved` ─▶ apply worker ─▶ published
```

| Workflow | When | Model? |
|---|---|---|
| `coverage-agents.yml` | every 6 h, one unit at a time (searches each city, pools pages by hash, extracts every place once) | Sonnet (scout, extract, verify), Haiku (translate), Opus (audit) |
| `coverage-sync.yml` | on catalog change + daily | no |
| `submissions-sync.yml` | every 15 min | no |
| `feedback-agent.yml` | every 3 h, only if issues need triage | Sonnet |
| `apply-approved.yml` | when the owner or a listed approver adds `approved` | no |
| `seed-sync.yml` | when `data/seed` changes | no |
| `ci.yml` | every push / PR | no |

## Publication policy
Historical records only (to 1952), no living people, no present-day religious activity. Conflicting sources are shown
side by side. Approximate locations are labeled. Chinese names we translated are marked 译. See `CLAUDE.md`.

## Setup (done)
Actions secrets `ANTHROPIC_API_KEY`, `SUPABASE_SECRET_KEY`. Vercel env `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY`.

## Local development
```bash
cd pipeline && pip install -e ".[dev]" && pytest -q
cd apps/web && npm ci && npm run dev
```

## Licence
Code: [MIT](LICENSE). Data (records, translations, source links): [CC BY-SA 4.0](LICENSE-DATA.md); photos and third-party
sources keep their own terms. See [CONTRIBUTING.md](CONTRIBUTING.md) to help.
