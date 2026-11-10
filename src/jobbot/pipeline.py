"""One run: find jobs, remove duplicates, check fit and email the owner."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from jobbot import filters, notify
from jobbot.ai import AIError
from jobbot.config import (
    APP_URL,
    ARBEITNOW_MIN_MINUTES_BETWEEN_FETCHES,
    CAREERS_MIN_MINUTES_BETWEEN_FETCHES,
    JSEARCH_RUN_HOURS,
    LOCATIONS,
    MAX_AI_ASSESSMENTS_PER_RUN,
    REMOTE_REGIONS,
    SOURCE_FAILURES_BEFORE_WARNING,
)
from jobbot.models import Job, normalize_company, normalize_title, similarity
from jobbot.store import FILTERED_OUT, MATCHED, Store, utc_now

log = logging.getLogger("jobbot")

COMPANY_SIMILARITY = 0.85
MIN_COMPANY_PREFIX = 4
TITLE_SIMILARITY = 0.55
TITLE_NOISE_WORDS = {"m", "w", "f", "d", "x", "all", "genders", "in", "and", "the", "of", "for", "at", "remote", "hybrid"} | {
    word for place in LOCATIONS + REMOTE_REGIONS for word in place.split()
}
DESCRIPTION_OVERLAP = 0.4
MIN_DESCRIPTION_PHRASES = 40
MAX_DUPLICATE_CANDIDATES = 5
ALERT_SOURCES = {"linkedin", "indeed", "stepstone", "xing"}
MAX_CONSECUTIVE_AI_FAILURES = 3


@dataclass
class RunSummary:
    discovered: int = 0
    relevant: int = 0
    new: int = 0
    duplicates: int = 0
    assessed: int = 0
    matched: int = 0
    filtered_out: int = 0
    notified: int = 0
    source_errors: list[str] = field(default_factory=list)


def _fetch_is_due(store: Store, key: str, minutes: int, now: datetime) -> bool:
    last = store.get_meta(key)
    return not last or now - datetime.fromisoformat(last) >= timedelta(minutes=minutes)


def arbeitnow_is_due(store: Store, now: datetime) -> bool:
    return _fetch_is_due(store, "arbeitnow_last_fetch", ARBEITNOW_MIN_MINUTES_BETWEEN_FETCHES, now)


def careers_is_due(store: Store, now: datetime) -> bool:
    return _fetch_is_due(store, "careers_last_fetch", CAREERS_MIN_MINUTES_BETWEEN_FETCHES, now)


def jsearch_is_due(store: Store, now_local: datetime) -> bool:
    """True once per scheduled hour, and only for the latest slot that has passed today."""
    slots = [hour for hour in JSEARCH_RUN_HOURS if now_local.hour >= hour]
    if not slots:
        return False
    last = store.get_meta("jsearch_last_fetch")
    slot_start = now_local.replace(hour=max(slots), minute=0, second=0, microsecond=0)
    return not last or datetime.fromisoformat(last) < slot_start


def collect(sources: dict[str, Callable[[], list[Job]]], store: Store, gmail, summary: RunSummary) -> list[Job]:
    jobs: list[Job] = []
    for name, fetch in sources.items():
        key = f"failures:{name}"
        try:
            found = fetch()
        except Exception as error:  # A broken source must not stop the others.
            failures = int(store.get_meta(key) or 0) + 1
            store.set_meta(key, str(failures))
            summary.source_errors.append(name)
            log.warning("%s failed (%d in a row): %s", name, failures, error)
            if gmail and failures == SOURCE_FAILURES_BEFORE_WARNING:
                try:
                    gmail.send_to_owner(*notify.build_source_warning(name, failures, str(error)[:500]))
                except Exception as send_error:
                    log.error("Could not send source warning: %s", send_error)
            continue
        store.set_meta(key, "0")
        log.info("%s: %d jobs", name, len(found))
        jobs.extend(found)
    summary.discovered = len(jobs)
    return jobs


def record_relevant(jobs: list[Job], store: Store, summary: RunSummary, max_age_hours: int | None = None) -> None:
    for job in jobs:
        if not filters.passes_rules(job, max_age_hours=max_age_hours):
            if job.source in ALERT_SOURCES:
                log.info("Alert job rejected by rules: %s | %s | %s", job.title, job.company, job.posted_at)
            continue
        summary.relevant += 1
        result = store.add(job)
        summary.new += result == "new"
        summary.duplicates += result == "duplicate"


def companies_match(first: str, second: str) -> bool:
    """Compare normalised names, so 'Northwind' matches 'Northwind Trading GmbH'."""
    if not first or not second:
        return False
    shorter, longer = sorted((first, second), key=len)
    return similarity(first, second) >= COMPANY_SIMILARITY or (len(shorter) >= MIN_COMPANY_PREFIX and longer.startswith(shorter))


def title_score(first: str, second: str) -> float:
    """Combine character similarity with shared words, so added words or a new order still score highly."""
    words_first = set(first.split()) - TITLE_NOISE_WORDS
    words_second = set(second.split()) - TITLE_NOISE_WORDS
    if not words_first or not words_second:
        return similarity(first, second)
    shared = len(words_first & words_second) / min(len(words_first), len(words_second))
    return max(similarity(first, second), shared)


def description_overlap(first: str, second: str) -> float:
    """Share of five-word phrases in common, which finds copied postings under another company name."""
    def phrases(text: str) -> set[tuple[str, ...]]:
        words = normalize_title(text).split()
        return {tuple(words[index : index + 5]) for index in range(len(words) - 4)}

    first_phrases, second_phrases = phrases(first), phrases(second)
    if len(first_phrases) < MIN_DESCRIPTION_PHRASES or len(second_phrases) < MIN_DESCRIPTION_PHRASES:
        return 0.0
    return len(first_phrases & second_phrases) / min(len(first_phrases), len(second_phrases))


def find_duplicate(job: dict, store: Store, ai) -> str | None:
    company = normalize_company(job["company"])
    title = normalize_title(job["title"])
    candidates = []
    for other in store.primaries(excluding=job["job_id"]):
        titles = title_score(title, normalize_title(other["title"]))
        copied_text = description_overlap(job["description"], other["description"])
        same_company = companies_match(company, normalize_company(other["company"]))
        if (same_company and titles >= TITLE_SIMILARITY) or copied_text >= DESCRIPTION_OVERLAP:
            candidates.append((copied_text + titles, other))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    for _, other in candidates[:MAX_DUPLICATE_CANDIDATES]:
        if ai.same_role(job, other):
            return other["job_id"]
    return None


def assess_pending(store: Store, ai, target_role: str, summary: RunSummary) -> None:
    consecutive_failures = 0
    for job in store.pending(MAX_AI_ASSESSMENTS_PER_RUN):
        try:
            duplicate_of = find_duplicate(job, store, ai)
            if duplicate_of:
                store.mark_duplicate(job["job_id"], duplicate_of)
                summary.duplicates += 1
                continue
            assessment = ai.assess_job(job, target_role)
        except AIError as error:
            consecutive_failures += 1
            log.warning("AI check failed for %s: %s", job["job_id"], error)
            if consecutive_failures >= MAX_CONSECUTIVE_AI_FAILURES:
                log.error("Stopping AI checks after repeated failures; jobs stay queued")
                return
            continue
        consecutive_failures = 0
        reason = filters.hard_requirement_failure(assessment)
        if reason is None and not assessment["worth_applying"]:
            reason = assessment["summary"]
        store.save_assessment(job["job_id"], assessment, reason)
        summary.assessed += 1
        summary.matched += reason is None
        summary.filtered_out += reason is not None


def track_manual_job(job: Job, store: Store, ai, target_role: str) -> tuple[str, bool]:
    """Add a job the owner pasted. Returns its job ID and whether it was already tracked.

    A job already in the system, even under another link or as a repost, is reused rather than added twice.
    """
    existing = store.find_by_url(job.url)
    if existing is None:
        result = store.add(job)
        existing = store.get(job.job_id)["duplicate_of"] if result == "duplicate" else None
        if existing is None and result == "new":
            try:
                existing_match = find_duplicate(store.get(job.job_id), store, ai)
            except AIError:
                existing_match = None  # Tracking the job matters more than catching a repost.
            if existing_match:
                store.mark_duplicate(job.job_id, existing_match)
                existing = existing_match
    job_id = existing or job.job_id
    row = store.get(job_id)
    already_tracked = row["status"] == MATCHED
    if not already_tracked:
        if row["status"] == FILTERED_OUT:
            store.track(job_id, None, row["filter_reason"])
        else:
            try:
                assessment = ai.assess_job(row, target_role)
            except AIError as error:
                log.warning("AI check failed for pasted job %s: %s", job_id, error)
                store.track(job_id, None, "The AI check failed, so this job has no fit score.")
            else:
                warning = filters.hard_requirement_failure(assessment)
                if warning is None and not assessment["worth_applying"]:
                    warning = assessment["summary"]
                store.track(job_id, assessment, warning)
    return job_id, already_tracked


def send_digest(store: Store, gmail, summary: RunSummary) -> None:
    jobs = store.unnotified_matches()
    if not jobs:
        return
    gmail.send_to_owner(*notify.build_digest(jobs, APP_URL, summary.source_errors))
    store.mark_notified([job["job_id"] for job in jobs])
    summary.notified = len(jobs)


def run_once(sources, store: Store, ai, gmail, target_role: str, max_age_hours: int | None = None) -> RunSummary:
    summary = RunSummary()
    jobs = collect(sources, store, gmail, summary)
    record_relevant(jobs, store, summary, max_age_hours)
    assess_pending(store, ai, target_role, summary)
    send_digest(store, gmail, summary)
    store.set_meta("last_successful_run", utc_now())
    log.info("Run finished: %s", summary)
    return summary


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
