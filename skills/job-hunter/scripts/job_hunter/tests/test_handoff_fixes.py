"""Tests for the handoff-doc fixes:
- #4 ATS auth-wall detection + APPLYING_BLOCKED_AUTH stage.
- #6 LinkedIn URL normalization across regional subdomains.
- title dedup helper.
- bundled adapters resolve via importlib.resources (wheel-safe).
"""

from __future__ import annotations

from job_hunter.adapters.loader import list_bundled
from job_hunter.apply import detect_auth_wall
from job_hunter.models import ACTIVE_STAGES, Stage
from job_hunter.sources.linkedin import (
    _dedup_title,
    canonical_job_url,
    normalize_job_url,
    parse_search_results,
)


def test_bundled_adapters_resolve_from_package() -> None:
    """Confirm list_bundled() returns a path that exists AND contains the 5 bundled YAMLs.
    Regression test for #1 root cause: pre-fix the path resolved outside the wheel."""
    d = list_bundled()
    assert d.exists(), f"bundled dir missing: {d}"
    sigs = {p.stem for p in d.glob("*.yaml")}
    assert {"gupy", "greenhouse", "lever", "workday", "ashby", "workable"} <= sigs


# ─── #6 URL normalization ────────────────────────────────────────────────────


def test_canonical_job_url() -> None:
    assert canonical_job_url("4413978233") == "https://www.linkedin.com/jobs/view/4413978233/"


def test_normalize_regional_subdomains() -> None:
    cases = [
        "https://in.linkedin.com/jobs/view/4413978233/",
        "https://br.linkedin.com/jobs/view/4413978233?utm=foo",
        "https://uk.linkedin.com/jobs/view/4413978233/foo/bar",
        "https://www.linkedin.com/jobs/view/4413978233/?ref=x",
    ]
    for url in cases:
        assert normalize_job_url(url) == "https://www.linkedin.com/jobs/view/4413978233/", url


def test_normalize_non_linkedin_passthrough() -> None:
    url = "https://nubank.gupy.io/jobs/4392838"
    assert normalize_job_url(url) == url


def test_parse_authenticated_yields_canonical_urls() -> None:
    html = """
    <ul>
      <li data-occludable-job-id="4413978233">
        <div class="artdeco-entity-lockup">
          <a class="job-card-container__link" href="/jobs/view/4413978233/?utm=foo">link</a>
          <div class="artdeco-entity-lockup__title">Senior Android Engineer</div>
          <div class="artdeco-entity-lockup__subtitle">Nubank</div>
          <div class="artdeco-entity-lockup__caption">São Paulo (Remote)</div>
        </div>
      </li>
    </ul>
    """
    postings = parse_search_results(html, "https://in.linkedin.com")  # regional base
    assert len(postings) == 1
    # URL must be canonical even though base_url was regional
    assert postings[0].url == "https://www.linkedin.com/jobs/view/4413978233/"


# ─── title dedup ─────────────────────────────────────────────────────────────


def test_dedup_doubled_title() -> None:
    raw = "Senior Android Engineer Senior Android Engineer"
    assert _dedup_title(raw) == "Senior Android Engineer"


def test_dedup_with_verification_suffix() -> None:
    raw = "Mobile Software Engineer with verification"
    assert _dedup_title(raw) == "Mobile Software Engineer"


def test_dedup_normal_title_passthrough() -> None:
    assert _dedup_title("Android Developer – Remote") == "Android Developer – Remote"


# ─── #4 auth-wall detection ──────────────────────────────────────────────────


def test_auth_wall_workday_authgwy_url() -> None:
    reason = detect_auth_wall(
        url="https://zillow.wd5.myworkdayjobs.com/wday/authgwy/Zillow_External_Career_Site/login",
    )
    assert reason is not None
    assert "Workday" in reason


def test_auth_wall_generic_login_url() -> None:
    reason = detect_auth_wall(url="https://careers.example.com/auth/login?next=/apply/123")
    assert reason is not None


def test_auth_wall_body_phrase() -> None:
    body = """<html><body>
        <h1>Welcome back</h1>
        <p>Please sign in to apply for this position.</p>
    </body></html>"""
    reason = detect_auth_wall(url="https://job.example.com/apply", body_text=body)
    assert reason is not None
    assert "sign in to apply" in reason


def test_auth_wall_clean_page_returns_none() -> None:
    body = "<html><body><form id='application_form'>...</form></body></html>"
    reason = detect_auth_wall(url="https://boards.greenhouse.io/foo/jobs/1", body_text=body)
    assert reason is None


def test_applying_blocked_auth_in_active_stages() -> None:
    """The new stage must be in ACTIVE_STAGES so job-hunter list/review surfaces it."""
    assert Stage.APPLYING_BLOCKED_AUTH in ACTIVE_STAGES
    assert Stage.APPLYING_BLOCKED_AUTH.value == "applying_blocked_auth"
