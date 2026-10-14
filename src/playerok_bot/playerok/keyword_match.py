"""Класифікація товару за назвою — словник King Legacy + blacklist.

Фото як критерій НЕ використовується (playerok/backlog_scan.py) — рішення
користувача 2026-07-20 після емпіричної перевірки: perceptual-hash не
розділяє надійно мініатюри цього акаунта (той самий арт із дівчиною майже на
всіх товарах, тож фото-подібність не несе розрізняльної інформації).

Словник — config/king_legacy_keywords.json (корінь репозиторію), редагується
користувачем напряму, без зміни коду. Порівняння регістронезалежне,
підрядкове (case-insensitive substring) — жодного fuzzy-матчингу: назва або
буквально містить слово зі словника, або товар пропускається ("якщо є
сумнів — пропустити", explicit-рішення користувача).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# src/playerok_bot/playerok/keyword_match.py -> repo root: parents[3]
_REPO_ROOT = Path(__file__).resolve().parents[3]
KEYWORDS_PATH = _REPO_ROOT / "config" / "king_legacy_keywords.json"


@dataclass(frozen=True)
class KeywordMatch:
    matched: bool
    matched_keyword: str | None
    reason: str


@lru_cache(maxsize=1)
def _load_dictionary() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """whitelist у файлі згруповано за категоріями (fruits/swords/...) —
    лише для зручності редагування; тут об'єднується в один плоский список."""
    if not KEYWORDS_PATH.is_file():
        raise FileNotFoundError(f"Словник ключових слів не знайдено: {KEYWORDS_PATH}")
    data = json.loads(KEYWORDS_PATH.read_text(encoding="utf-8"))
    whitelist = tuple(
        kw.lower() for category in data["whitelist"].values() for kw in category
    )
    blacklist = tuple(kw.lower() for kw in data["blacklist"])
    return whitelist, blacklist


def classify_name(product_name: str) -> KeywordMatch:
    """blacklist -> whitelist -> непевність. blacklist має пріоритет: назва
    з "Robux" у собі ніколи не пройде, навіть якщо десь випадково зустрінеться
    слово зі whitelist."""
    whitelist, blacklist = _load_dictionary()
    name_lower = product_name.lower()

    for keyword in blacklist:
        if keyword in name_lower:
            return KeywordMatch(
                matched=False,
                matched_keyword=keyword,
                reason=f"Назва містить слово з чорного списку: {keyword!r}",
            )

    for keyword in whitelist:
        if keyword in name_lower:
            return KeywordMatch(
                matched=True,
                matched_keyword=keyword,
                reason=f"Назва відповідає словнику King Legacy: {keyword!r}",
            )

    return KeywordMatch(
        matched=False,
        matched_keyword=None,
        reason="Назва не відповідає жодному слову зі словника King Legacy",
    )
