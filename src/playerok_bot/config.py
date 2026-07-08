"""Налаштування застосунку, що зчитуються зі змінних середовища."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BrowserConfig:
    headless: bool
    slow_mo_ms: int
    default_timeout_ms: int
    user_data_dir: Path


def load_browser_config() -> BrowserConfig:
    return BrowserConfig(
        headless=os.getenv("BROWSER_HEADLESS", "false").lower() == "true",
        slow_mo_ms=int(os.getenv("BROWSER_SLOW_MO_MS", "0")),
        default_timeout_ms=int(os.getenv("BROWSER_DEFAULT_TIMEOUT_MS", "30000")),
        user_data_dir=Path(os.getenv("BROWSER_USER_DATA_DIR", "./browser-profile")),
    )
