"""Adapter learner: inspect unknown forms, hash a platform signature, draft
an adapter into `$XDG_DATA_HOME/job-hunter/adapters_inbox/<sig>.yaml`.

The browser-level inspection (navigate + screenshot + DOM dump) is the
Playwright surface (Phase 5+ — implemented separately as the dev tests
the live runner). This module provides:

- `compute_signature(form_dom)`: deterministic 16-hex signature.
- `match_known_ats(signature, dom_text)`: try to spot Greenhouse/Lever/...
  by structural markers BEFORE drafting a brand-new adapter.
- `draft_adapter_from_dom(dom_text, signature, field_labels)`: heuristic
  per-input matching against `field_labels.yaml` to produce a YAML draft.

All inputs/outputs are strings + dicts so we can test against synthetic
HTML fixtures without a real browser.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from selectolax.parser import HTMLParser

from .paths import Paths

KNOWN_HOSTS: tuple[tuple[str, str], ...] = (
    ("*.gupy.io", "gupy"),
    ("boards.greenhouse.io", "greenhouse"),
    ("*jobs.lever.co", "lever"),
    ("*.myworkdayjobs.com", "workday"),
    ("*jobs.ashbyhq.com", "ashby"),
    ("apply.workable.com", "workable"),
    ("*.smartrecruiters.com", "smartrecruiters"),
    ("*.recruitee.com", "recruitee"),
    ("*.bamboohr.com", "bamboohr"),
    ("*.personio.com", "personio"),
    ("*.jobvite.com", "jobvite"),
)


@dataclass(frozen=True)
class LearnedField:
    selector: str
    inferred_source: str | None
    required: bool
    mask: str | None
    raw_label: str


def compute_signature(form_dom: str, url: str | None = None) -> str:
    """Deterministic signature: SHA-256(class list + framework + input names + path).

    16 hex chars. See references/self_improvement.md.
    """
    tree = HTMLParser(form_dom)
    form = tree.css_first("form") or tree.body
    class_list = ""
    framework = ""
    if form is not None:
        cls = (form.attributes.get("class") or "").split()
        class_list = " ".join(sorted(cls))
        for marker in ("data-react-helmet", "data-ember-action", "ng-version"):
            if form.attributes.get(marker) is not None:
                framework = marker
                break
    inputs = sorted(
        (n.attributes.get("name") or n.attributes.get("id") or "")
        for n in tree.css("input, textarea, select")
    )
    inputs = [i for i in inputs if i]

    path_template = ""
    if url:
        path_template = _path_template(url)

    material = "\n".join([class_list, framework, ",".join(inputs), path_template])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _path_template(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    parts = []
    for seg in parsed.path.split("/"):
        if not seg:
            continue
        if seg.isdigit():
            parts.append(":id")
        elif re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", seg):
            parts.append(":uuid")
        else:
            parts.append(seg)
    return f"{parsed.hostname or ''}/{'/'.join(parts)}"


def match_known_ats(url: str, dom_text: str) -> str | None:
    """Return the canonical platform_signature if the URL matches a known ATS."""
    from fnmatch import fnmatch
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    for pattern, sig in KNOWN_HOSTS:
        if fnmatch(host, pattern):
            return sig
    # Fallback DOM hints
    lowered = dom_text[:5000].lower()
    if "boards.greenhouse.io" in lowered:
        return "greenhouse"
    if "jobs.lever.co" in lowered:
        return "lever"
    if "myworkdayjobs.com" in lowered:
        return "workday"
    return None


def bundled_assets_dir() -> Path:
    """In-package assets dir; mirrors `assets/` from source for wheel installs."""
    from importlib.resources import files

    return Path(str(files("job_hunter") / "_assets"))


def load_field_labels(paths: Paths, bundled_assets: Path) -> dict[str, Any]:
    """Merge bundled `field_labels.yaml` with any user override.

    Returns: {canonical_name: {"source": "...", "labels": [...], "mask": "..."}}
    """
    bundle = bundled_assets / "field_labels.yaml"
    base: dict[str, Any] = {}
    if bundle.exists():
        raw = yaml.safe_load(bundle.read_text(encoding="utf-8")) or {}
        if isinstance(raw, dict):
            base = raw
    user = paths.field_labels_override
    if user.exists():
        try:
            override = yaml.safe_load(user.read_text(encoding="utf-8")) or {}
            if isinstance(override, dict):
                base = {**base, **override}
        except yaml.YAMLError:
            pass
    return base


def infer_source_for_input(
    *,
    name: str | None,
    placeholder: str | None,
    aria_label: str | None,
    surrounding_text: str | None,
    field_labels: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Returns (source_string, mask) for the best matching dictionary entry."""
    haystack = " ".join(s for s in (name, placeholder, aria_label, surrounding_text) if s).lower()
    if not haystack:
        return None, None

    best_match: tuple[str, str | None, int] | None = None  # (source, mask, score)
    for entry in field_labels.values():
        if not isinstance(entry, dict):
            continue
        labels = entry.get("labels") or []
        score = 0
        for label in labels:
            if not isinstance(label, str):
                continue
            if label.lower() in haystack:
                score += len(label.split())
        if score > 0:
            source = entry.get("source")
            mask = entry.get("mask")
            if isinstance(source, str) and (best_match is None or score > best_match[2]):
                best_match = (source, mask if isinstance(mask, str) else None, score)
    if best_match is None:
        return None, None
    return best_match[0], best_match[1]


