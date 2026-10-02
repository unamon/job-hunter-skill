"""Indeed + Glassdoor HTML parsers against synthetic fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from job_hunter.sources.glassdoor import (
    parse_listing as parse_glassdoor,
)
from job_hunter.sources.glassdoor import (
    parse_salary_page,
)
from job_hunter.sources.indeed import (
    _looks_like_captcha,
)
from job_hunter.sources.indeed import (
    parse_listing as parse_indeed,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_indeed_parser_extracts_three_cards() -> None:
    html = (FIXTURES / "indeed.html").read_text(encoding="utf-8")
    postings = parse_indeed(html, "https://br.indeed.com")
    assert len(postings) == 3
    titles = {p.title for p in postings}
    assert "Senior Android Engineer" in titles
    assert "Kotlin Multiplatform Engineer" in titles
    senior = next(p for p in postings if p.title == "Senior Android Engineer")
    assert senior.company == "Nubank"
    assert senior.external_id == "abc111def222"
    assert senior.url == "https://br.indeed.com/viewjob?jk=abc111def222"
    assert senior.salary_min == 12000
    assert senior.salary_max == 18000
    assert senior.currency == "BRL"
    assert senior.remote is True  # location says "Remoto"


def test_indeed_parses_usd_salary() -> None:
    html = (FIXTURES / "indeed.html").read_text(encoding="utf-8")
    kmp = next(
        p for p in parse_indeed(html, "https://br.indeed.com") if "Kotlin Multiplatform" in p.title
    )
    assert kmp.currency == "USD"
    assert kmp.salary_min == 90000
    assert kmp.salary_max == 130000


def test_indeed_captcha_detection_positive() -> None:
    assert _looks_like_captcha("<html>...checking your browser...</html>") is True
    assert _looks_like_captcha("<html>cf-browser-verification</html>") is True
    assert _looks_like_captcha("normal html no challenge") is False


@pytest.mark.parametrize(
    "text,expected_currency,expected_min,expected_max",
    [
        ("R$ 12.000 - R$ 18.000", "BRL", 12000, 18000),
        ("USD 90,000 - 130,000", "USD", 90000, 130000),
        ("$95,000 - $135,000", "USD", 95000, 135000),
        ("", None, None, None),
    ],
)
def test_indeed_salary_parser_currencies(
    text: str,
    expected_currency: str | None,
    expected_min: int | None,
    expected_max: int | None,
) -> None:
    from job_hunter.sources.indeed import _parse_salary

    cur, lo, hi = _parse_salary(text)
    assert cur == expected_currency
    if expected_min is not None:
        assert lo == expected_min
    if expected_max is not None:
        assert hi == expected_max


def test_glassdoor_listing_parser() -> None:
    html = (FIXTURES / "glassdoor_listing.html").read_text(encoding="utf-8")
    postings = parse_glassdoor(html, "https://www.glassdoor.com")
    assert len(postings) == 2
    senior = next(p for p in postings if "Senior" in p.title)
    assert senior.company == "Nubank"
    assert senior.currency == "BRL"
    assert senior.salary_min == 13000
    assert senior.salary_max == 19500
    assert senior.remote is True


def test_glassdoor_salary_page_parser() -> None:
    html = (FIXTURES / "glassdoor_salary.html").read_text(encoding="utf-8")
    est = parse_salary_page(html, role="Senior Android Engineer", location="Brazil")
    assert est is not None
    assert est.p25 == 98000
    assert est.median == 125000
    assert est.p75 == 155000
    assert est.sample_size == 1234
    assert est.currency == "USD"
