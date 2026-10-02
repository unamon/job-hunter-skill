"""Adapter YAML schema + loader + URL matching.

Resolution order:
1. User-customized adapters in `$XDG_DATA_HOME/job-hunter/adapters_user/`
2. Bundled adapters in `skills/job-hunter/assets/adapters/`

Same `platform_signature` in adapters_user/ overrides the bundled file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml

from ..paths import Paths


class AdapterError(Exception):
    """Invalid adapter YAML or no matching adapter for URL."""


@dataclass(frozen=True)
class AdapterField:
    selector: str
    source: str
    required: bool = False
    mask: str | None = None
    fallback: str | None = None  # optional literal fallback if source missing


@dataclass(frozen=True)
class AdapterMatch:
    url_pattern: str | None = None
    dom_markers: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdapterSubmit:
    selector: str
    mode: str = "shadow"  # shadow | auto
    auto_eligible: bool = False
    pre_submit_checks: tuple[str, ...] = (
        "screenshot",
        "assert_no_pii_in_logs",
        "assert_all_required_filled",
    )
    cooldown_seconds: float = 30.0


@dataclass(frozen=True)
class Adapter:
    platform_signature: str
    version: int
    match: AdapterMatch
    fields: tuple[AdapterField, ...]
    submit: AdapterSubmit
    inherited_from: str | None = None
    source_path: Path | None = field(default=None, compare=False)

    def required_fields(self) -> tuple[AdapterField, ...]:
        return tuple(f for f in self.fields if f.required)


def _parse_adapter(data: dict[str, Any], path: Path | None = None) -> Adapter:
    if "platform_signature" not in data:
        raise AdapterError(f"{path or '<dict>'}: missing platform_signature")
    if "fields" not in data:
        raise AdapterError(f"{path or '<dict>'}: missing fields[]")
    if "submit" not in data:
        raise AdapterError(f"{path or '<dict>'}: missing submit")

    raw_match = data.get("match", {}) or {}
    match = AdapterMatch(
        url_pattern=raw_match.get("url_pattern"),
        dom_markers=tuple(raw_match.get("dom_markers") or ()),
    )

    fields: list[AdapterField] = []
    for entry in data["fields"]:
        if "selector" not in entry or "source" not in entry:
            raise AdapterError(f"{path or '<dict>'}: field missing selector/source: {entry}")
        fields.append(
            AdapterField(
                selector=str(entry["selector"]),
                source=str(entry["source"]),
                required=bool(entry.get("required", False)),
                mask=entry.get("mask"),
                fallback=entry.get("fallback"),
            )
        )

    raw_submit = data["submit"]
    submit = AdapterSubmit(
        selector=str(raw_submit["selector"]),
        mode=str(raw_submit.get("mode", "shadow")),
        auto_eligible=bool(raw_submit.get("auto_eligible", False)),
        pre_submit_checks=tuple(raw_submit.get("pre_submit_checks") or ()),
        cooldown_seconds=float(raw_submit.get("cooldown_seconds", 30.0)),
    )

    return Adapter(
        platform_signature=str(data["platform_signature"]),
        version=int(data.get("version", 1)),
        match=match,
        fields=tuple(fields),
        submit=submit,
        inherited_from=data.get("inherited_from"),
        source_path=path,
    )


def load_adapter(path: Path) -> Adapter:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise AdapterError(f"{path}: invalid YAML: {e}") from e
    if not isinstance(data, dict):
        raise AdapterError(f"{path}: top-level must be a mapping")
    return _parse_adapter(data, path)


def list_bundled() -> Path:
    """Path to the bundled adapters dir (read-only).

    Adapters live inside the package at `job_hunter/_assets/adapters/` so
    they ride along in the installed wheel. importlib.resources resolves
    the path whether running from source or from a wheel install.
    """
    from importlib.resources import files

    return Path(str(files("job_hunter") / "_assets" / "adapters"))


def list_user(paths: Paths) -> Path:
    return paths.adapters_user


def load_all(paths: Paths) -> list[Adapter]:
    """Load all known adapters with user overrides applied by signature."""
    by_sig: dict[str, Adapter] = {}

    bundled = list_bundled()
    if bundled.exists():
        for p in sorted(bundled.glob("*.yaml")):
            ad = load_adapter(p)
            by_sig[ad.platform_signature] = ad

    user = list_user(paths)
    if user.exists():
        for p in sorted(user.glob("*.yaml")):
            ad = load_adapter(p)
            by_sig[ad.platform_signature] = ad  # override

    return list(by_sig.values())


def match_url(url: str, adapters: list[Adapter]) -> Adapter | None:
    """First-match wins. User overrides come first in load_all() because they
    arrive last and overwrite the bundled entry; load_all() returns a single
    list with no duplicates, so order here is by signature insertion."""
    for ad in adapters:
        pattern = ad.match.url_pattern
        if pattern and fnmatch(url, pattern):
            return ad
    return None
