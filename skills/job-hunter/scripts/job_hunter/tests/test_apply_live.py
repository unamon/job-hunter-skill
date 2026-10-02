"""apply_live: shadow fill against a fake page (no Playwright needed)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from job_hunter import paths as paths_mod
from job_hunter.adapters import (
    Adapter,
    AdapterField,
    AdapterMatch,
    AdapterSubmit,
    SecretResolver,
)
from job_hunter.apply_live import check_required_on_page, fill_fields, run_shadow
from job_hunter.models import FillOutcome

SECRET_PHONE = "+55 21 90000-0000"


class FakeLocator:
    def __init__(self, page: FakePage, selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> FakeLocator:
        return self

    def count(self) -> int:
        return 1 if self.selector in self.page.elements else 0

    def evaluate(self, js: str) -> bool:
        if "e.style.color = 'transparent'" in js:
            self.page.hidden.add(self.selector)
        elif "delete e.dataset.jhColor" in js:
            self.page.hidden.discard(self.selector)
        return self.page.elements[self.selector] == "file"

    def fill(self, value: str, timeout: float = 0) -> None:  # noqa: ARG002
        if self.page.elements[self.selector] == "broken":
            raise RuntimeError(f"cannot type {value!r}")
        self.page.values[self.selector] = value

    def set_input_files(self, path: str, timeout: float = 0) -> None:  # noqa: ARG002
        self.page.values[self.selector] = Path(path).name

    def click(self, timeout: float = 0) -> None:  # noqa: ARG002
        self.page.clicked.append(self.selector)


class FakePage:
    def __init__(self, elements: dict[str, str]) -> None:
        self.elements = elements  # selector -> "text" | "file" | "broken"
        self.values: dict[str, str] = {}
        self.clicked: list[str] = []
        self.hidden: set[str] = set()
        self.masked: dict[str, list[str]] = {}

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    def screenshot(self, path: str, full_page: bool = False) -> None:  # noqa: ARG002
        self.masked[Path(path).name] = sorted(self.hidden)
        Path(path).write_bytes(b"png")

    def wait_for_timeout(self, _ms: float) -> None:
        return None

    def wait_for_load_state(self, _state: str, timeout: float = 0) -> None:  # noqa: ARG002
        return None


ADAPTER = Adapter(
    platform_signature="fake",
    version=1,
    match=AdapterMatch(url_pattern="*fake/*"),
    fields=(
        AdapterField("#first", "profile.first_name", required=True),
        AdapterField("#phone", "secret.JOB_HUNTER_PHONE"),
        AdapterField("#resume", "file.resume_en", required=True),
        AdapterField("#letter", "file.cover_letter_en"),
        AdapterField("#gone", "profile.headline"),
    ),
    submit=AdapterSubmit(selector="#submit", pre_submit_checks=("screenshot",)),
)

ELEMENTS = {
    "#first": "text",
    "#phone": "text",
    "#resume": "file",
    "#letter": "text",
    "#submit": "button",
}


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SecretResolver:
    paths_mod.clear_cache()
    monkeypatch.setenv("JOB_HUNTER_HOME_OVERRIDE", str(tmp_path))
    monkeypatch.setenv("JOB_HUNTER_PHONE", SECRET_PHONE)
    p = paths_mod.resolve()
    p.ensure()
    p.profile_yaml.write_text("first_name: Ana\nheadline: Dev\n", encoding="utf-8")
    (p.files_dir / "resume_en.pdf").write_bytes(b"%PDF")
    (p.files_dir / "cover_letter_en.txt").write_text("Hello team.\n", encoding="utf-8")
    return SecretResolver(p)


def _run(
    resolver: SecretResolver, page: FakePage, answers: list[str], run_dir: Path
) -> tuple[Any, list[str]]:
    asked: list[str] = []

    def prompt(label: str) -> str:
        asked.append(label)
        return answers.pop(0)

    plan = resolver.build_plan(ADAPTER, {"x": "y"})
    report = run_shadow(
        page,
        ADAPTER,
        plan,
        resolver,
        generate_inputs={},
        run_dir=run_dir,
        locale_hint="en",
        label="001 Fake",
        log_paths=[],
        prompt=prompt,
    )
    return report, asked


def test_fill_fields_values_files_and_text(resolver: SecretResolver) -> None:
    page = FakePage(dict(ELEMENTS))
    results = {r.selector: r for r in fill_fields(page, ADAPTER, resolver, {})}

    assert page.values["#first"] == "Ana"
    assert page.values["#phone"] == SECRET_PHONE
    assert page.values["#resume"] == "resume_en.pdf"
    assert page.values["#letter"] == "Hello team."
    assert results["#gone"].status == "missing"
    assert check_required_on_page(ADAPTER, list(results.values())).ok


def test_fill_error_does_not_leak_value(resolver: SecretResolver) -> None:
    page = FakePage({**ELEMENTS, "#phone": "broken"})
    results = {r.selector: r for r in fill_fields(page, ADAPTER, resolver, {})}
    assert results["#phone"].status == "error"
    assert SECRET_PHONE not in results["#phone"].detail


def test_required_missing_on_page_fails_check(resolver: SecretResolver) -> None:
    elements = {k: v for k, v in ELEMENTS.items() if k != "#resume"}
    results = fill_fields(FakePage(elements), ADAPTER, resolver, {})
    check = check_required_on_page(ADAPTER, results)
    assert not check.ok
    assert "#resume" in check.detail


def test_shadow_yes_submits_and_masks_secrets(resolver: SecretResolver, tmp_path: Path) -> None:
    page = FakePage(dict(ELEMENTS))
    report, _ = _run(resolver, page, ["y"], tmp_path)

    assert report.outcome == FillOutcome.SUBMITTED.value
    assert page.clicked == ["#submit"]
    assert page.masked == {"before_submit.png": ["#phone"], "after_submit.png": ["#phone"]}
    assert (tmp_path / "before_submit.png").exists()
    assert (tmp_path / "after_submit.png").exists()
    assert page.hidden == set(), "secret fields must be restored after the screenshot"
    raw = (tmp_path / "report.json").read_text(encoding="utf-8")
    assert SECRET_PHONE not in raw
    assert json.loads(raw)["fields_filled"] == 4


def test_shadow_no_never_clicks(resolver: SecretResolver, tmp_path: Path) -> None:
    page = FakePage(dict(ELEMENTS))
    report, _ = _run(resolver, page, ["n"], tmp_path)
    assert report.outcome == FillOutcome.ABORTED_FOR_REVIEW.value
    assert report.reason == "user_declined"
    assert page.clicked == []


def test_shadow_edit_asks_again(resolver: SecretResolver, tmp_path: Path) -> None:
    page = FakePage(dict(ELEMENTS))
    report, asked = _run(resolver, page, ["edit", "y"], tmp_path)
    assert len(asked) == 2
    assert report.outcome == FillOutcome.SUBMITTED.value


def test_shadow_eof_is_no_tty(resolver: SecretResolver, tmp_path: Path) -> None:
    report, _ = _run(resolver, FakePage(dict(ELEMENTS)), [""], tmp_path)
    assert report.outcome == FillOutcome.ABORTED_FOR_REVIEW.value
    assert report.reason == "no_tty"
