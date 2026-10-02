"""Adapter loader + resolver + URL matching."""

from __future__ import annotations

from pathlib import Path

import pytest

from job_hunter import paths as paths_mod
from job_hunter.adapters import (
    AdapterError,
    list_bundled,
    list_user,
    load_adapter,
    load_all,
    match_url,
)
from job_hunter.adapters.generators import DEFAULTS as GENERATORS
from job_hunter.adapters.resolver import SecretResolver, build_plan


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> paths_mod.Paths:
    paths_mod.clear_cache()
    monkeypatch.setenv("JOB_HUNTER_HOME_OVERRIDE", str(tmp_path))
    p = paths_mod.resolve()
    p.ensure()
    return p


def test_bundled_adapters_load() -> None:
    bundled_dir = list_bundled()
    yaml_files = sorted(bundled_dir.glob("*.yaml"))
    assert {p.stem for p in yaml_files} == {
        "gupy",
        "greenhouse",
        "lever",
        "workday",
        "ashby",
        "workable",
    }
    for p in yaml_files:
        ad = load_adapter(p)
        assert ad.submit.auto_eligible is False
        assert ad.fields, f"{p}: empty fields list"


def test_load_all_user_overrides_bundled(home: paths_mod.Paths) -> None:
    paths = home
    user_dir = list_user(paths)
    user_dir.mkdir(parents=True, exist_ok=True)
    override = """
platform_signature: gupy
version: 99
match:
  url_pattern: "*.example.com/*"
fields:
  - selector: "input"
    source: profile.full_name
submit:
  selector: "button"
"""
    (user_dir / "gupy.yaml").write_text(override)

    adapters = load_all(paths)
    gupy = next(a for a in adapters if a.platform_signature == "gupy")
    assert gupy.version == 99
    assert gupy.match.url_pattern == "*.example.com/*"


def test_match_url_picks_correct_adapter(home: paths_mod.Paths) -> None:
    adapters = load_all(home)
    # Gupy match (note: leading "https://" handled by fnmatch on full URL)
    matched = match_url("https://nubank.gupy.io/jobs/4392838", adapters)
    assert matched is not None
    assert matched.platform_signature == "gupy"

    # Greenhouse
    matched = match_url("https://boards.greenhouse.io/some-company/jobs/123", adapters)
    assert matched is not None
    assert matched.platform_signature == "greenhouse"

    # Workable
    matched = match_url("https://apply.workable.com/acme/j/85FBE31A14/apply/", adapters)
    assert matched is not None
    assert matched.platform_signature == "workable"

    # No match
    assert match_url("https://example.com/jobs/1", adapters) is None


def test_invalid_yaml_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("platform_signature: x\nfields: []\n# missing submit")
    with pytest.raises(AdapterError):
        load_adapter(p)


def test_resolver_profile_secret_file_generate(
    home: paths_mod.Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = home
    # Profile
    paths.profile_yaml.write_text(
        "first_name: Luisa\nlast_name: Martins\nlinks:\n  email: lm@example.com\n"
    )
    # File
    paths.files_dir.mkdir(parents=True, exist_ok=True)
    (paths.files_dir / "resume_pt.pdf").write_text("(stub resume)")
    # Secret
    monkeypatch.setenv("JOB_HUNTER_PHONE", "11999999999")  # job-hunter:allow-pii
    # Generator
    resolver = SecretResolver(paths, generators=GENERATORS)

    bundled_dir = list_bundled()
    gupy = load_adapter(bundled_dir / "gupy.yaml")

    plan = resolver.build_plan(
        gupy,
        generate_inputs={
            "job_title": "Senior Android Engineer",
            "company": "Nubank",
            "role_summary": "kotlin-first stack",
            "public_profile_blurb": "Senior Android dev",
        },
    )

    # All entries present
    by_sel = {e.selector: e for e in plan.entries}
    assert by_sel["input[name='phone']"].has_value is True
    assert by_sel["input[name='phone']"].source_kind == "secret"
    assert by_sel["input[type='file'][name='resume']"].has_value is True
    assert by_sel["textarea[name='cover_letter']"].has_value is True

    # No CPF in env yet → required field missing
    missing = plan.missing_required()
    cpf_missing = any(e.source_key == "JOB_HUNTER_CPF" for e in missing)
    assert cpf_missing


def test_generator_receives_only_public_context() -> None:
    """The cover-letter generator must not have access to env vars or profile."""
    from job_hunter.adapters.generators import cover_letter

    ctx = {
        "job_title": "Senior Android Engineer",
        "company": "Nubank",
        "role_summary": "platform team",
        "public_profile_blurb": "Senior mobile dev",
    }
    text = cover_letter(ctx)
    assert "Nubank" in text
    assert "Senior Android Engineer" in text
    # Function signature literally only accepts a dict; this is a structural assert.
    import inspect

    sig = inspect.signature(cover_letter)
    assert list(sig.parameters) == ["ctx"]


def test_resolver_missing_secret_does_not_leak_value(
    home: paths_mod.Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = home
    monkeypatch.delenv("JOB_HUNTER_CPF", raising=False)
    monkeypatch.delenv("JOB_HUNTER_PHONE", raising=False)
    resolver = SecretResolver(paths)
    gupy = load_adapter(list_bundled() / "gupy.yaml")
    plan = resolver.build_plan(gupy, generate_inputs={})

    # No env var set → has_value False, but the plan still returns the entry
    cpf_entry = next(e for e in plan.entries if e.source_key == "JOB_HUNTER_CPF")
    assert cpf_entry.has_value is False
    # Plan never contains a `value` field. By construction.
    assert not hasattr(cpf_entry, "value")


def test_resolver_file_resolution(home: paths_mod.Paths) -> None:
    paths = home
    paths.files_dir.mkdir(parents=True, exist_ok=True)
    (paths.files_dir / "resume_en.pdf").write_text("stub")
    resolver = SecretResolver(paths)
    p = resolver.get_file_path("resume_en")
    assert p is not None
    assert p.suffix == ".pdf"


def test_build_plan_convenience(home: paths_mod.Paths) -> None:
    paths = home
    paths.profile_yaml.write_text("first_name: L\n")
    adapter = load_adapter(list_bundled() / "greenhouse.yaml")
    plan = build_plan(adapter, paths)
    assert plan.adapter_signature == "greenhouse"
    first_name = next(e for e in plan.entries if e.source_key == "first_name")
    assert first_name.has_value is True
