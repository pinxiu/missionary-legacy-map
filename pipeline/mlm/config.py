from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("MLM_DATA_DIR", REPO_ROOT / "data"))
WORK_DIR = Path(os.environ.get("MLM_WORK_DIR", REPO_ROOT / ".mlm-work"))  # raw crawl cache, gitignored

USER_AGENT = os.environ.get(
    "MLM_USER_AGENT",
    "MissionaryLegacyMapBot/0.1 (+https://github.com/pinxiu/missionary-legacy-map; historical research)",
)
CRAWL_DELAY_S = float(os.environ.get("MLM_CRAWL_DELAY_S", "2.0"))

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
# Extraction runs on the cheap model first; a page whose result fails local checks (excerpts not found in the page,
# output cut off, nothing found on a clearly relevant page) is redone with the stronger model. Precision is guarded
# downstream anyway: the trusted verifier and the independent audit decide what is kept.
EXTRACT_MODEL = os.environ.get("MLM_EXTRACT_MODEL", "claude-haiku-5-5")
EXTRACT_ESCALATE_MODEL = os.environ.get("MLM_EXTRACT_ESCALATE_MODEL", "claude-sonnet-5-5")
EXTRACT_MIN_EXCERPT_RATE = float(os.environ.get("MLM_EXTRACT_MIN_EXCERPT_RATE", "0.9"))
# Translation: Gemini's free tier when GEMINI_API_KEY is set (it is strong at Chinese), Claude Haiku otherwise or when
# Gemini is out of quota. Set MLM_TRANSLATE_MODEL to a Claude model to skip Gemini entirely.
TRANSLATE_MODEL = os.environ.get("MLM_TRANSLATE_MODEL", "gemini-3.8-flash")
TRANSLATE_FALLBACK_MODEL = os.environ.get("MLM_TRANSLATE_FALLBACK_MODEL", "claude-haiku-5-5")
VERIFY_MODEL = os.environ.get("MLM_VERIFY_MODEL", "claude-sonnet-5-5")       # support check in the trusted verifier
AUDIT_MODEL = os.environ.get("MLM_AUDIT_MODEL", "claude-opus-5-5")           # independent auditor: a different, stronger model



def cached_system(text: str) -> list[dict]:
    """System prompt marked for prompt caching. Within a run the same instructions (and, for extraction, the tool
    schema before them) go out with every call, so later calls read them from cache at a small fraction of the
    input price instead of paying full price each time. Prompts below the model's cache minimum are sent uncached."""
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


SUPABASE_URL = os.environ.get("SUPABASE_URL")
# New-style secret key (sb_secret_...). The legacy service_role JWT is still accepted as a fallback.
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

# Publication policy (see policy.py). Nothing describing activity after this year is published.
HISTORICAL_CUTOFF_YEAR = int(os.environ.get("MLM_CUTOFF_YEAR", "1952"))

# Review agent thresholds.
AUTO_APPROVE_MIN_SOURCES = 2
AUTO_APPROVE_MIN_CONFIDENCE = 0.8


def notice(msg: str, level: str = "notice") -> None:
    """Print a message; on GitHub Actions also surface it as a run annotation."""
    import os as _os
    print(f"::{level}::{msg}" if _os.environ.get("GITHUB_ACTIONS") else msg)
