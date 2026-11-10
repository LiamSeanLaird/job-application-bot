"""JSearch, which searches Google for Jobs by role across all employers."""

from __future__ import annotations

import logging

import requests

from jobbot.config import JSEARCH_COUNTRY, JSEARCH_QUERIES
from jobbot.models import Job

log = logging.getLogger("jobbot")

URL = "https://api.openwebninja.com/jsearch/search-v2"
TIMEOUT_SECONDS = 45


def best_link(item: dict) -> str:
    """Prefer the employer's own site over a reposting board."""
    if item.get("job_apply_is_direct") and item.get("job_apply_link"):
        return item["job_apply_link"]
    for option in item.get("apply_options") or []:
        if option.get("is_direct") and option.get("apply_link"):
            return option["apply_link"]
    return item.get("job_apply_link") or item.get("job_google_link") or ""


def parse_job(item: dict) -> Job:
    return Job(
        source="jsearch",
        native_id=item["job_id"],
        title=item.get("job_title") or "",
        company=item.get("employer_name") or "",
        location=item.get("job_city") or (item.get("job_location") or "").split("•")[0].strip(),
        url=best_link(item),
        description=item.get("job_description") or "",
        remote=bool(item.get("job_is_remote")),
        posted_at=item.get("job_posted_at_datetime_utc") or "",
    )


def fetch_jsearch(session: requests.Session, api_key: str) -> list[Job]:
    jobs: dict[str, Job] = {}
    last_error: Exception | None = None
    for query in JSEARCH_QUERIES:
        try:
            response = session.get(
                URL,
                headers={"x-api-key": api_key},
                params={"query": query, "country": JSEARCH_COUNTRY, "date_posted": "3days", "num_pages": 1},
                timeout=TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            last_error = error
            log.warning("JSearch query failed: %s: %s", query, type(error).__name__)
            continue
        for item in response.json().get("data", {}).get("jobs") or []:
            job = parse_job(item)
            jobs[job.native_id] = job
    if not jobs and last_error is not None:
        raise last_error
    return list(jobs.values())
