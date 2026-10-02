"""FX rate cache for the webapp's currency-conversion column.

Uses Frankfurter (https://www.frankfurter.app), free, no auth, ECB-backed
daily reference rates. Frankfurter covers all majors we care about
(BRL, USD, EUR, GBP, CAD, etc.) — we only render conversions when both
currencies are present.

Cache layout: a single JSON dict on disk at $XDG_STATE_HOME/job-hunter/
fx_cache.json. Schema:

  {
    "base": "EUR",
    "fetched_at": "2026-05-13T20:00:00+00:00",
    "rates": { "USD": 1.07, "BRL": 5.5, "EUR": 1.0, ... }
  }

We always normalize to a single base internally; conversion between any
two currencies is `(amount / rates[source]) * rates[target]`. Frankfurter
returns rates relative to whatever base you ask for, but storing one base
means we make one HTTP call per refresh instead of one per (src, dst)
pair.

Refresh policy: 12 hours. Older than that, refetch; on network failure,
return the cached rates (stale is better than nothing). Returns None only
when there's nothing cached AND the network is unreachable.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from ..paths import Paths

log = logging.getLogger(__name__)

SUPPORTED = ("BRL", "USD", "EUR")
DEFAULT = "BRL"

_ENDPOINT = "https://api.frankfurter.dev/v1/latest"
_NORMALIZED_BASE = "EUR"  # Frankfurter's native base; cheapest choice.
_TTL = timedelta(hours=12)


@dataclass(frozen=True)
class Rates:
    base: str
    fetched_at: datetime
    rates: dict[str, float]

    def is_fresh(self) -> bool:
        age = datetime.now(UTC) - self.fetched_at
        return age < _TTL


def _cache_path(paths: Paths) -> Path:
    return paths.state_dir / "fx_cache.json"


def _read_cache(paths: Paths) -> Rates | None:
    p = _cache_path(paths)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return Rates(
            base=str(data["base"]),
            fetched_at=datetime.fromisoformat(data["fetched_at"]),
            rates={str(k): float(v) for k, v in dict(data["rates"]).items()},
        )
    except (OSError, KeyError, ValueError, TypeError) as e:
        log.warning("fx cache unreadable, ignoring: %s", e)
        return None


def _write_cache(paths: Paths, rates: Rates) -> None:
    p = _cache_path(paths)
    p.parent.mkdir(parents=True, exist_ok=True)
    body: dict[str, Any] = {
        "base": rates.base,
        "fetched_at": rates.fetched_at.isoformat(),
        "rates": rates.rates,
    }
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def _fetch_remote(timeout: float = 6.0) -> Rates | None:
    """One HTTP call. Returns None on any network/parse failure (caller falls back)."""
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            r = client.get(_ENDPOINT, params={"base": _NORMALIZED_BASE})
            r.raise_for_status()
            data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("fx fetch failed: %s", e)
        return None
    rates = dict(data.get("rates", {}))
    rates[_NORMALIZED_BASE] = 1.0  # self-rate; the API omits it.
    return Rates(
        base=_NORMALIZED_BASE,
        fetched_at=datetime.now(UTC),
        rates={str(k): float(v) for k, v in rates.items()},
    )


def load_rates(paths: Paths, *, force_refresh: bool = False) -> Rates | None:
    """Return current rates, refreshing if stale. Stale-but-cached beats None."""
    cached = _read_cache(paths)
    if cached and cached.is_fresh() and not force_refresh:
        return cached

    fresh = _fetch_remote()
    if fresh is not None:
        _write_cache(paths, fresh)
        return fresh

    # Fetch failed; serve whatever we have, even if stale.
    return cached


def convert(amount: float, src: str, dst: str, rates: Rates | None) -> float | None:
    """Convert `amount` from `src` to `dst` using `rates`. None if unconvertible."""
    if rates is None:
        return None
    src = src.upper()
    dst = dst.upper()
    if src == dst:
        return amount
    if src not in rates.rates or dst not in rates.rates:
        return None
    # Both rates are expressed relative to `rates.base`. Bridge via base.
    base_amount = amount / rates.rates[src]
    return base_amount * rates.rates[dst]


def symbol(currency: str) -> str:
    return {"BRL": "R$", "USD": "$", "EUR": "€"}.get(currency.upper(), currency.upper() + " ")
