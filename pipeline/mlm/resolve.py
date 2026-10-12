"""Entity resolution: merge mentions of the same site or person across documents.

Historical romanizations are normalized before fuzzy matching, so "Hangchow Presbyterian College"
and "Hangzhou Presbyterian College" resolve together.
"""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

# Postal / Wade-Giles / missionary spellings -> pinyin. Extend per region.
ROMANIZATION = {
    "hangchow": "hangzhou", "hangchau": "hangzhou", "hang-chow": "hangzhou",
    "ningpo": "ningbo", "chekiang": "zhejiang", "kwang-chi": "guangji", "kwangchi": "guangji",
    "peking": "beijing", "peiping": "beijing", "canton": "guangzhou", "amoy": "xiamen",
    "foochow": "fuzhou", "soochow": "suzhou", "nanking": "nanjing", "tientsin": "tianjin",
    "chefoo": "yantai", "hankow": "hankou", "tsingtao": "qingdao", "shantung": "shandong",
    "szechwan": "sichuan", "kiangsu": "jiangsu", "chinkiang": "zhenjiang", "yangchow": "yangzhou",
}
_STOP = {"the", "of", "a", "an", "church", "hospital", "school", "college", "mission"}


def normalize(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[’'`]", "", s)
    for old, new in ROMANIZATION.items():
        s = re.sub(rf"\b{re.escape(old)}\b", new, s)
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


def slugify(name: str) -> str:
    return re.sub(r"\s+", "-", normalize(name))[:80].strip("-")


def similarity(a: str, b: str) -> float:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    ta = " ".join(w for w in na.split() if w not in _STOP) or na
    tb = " ".join(w for w in nb.split() if w not in _STOP) or nb
    return max(fuzz.token_set_ratio(na, nb), fuzz.token_sort_ratio(ta, tb)) / 100


def same_entity(names_a: list[str], names_b: list[str], zh_a: str | None = None, zh_b: str | None = None,
                threshold: float = 0.88) -> bool:
    if zh_a and zh_b:
        return zh_a == zh_b
    return any(similarity(x, y) >= threshold for x in names_a for y in names_b)


def cluster(items: list[dict], names_of, zh_of, threshold: float = 0.88) -> list[list[dict]]:
    """Greedy single-link clustering; good enough at city scale, swap for blocking + ANN at country scale."""
    clusters: list[list[dict]] = []
    for item in items:
        for c in clusters:
            rep = c[0]
            if same_entity(names_of(item), names_of(rep), zh_of(item), zh_of(rep), threshold):
                c.append(item)
                break
        else:
            clusters.append([item])
    return clusters
