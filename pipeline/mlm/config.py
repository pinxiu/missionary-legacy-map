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
EXTRACT_MODEL = os.environ.get("MLM_EXTRACT_MODEL", "claude-sonnet-5-5")
TRANSLATE_MODEL = os.environ.get("MLM_TRANSLATE_MODEL", "claude-haiku-5-5")  # cheap model for zh fill-in
VERIFY_MODEL = os.environ.get("MLM_VERIFY_MODEL", "claude-sonnet-5-5")       # support check in the trusted verifier
AUDIT_MODEL = os.environ.get("MLM_AUDIT_MODEL", "claude-opus-5-5")           # independent auditor: a different, stronger model

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