def draft_adapter_from_dom(
    dom_text: str,
    *,
    signature: str,
    url_pattern: str,
    field_labels: dict[str, Any],
) -> dict[str, Any]:
    """Produce an adapter dict for YAML serialization. Marks unknown inputs as TODO."""
    tree = HTMLParser(dom_text)
    fields: list[dict[str, Any]] = []
    for node in tree.css("input, textarea, select"):
        kind = (node.tag or "").lower()
        input_type = (node.attributes.get("type") or "").lower()
        if kind == "input" and input_type in {"hidden", "submit", "button"}:
            continue
        name = node.attributes.get("name")
        placeholder = node.attributes.get("placeholder")
        aria_label = node.attributes.get("aria-label")
        required = node.attributes.get("required") is not None
        selector = _selector_for(kind, name, node.attributes)
        surrounding = _label_text(node)
        source, mask = infer_source_for_input(
            name=name,
            placeholder=placeholder,
            aria_label=aria_label,
            surrounding_text=surrounding,
            field_labels=field_labels,
        )
        entry: dict[str, Any] = {
            "selector": selector,
            "source": source or "TODO",
            "required": required,
        }
        if mask:
            entry["mask"] = mask
        fields.append(entry)

    return {
        "platform_signature": signature,
        "version": 1,
        "match": {"url_pattern": url_pattern, "dom_markers": []},
        "fields": fields,
        "submit": {
            "selector": "button[type='submit']",
            "mode": "shadow",
            "auto_eligible": False,
            "pre_submit_checks": [
                "screenshot",
                "assert_no_pii_in_logs",
                "assert_all_required_filled",
            ],
            "cooldown_seconds": 30,
        },
    }


def _selector_for(tag: str, name: str | None, attrs: dict[str, str | None]) -> str:
    if name:
        return f"{tag}[name='{name}']"
    if attrs.get("id"):
        return f"#{attrs['id']}"
    return tag


def _label_text(node: object) -> str:
    """Naive: read sibling/parent text. Real impl would walk to <label for=...>."""
    text = getattr(node, "text", lambda **_: "")(deep=False)
    return text or ""


def save_inbox_draft(paths: Paths, signature: str, draft: dict[str, Any]) -> Path:
    paths.adapters_inbox.mkdir(parents=True, exist_ok=True)
    target = paths.adapters_inbox / f"{signature}.yaml"
    target.write_text(yaml.safe_dump(draft, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return target


__all__ = [
    "LearnedField",
    "KNOWN_HOSTS",
    "compute_signature",
    "draft_adapter_from_dom",
    "infer_source_for_input",
    "load_field_labels",
    "match_known_ats",
    "save_inbox_draft",
]


def _silence(_: Iterable[object]) -> None:
    return None
