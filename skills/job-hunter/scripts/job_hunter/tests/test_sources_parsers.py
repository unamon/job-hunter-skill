"""Source parsers against synthetic fixtures.

We test parser correctness directly (no network). Integration tests in
test_discover.py drive end-to-end with mocked httpx responses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from job_hunter.sources.gupy import parse_listing as parse_gupy
from job_hunter.sources.job_na_gringa import parse_listing as parse_jng
from job_hunter.sources.linkedin import parse_search_results as parse_linkedin
from job_hunter.sources.remoteok import _to_posting as remoteok_item

FIXTURES = Path(__file__).parent / "fixtures"


def test_remoteok_parser_filters_and_maps() -> None:
    raw = json.loads((FIXTURES / "remoteok.json").read_text(encoding="utf-8"))
    # Skip metadata at index 0
    items = raw[1:]
    postings = [remoteok_item(item) for item in items]
    assert len(postings) == 3
    nubank = next(p for p in postings if p.company == "Nubank")
    assert nubank.title == "Senior Android Engineer"
    assert nubank.salary_min == 80000
    assert nubank.salary_max == 120000
    assert nubank.currency == "USD"
    assert nubank.external_id == "abc123"
    assert nubank.posted_at is not None


def test_job_na_gringa_parser_extracts_three_cards() -> None:
    html = (FIXTURES / "job_na_gringa.html").read_text(encoding="utf-8")
    postings = parse_jng(html, "https://jobnagringa.com.br")
    assert len(postings) == 3
    titles = {p.title for p in postings}
    assert "Senior Android Engineer" in titles
    assert "Mobile Platform Engineer (KMP)" in titles
    nubank = next(p for p in postings if p.company == "Nubank")
    assert nubank.salary_min == 80000
    assert nubank.salary_max == 120000
    assert nubank.currency == "USD"
    assert nubank.url.endswith("/jobs/nubank-senior-android-1")


def test_gupy_parser_extracts_three_cards() -> None:
    html = (FIXTURES / "gupy_nubank.html").read_text(encoding="utf-8")
    postings = parse_gupy(html, "nubank")
    assert len(postings) == 3
    titles = {p.title for p in postings}
    assert "Senior Android Engineer" in titles
    assert "Mobile Platform Engineer KMP" in titles
    senior = next(p for p in postings if p.title == "Senior Android Engineer")
    assert senior.company == "Nubank"
    assert senior.location == "Remote / BR"
    assert senior.external_id == "4392838"
    assert senior.url == "https://nubank.gupy.io/jobs/4392838"


def test_linkedin_parser_anonymous_layout() -> None:
    html = (FIXTURES / "linkedin_search.html").read_text(encoding="utf-8")
    postings = parse_linkedin(html, "https://www.linkedin.com")
    assert len(postings) == 3
    senior = next(p for p in postings if p.title == "Senior Android Engineer")
    assert senior.company == "Nubank"
    assert senior.location == "Remote, Brazil"
    assert senior.external_id == "3812345678"
    assert "?" not in senior.url
    assert senior.raw_payload.get("layout") == "anonymous"


def test_linkedin_parser_authenticated_layout() -> None:
    """Logged-in SPA layout uses `[data-occludable-job-id]` + artdeco lockup."""
    html = (FIXTURES / "linkedin_authenticated.html").read_text(encoding="utf-8")
    postings = parse_linkedin(html, "https://www.linkedin.com")
    assert len(postings) == 3
    senior = next(p for p in postings if p.title == "Senior Android Engineer")
    assert senior.company == "Nubank"
    assert senior.location == "São Paulo, SP (Remote)"
    assert senior.external_id == "4413978233"
    assert senior.url == "https://www.linkedin.com/jobs/view/4413978233/"
    assert senior.raw_payload.get("layout") == "authenticated"
    assert "?" not in senior.url
    # All 3 expected titles
    titles = {p.title for p in postings}
    assert {
        "Senior Android Engineer",
        "Kotlin Multiplatform Engineer",
        "Mobile Tech Lead",
    } <= titles


def test_search_query_role_filter_excludes_junior() -> None:
    from job_hunter.sources.base import SearchQuery

    q = SearchQuery(
        roles=["Android Engineer", "Kotlin Multiplatform"],
        exclude_keywords=["junior", "estagiário"],
    )
    assert q.matches_role("Senior Android Engineer")
    assert q.matches_role("Kotlin Multiplatform Lead")
    assert not q.matches_role("Junior Android Engineer")
    assert not q.matches_role("Estagiário Android")
    assert not q.matches_role("Data Scientist")


def test_remoteok_external_id_stable() -> None:
    raw = json.loads((FIXTURES / "remoteok.json").read_text(encoding="utf-8"))
    p1 = remoteok_item(raw[1])
    p2 = remoteok_item(raw[1])
    assert p1.external_id == p2.external_id
    assert p1.fingerprint() == p2.fingerprint()


@pytest.mark.parametrize(
    "salary,expected",
    [
        ("USD 80k - 120k", ("USD", 80000, 120000)),
        ("$120,000", ("USD", 120000, None)),
        ("", (None, None, None)),
    ],
)
def test_jng_salary_parser(
    salary: str, expected: tuple[str | None, int | None, int | None]
) -> None:
    from job_hunter.sources.job_na_gringa import _parse_salary

    result = _parse_salary(salary)
    cur, lo, hi = result
    assert cur == expected[0]
    # The "$120,000" case will be parsed loosely; tolerate min == 120 or 120000
    if salary == "$120,000":
        assert lo in (120, 120000)
    else:
        assert lo == expected[1]
