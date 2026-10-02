"""learn.py: signature hashing, ATS detection, label-based source inference."""

from __future__ import annotations

from pathlib import Path

import pytest

from job_hunter import paths as paths_mod
from job_hunter.adapters.loader import list_bundled
from job_hunter.learn import (
    compute_signature,
    draft_adapter_from_dom,
    infer_source_for_input,
    load_field_labels,
    match_known_ats,
    save_inbox_draft,
)


def test_signature_stable_across_runs() -> None:
    dom = """
    <form class='application-form widget' data-react-helmet=''>
      <input name='full_name' />
      <input name='email' />
      <input name='cpf' />
    </form>
    """
    s1 = compute_signature(dom, url="https://example.com/jobs/123/apply")
    s2 = compute_signature(dom, url="https://example.com/jobs/123/apply")
    assert s1 == s2
    assert len(s1) == 16


def test_signature_differs_for_different_inputs() -> None:
    base_form = "<form class='x'><input name='{a}'/><input name='{b}'/></form>"
    s_a = compute_signature(base_form.format(a="email", b="phone"))
    s_b = compute_signature(base_form.format(a="email", b="cpf"))
    assert s_a != s_b


def test_match_known_ats_by_host() -> None:
    assert match_known_ats("https://nubank.gupy.io/jobs/123/apply", "") == "gupy"
    assert match_known_ats("https://boards.greenhouse.io/co/jobs/123", "") == "greenhouse"
    assert match_known_ats("https://jobs.lever.co/foo/abc/apply", "") == "lever"
    assert match_known_ats("https://stark.myworkdayjobs.com/x", "") == "workday"
    assert match_known_ats("https://jobs.ashbyhq.com/foo/123", "") == "ashby"
    assert match_known_ats("https://acme.com/careers/123", "") is None


def test_infer_source_by_label_dictionary() -> None:
    labels = {
        "cpf": {"source": "secret.JOB_HUNTER_CPF", "labels": ["cpf", "documento"], "mask": "###"},
        "email": {"source": "profile.links.email", "labels": ["email"]},
    }
    src, mask = infer_source_for_input(
        name="cpf", placeholder=None, aria_label=None, surrounding_text=None, field_labels=labels
    )
    assert src == "secret.JOB_HUNTER_CPF"
    assert mask == "###"

    src, _ = infer_source_for_input(
        name=None,
        placeholder="seu email",
        aria_label=None,
        surrounding_text=None,
        field_labels=labels,
    )
    assert src == "profile.links.email"

    # Unknown -> None
    src, _ = infer_source_for_input(
        name="xyz",
        placeholder=None,
        aria_label=None,
        surrounding_text=None,
        field_labels=labels,
    )
    assert src is None


def test_draft_adapter_with_bundled_labels(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bundled = list_bundled().parent  # assets/
    paths_mod.clear_cache()
    monkeypatch.setenv("JOB_HUNTER_HOME_OVERRIDE", str(tmp_path))
    p = paths_mod.resolve()
    p.config_dir.mkdir(parents=True, exist_ok=True)
    labels = load_field_labels(p, bundled)
    dom = """
    <form>
      <input name='nome_completo' required>
      <input name='cpf' required>
      <input name='telefone' required>
      <input type='file' name='curriculo' required>
      <textarea name='carta_de_apresentacao'></textarea>
    </form>
    """
    draft = draft_adapter_from_dom(
        dom,
        signature="abc1234567890def",
        url_pattern="https://example.com/jobs/*",
        field_labels=labels,
    )
    assert draft["platform_signature"] == "abc1234567890def"
    assert draft["submit"]["auto_eligible"] is False
    # Each field has a source (either inferred or "TODO" for unmatched).
    fields = draft["fields"]
    sources = {f["selector"]: f["source"] for f in fields}
    # Lenient: at least cpf was inferred since the label dictionary has "cpf"
    assert any("cpf" in sel and src.startswith("secret.") for sel, src in sources.items())


def test_save_inbox_draft_writes_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    paths_mod.clear_cache()
    monkeypatch.setenv("JOB_HUNTER_HOME_OVERRIDE", str(tmp_path))
    p = paths_mod.resolve()
    p.ensure()

    draft = {
        "platform_signature": "test_signature_1",
        "version": 1,
        "fields": [],
        "submit": {"selector": "button"},
    }
    out = save_inbox_draft(p, "test_signature_1", draft)
    assert out.exists()
    assert "platform_signature: test_signature_1" in out.read_text(encoding="utf-8")
