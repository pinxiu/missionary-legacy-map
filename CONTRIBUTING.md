# Contributing 参与贡献

Thank you for helping map the history of Christianity. A few rules keep the map trustworthy.

1. **Cite everything.** No fact without a source. A suggestion without a source can't be published.
2. **Historical only.** Nothing after 1952; no living people; no present-day religious workers or activity.
3. **Be honest about uncertainty.** Say how precise a location is; keep disagreements between sources visible.
4. **Chinese names.** Only mark a Chinese name as cited if a source writes it.

## Ways to help
- **On the map:** use the in-app cards to correct a site, add a site (with a location pin), add a photo, or leave feedback.
  Each suggestion becomes a GitHub issue; the owner or a listed approver reviews it before anything goes live.
- **Issues:** report data errors or bugs. Please don't put personal information in them.
- **Code:** open a pull request. `cd pipeline && pip install -e ".[dev]" && pytest -q && ruff check .` and
  `cd apps/web && npm ci && npm run build` must pass. Read `CLAUDE.md` first, especially "Rules that never bend".
- **Sources:** suggest reputable books, archives and gazetteers (Chinese-language ones especially).

## Licence of your contribution
By contributing you agree that code is released under the MIT licence (`LICENSE`) and data, text and translations under
CC BY-SA 4.0 (`LICENSE-DATA.md`). Only submit what you have the right to share.
