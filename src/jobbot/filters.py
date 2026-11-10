"""Rules that need no AI. Titles are matched broadly, and the AI check narrows them."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from jobbot.config import (
    EXCLUDED_COMPANY_NAMES,
    EXCLUDED_TITLE_PATTERNS,
    LANGUAGE_LIMIT,
    LOCATIONS,
    MAX_JOB_AGE_HOURS,
    MAX_ON_SITE_DAYS,
    MIN_SALARY,
    SALARY_CURRENCY,
    SOURCE_MAX_AGE_HOURS,
    TITLE_PATTERNS,
)
from jobbot.models import Job, normalize_company

# Title patterns come from the profile. They are broad on purpose, and the AI check narrows them.
TARGET_TITLE_RE = re.compile("|".join(f"(?:{pattern})" for pattern in TITLE_PATTERNS), re.IGNORECASE)
EXCLUDED_TITLE_RE = re.compile("|".join(f"(?:{pattern})" for pattern in EXCLUDED_TITLE_PATTERNS) or r"(?!x)x", re.IGNORECASE)
EXCLUDED_COMPANIES = {normalize_company(name) for name in EXCLUDED_COMPANY_NAMES}


def is_target_title(title: str) -> bool:
    return bool(TARGET_TITLE_RE.search(title)) and not EXCLUDED_TITLE_RE.search(title)


def is_in_search_area(job: Job) -> bool:
    """Remote, location not stated, or in one of the profile's locations."""
    location = job.location.lower()
    return job.remote or not location.strip() or any(place in location for place in LOCATIONS)


def is_excluded_company(company: str) -> bool:
    return normalize_company(company) in EXCLUDED_COMPANIES


def is_recent(job: Job, now: datetime | None = None, max_age_hours: int | None = None) -> bool:
    """True if posted within the age limit. A job with no known date is kept."""
    if not job.posted_at:
        return True
    posted = datetime.fromisoformat(job.posted_at.replace("Z", "+00:00"))
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    limit = max_age_hours or SOURCE_MAX_AGE_HOURS.get(job.source, MAX_JOB_AGE_HOURS)
    return (now or datetime.now(timezone.utc)) - posted <= timedelta(hours=limit)


def passes_rules(job: Job, now: datetime | None = None, max_age_hours: int | None = None) -> bool:
    return (
        is_target_title(job.title)
        and is_in_search_area(job)
        and not is_excluded_company(job.company)
        and is_recent(job, now, max_age_hours)
    )


def hard_requirement_failure(assessment: dict) -> str | None:
    """Enforce the profile's limits in code, using what the AI extracted from the posting."""
    if LANGUAGE_LIMIT and assessment.get("language_requirement") == "required":
        return f"Requires {LANGUAGE_LIMIT} above a basic level"
    on_site_days = assessment.get("on_site_days_per_week")
    if MAX_ON_SITE_DAYS is not None and on_site_days is not None and on_site_days > MAX_ON_SITE_DAYS:
        return f"Requires {on_site_days} days on site"
    salary = assessment.get("salary_max_per_year")
    if MIN_SALARY is not None and salary is not None and salary < MIN_SALARY:
        return f"Salary tops out at {SALARY_CURRENCY} {salary:,}"
    return None
