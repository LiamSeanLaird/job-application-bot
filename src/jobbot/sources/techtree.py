"""TechTree (jobs.techtree.dev), a small curated job board.

It has no API or feed. The home page embeds every open job as a JavaScript object, so one request
lists them all. Only new jobs that pass the rules cost a second request, for the full description.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Callable

import requests

from jobbot import filters
from jobbot.models import Job, html_to_text
from jobbot.sources.careers import _remote_from_here

log = logging.getLogger("jobbot")

BASE_URL = "https://jobs.techtree.dev"
TIMEOUT_SECONDS = 25
PAUSE_BETWEEN_REQUESTS_SECONDS = 2
MAX_DETAIL_PAGES_PER_RUN = 10
# The client company is hidden on most jobs, and shown as this instead.
ANONYMOUS_ORGANIZATION = "TechTree's client"

JOB_START_RE = re.compile(r'\{id:"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",title:')
# A JavaScript string literal, with escapes.
JS_STRING = r'"((?:[^"\\]|\\.)*)"'


def _js_string(raw: str) -> str:
    """Decode a JavaScript string literal body. JSON handles the same escapes, except \\x."""
    raw = re.sub(r"\\x([0-9a-fA-F]{2})", lambda match: f"\\u00{match.group(1)}", raw)
    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return raw


def _field(block: str, name: str) -> str:
    match = re.search(rf"[{{,]{name}:{JS_STRING}", block)
    return _js_string(match.group(1)) if match else ""


def _job_blocks(page: str) -> list[tuple[str, str]]:
    """Each job's id and the text of its object, up to the next job."""
    starts = list(JOB_START_RE.finditer(page))
    return [
        (match.group(1), page[match.start(): starts[index + 1].start() if index + 1 < len(starts) else len(page)])
        for index, match in enumerate(starts)
    ]


def parse_job(native_id: str, block: str) -> Job:
    organization = _field(block, "organization")
    company = organization if organization and organization != ANONYMOUS_ORGANIZATION else ""
    if not company:
        # The company object comes first in the block, so its displayName is the first one.
        company = _field(block, "displayName")
    # Each location's own text, for example "Berlin, Germany" or "Europe (remote)".
    places = re.findall(rf'\{{id:"[0-9a-f-]{{36}}",location:{JS_STRING}', block)
    locations = list(dict.fromkeys(_js_string(place) for place in places)) or [_field(block, "location")]
    where = "; ".join(location for location in locations if location)
    workplace = _field(block, "workplaceType")
    posted = _field(block, "postedDateRaw")
    return Job(
        source="techtree",
        native_id=native_id,
        title=_field(block, "title"),
        company=company,
        location=where,
        url=f"{BASE_URL}/job/{native_id}",
        description=_field(block, "description"),
        remote=_remote_from_here(workplace == "Remote" or "remote" in where.lower(), where),
        posted_at=f"{posted}+00:00" if posted and "+" not in posted and not posted.endswith("Z") else posted,
        details={"workplace": workplace} if workplace else {},
    )


def parse_listing(page: str) -> list[Job]:
    return [parse_job(native_id, block) for native_id, block in _job_blocks(page)]


def parse_detail(page: str, native_id: str) -> str:
    """The full description from a job's own page, as plain text."""
    for found_id, block in _job_blocks(page):
        if found_id == native_id:
            return html_to_text(_field(block, "detailedDescription"))
    return ""


def _get(session: requests.Session, url: str) -> str:
    response = session.get(url, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.text


def fetch_techtree(session: requests.Session, is_known: Callable[[str], bool], max_age_hours: int | None = None) -> list[Job]:
    """Return the jobs that pass the rules, with full descriptions for the new ones."""
    jobs = [job for job in parse_listing(_get(session, BASE_URL + "/")) if job.title and filters.passes_rules(job, max_age_hours=max_age_hours)]
    detail_pages = 0
    for job in jobs:
        if is_known(job.job_id) or detail_pages >= MAX_DETAIL_PAGES_PER_RUN:
            continue
        time.sleep(PAUSE_BETWEEN_REQUESTS_SECONDS)
        detail_pages += 1
        try:
            job.description = parse_detail(_get(session, job.url), job.native_id) or job.description
        except requests.RequestException as error:
            if getattr(error.response, "status_code", None) in {403, 429}:
                log.warning("TechTree refused a job page; keeping short descriptions until the next run")
                break
            log.info("TechTree job page failed for %s: %s", job.native_id, error)
    log.info("TechTree: %d jobs passed rules, %d job pages read", len(jobs), detail_pages)
    return jobs
