"""Discovery orchestrator: profile → source → DB upsert + run report."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx
import yaml
from sqlmodel import Session, select

from .models import Application, Job, Stage
from .paths import Paths
from .sources import (
    DiscoveryReport,
    JobPosting,
    SearchQuery,
    Source,
    SourceError,
    new_run_dir,
    write_report,
)
from .sources.base import RateLimiter, default_client

logger = logging.getLogger(__name__)


def load_query(paths: Paths) -> SearchQuery:
    """Build a SearchQuery from profile.yaml. Missing fields → defaults."""
    if not paths.profile_yaml.exists():
        return SearchQuery()
    try:
        data = yaml.safe_load(paths.profile_yaml.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        logger.warning("profile.yaml unreadable: %s", e)
        return SearchQuery()
    return SearchQuery(
        roles=list(data.get("roles") or []),
        seniority=list(data.get("seniority") or []),
        locations=list(data.get("locations") or []),
        exclude_keywords=list(data.get("exclude_keywords") or []),
        languages=list(data.get("languages") or []),
    )


async def run_discover(
    source: Source,
    query: SearchQuery,
    paths: Paths,
    sess: Session,
) -> tuple[DiscoveryReport, list[int]]:
    """Discover via the given source, upsert into DB, return (report, new_job_ids)."""
    report = DiscoveryReport(
        source=source.name,
        started_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    run_dir = new_run_dir(paths, source.name)
    new_job_ids: list[int] = []
    limiter = RateLimiter(paths)

    async def _on_request(_: httpx.Request) -> None:
        if source.rate_limit is not None:
            await limiter.wait(source.rate_limit)

    async with default_client() as client:
        client.event_hooks["request"].append(_on_request)
        try:
            async for posting in source.discover(query, client):
                report.discovered += 1
                try:
                    job_id, was_new = upsert_posting(sess, posting)
                    if was_new:
                        report.new += 1
                        new_job_ids.append(job_id)
                    else:
                        report.updated += 1
                except Exception as e:  # noqa: BLE001
                    report.record_error(f"upsert({posting.url})", e)
        except SourceError as e:
            report.record_error("source", e)
        except Exception as e:  # noqa: BLE001
            report.record_error("discover", e)

    report.finished_at = datetime.now(UTC).isoformat(timespec="seconds")
    write_report(run_dir, report)
    return report, new_job_ids


def upsert_posting(sess: Session, posting: JobPosting) -> tuple[int, bool]:
    """INSERT or UPDATE by (source, external_id). Returns (job_id, was_new)."""
    existing = sess.exec(
        select(Job).where((Job.source == posting.source) & (Job.external_id == posting.external_id))
    ).first()
    now = datetime.now(UTC).replace(tzinfo=None)

    if existing is None:
        job = Job(
            source=posting.source,
            external_id=posting.external_id,
            url=posting.url,
            title=posting.title,
            company=posting.company,
            location=posting.location,
            remote=posting.remote,
            salary_min=posting.salary_min,
            salary_max=posting.salary_max,
            currency=posting.currency,
            posted_at=_strip_tz(posting.posted_at),
            description=posting.description,
            raw_payload=_dump_payload(posting.raw_payload),
            scraped_at=now,
            fingerprint=posting.fingerprint(),
            tags=_dump_tags(posting.tags),
            salary_period=posting.salary_period,
        )
        sess.add(job)
        sess.commit()
        sess.refresh(job)
        assert job.id is not None
        app = Application(
            job_id=job.id,
            current_stage=Stage.DISCOVERED.value,
            updated_at=now,
        )
        sess.add(app)
        sess.commit()
        return job.id, True

    # Update: preserve scraped_at? No — refresh scraped_at, leave others sticky.
    existing.title = posting.title
    existing.company = posting.company
    existing.location = posting.location or existing.location
    existing.salary_min = posting.salary_min or existing.salary_min
    existing.salary_max = posting.salary_max or existing.salary_max
    existing.currency = posting.currency or existing.currency
    existing.salary_period = posting.salary_period or existing.salary_period
    existing.description = posting.description or existing.description
    if posting.raw_payload:
        existing.raw_payload = _dump_payload(posting.raw_payload)
    existing.scraped_at = now
    existing.fingerprint = posting.fingerprint()
    if posting.tags:
        # Merge: union existing + new tags so we never lose info on re-scrape.
        merged = _merge_tags(existing.tags, posting.tags)
        existing.tags = _dump_tags(merged)
    sess.add(existing)
    sess.commit()
    assert existing.id is not None
    return existing.id, False


def _strip_tz(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _dump_payload(payload: dict[str, Any]) -> str | None:
    if not payload:
        return None
    try:
        return json.dumps(payload, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return None


def _dump_tags(tags: list[str]) -> str | None:
    if not tags:
        return None
    # Normalize, dedupe (preserve order), drop empty.
    seen: dict[str, None] = {}
    for t in tags:
        norm = (t or "").strip().lower()
        if norm and norm not in seen:
            seen[norm] = None
    if not seen:
        return None
    return json.dumps(list(seen), sort_keys=False)


def _merge_tags(existing_json: str | None, new_tags: list[str]) -> list[str]:
    """Union of existing JSON-encoded tags + new list. Preserves prior order."""
    prior: list[str] = []
    if existing_json:
        try:
            loaded = json.loads(existing_json)
            if isinstance(loaded, list):
                prior = [str(t) for t in loaded if isinstance(t, str)]
        except (TypeError, ValueError):
            prior = []
    return prior + [t for t in new_tags if t not in prior]
