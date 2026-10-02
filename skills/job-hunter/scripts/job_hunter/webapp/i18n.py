"""Tiny i18n: load JSON dict per locale, fall back to the key itself.

Locale lives in a `lang` cookie. Supported: en, pt_BR. No accept-language
sniffing — explicit toggle keeps the UX predictable.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Final

SUPPORTED: Final[tuple[str, ...]] = ("en", "pt_BR")
DEFAULT: Final[str] = "en"


@lru_cache(maxsize=4)
def load(locale: str) -> dict[str, str]:
    if locale not in SUPPORTED:
        locale = DEFAULT
    raw = (files("job_hunter.webapp.i18n_data") / f"{locale}.json").read_text(encoding="utf-8")
    return dict(json.loads(raw))


def t(locale: str, key: str) -> str:
    """Translate; fall back to key on miss so missing strings are visible."""
    return load(locale).get(key, key)


def normalize(locale: str | None) -> str:
    if locale and locale in SUPPORTED:
        return locale
    return DEFAULT
