"""Source resolver: turn `profile.foo`, `secret.BAR`, `file.X`, `generate.Y`
references into a FieldPlan that `apply.py` can execute.

The resolver enforces PII isolation architecturally:
- `secret.*` values are read from `os.environ` and never echoed.
- `generate.*` callables receive only `{job_title, company, role_summary,
  public_profile_blurb}` — never PII.
- The model never sees secret values; it sees the *plan* (selector +
  source-reference), not resolved values.

`FieldPlan` is the only thing the model is allowed to inspect.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..paths import Paths
from .loader import Adapter, AdapterField


class ResolverError(Exception):
    """Raised when a required source can't be resolved."""


@dataclass(frozen=True)
class PlanEntry:
    selector: str
    source_kind: str  # profile | secret | file | generate | literal
    source_key: str
    required: bool
    mask: str | None
    has_value: bool


@dataclass
class FieldPlan:
    adapter_signature: str
    entries: list[PlanEntry] = field(default_factory=list)

    def missing_required(self) -> list[PlanEntry]:
        return [e for e in self.entries if e.required and not e.has_value]


GeneratorFn = Callable[[dict[str, str]], str]


class SecretResolver:
    """Resolves `profile.*`, `secret.*`, `file.*`, `generate.*` references.

    Real values stay inside this class. Callers receive only a FieldPlan
    (presence flags + selectors).
    """

    def __init__(
        self,
        paths: Paths,
        *,
        generators: dict[str, GeneratorFn] | None = None,
    ) -> None:
        self.paths = paths
        self._profile = _load_profile(paths)
        self._generators = generators or {}

    # ─── public API: plan-building ──────────────────────────────────────────

    def build_plan(self, adapter: Adapter, generate_inputs: dict[str, str]) -> FieldPlan:
        plan = FieldPlan(adapter_signature=adapter.platform_signature)
        for f in adapter.fields:
            kind, key = _split_source(f.source)
            has_value = self._has_value(kind, key, f, generate_inputs)
            plan.entries.append(
                PlanEntry(
                    selector=f.selector,
                    source_kind=kind,
                    source_key=key,
                    required=f.required,
                    mask=f.mask,
                    has_value=has_value,
                )
            )
        return plan

    # ─── value-fetch (used by apply.py at fill time, never exposed) ─────────

    def get_value(self, field: AdapterField, generate_inputs: dict[str, str]) -> str | None:
        kind, key = _split_source(field.source)
        v = self._fetch(kind, key, field, generate_inputs)
        if v is None and field.fallback is not None:
            return field.fallback
        return v

    def get_file_path(self, key: str) -> Path | None:
        candidate = self.paths.files_dir / key
        if candidate.exists():
            return candidate
        # Allow user to specify a relative subpath (e.g. `resume_pt.pdf` -> files/resume_pt.pdf)
        # .txt/.md variants feed text targets (cover-letter textareas).
        for variant in (f"{key}.pdf", f"{key}.docx", f"{key}.txt", f"{key}.md"):
            candidate = self.paths.files_dir / variant
            if candidate.exists():
                return candidate
        return None

    # ─── internals ──────────────────────────────────────────────────────────

    def _has_value(
        self,
        kind: str,
        key: str,
        f: AdapterField,
        generate_inputs: dict[str, str],
    ) -> bool:
        if kind == "literal":
            return True
        if kind == "profile":
            return self._profile_value(key) is not None
        if kind == "secret":
            return bool(os.environ.get(key))
        if kind == "file":
            return self.get_file_path(key) is not None
        if kind == "generate":
            return key in self._generators and bool(generate_inputs)
        return False

    def _fetch(
        self,
        kind: str,
        key: str,
        f: AdapterField,  # noqa: ARG002
        generate_inputs: dict[str, str],
    ) -> str | None:
        if kind == "literal":
            return key
        if kind == "profile":
            return self._profile_value(key)
        if kind == "secret":
            return os.environ.get(key)
        if kind == "file":
            p = self.get_file_path(key)
            return str(p) if p else None
        if kind == "generate":
            gen = self._generators.get(key)
            if gen is None:
                return None
            return gen(generate_inputs)
        raise ResolverError(f"unknown source kind: {kind}")

    def _profile_value(self, dotted: str) -> str | None:
        # Walk dotted path through profile.yaml mapping.
        cur: Any = self._profile
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        if isinstance(cur, str):
            return cur
        if isinstance(cur, int | float):
            return str(cur)
        return None


def _split_source(source: str) -> tuple[str, str]:
    """`profile.full_name` -> ("profile", "full_name")"""
    if "." not in source:
        return "literal", source
    kind, _, key = source.partition(".")
    return kind, key


def _load_profile(paths: Paths) -> dict[str, Any]:
    if not paths.profile_yaml.exists():
        return {}
    try:
        data = yaml.safe_load(paths.profile_yaml.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def build_plan(
    adapter: Adapter,
    paths: Paths,
    *,
    generate_inputs: dict[str, str] | None = None,
    generators: dict[str, GeneratorFn] | None = None,
) -> FieldPlan:
    """Convenience: build a plan without instantiating SecretResolver yourself."""
    resolver = SecretResolver(paths, generators=generators)
    return resolver.build_plan(adapter, generate_inputs or {})
