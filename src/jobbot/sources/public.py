"""Job sources that need no login: Arbeitnow and Berlin Startup Jobs."""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

from jobbot.config import MAX_JOB_AGE_HOURS
from jobbot.models import Job, html_to_text

log = logging.getLogger("jobbot")

TIMEOUT_SECONDS = 25
PAUSE_BETWEEN_REQUESTS_SECONDS = 1
USER_AGENT = "job-application-bot (personal job search)"

ARBEITNOW_URL = "https://www.arbeitnow.com/api/job-board-api"
ARBEITNOW_MAX_PAGES = 10
BERLIN_STARTUP_JOBS_FEED = "https://berlinstartupjobs.com/feed/"


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    return session


def parse_arbeitnow(item: dict) -> Job:
    return Job(
        source="arbeitnow",
        native_id=item["slug"],
        title=item["title"],
        company=item["company_name"],
        location=item.get("location") or "",
        url=item["url"],
        description=html_to_text(item.get("description") or ""),
        remote=bool(item.get("remote")),
        posted_at=datetime.fromtimestamp(item["created_at"], timezone.utc).isoformat(timespec="seconds"),
    )


def fetch_arbeitnow(session: requests.Session) -> list[Job]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=MAX_JOB_AGE_HOURS)).timestamp()
    jobs: list[Job] = []
    pages_without_recent_jobs = 0
    for page in range(1, ARBEITNOW_MAX_PAGES + 1):
        try:
            response = session.get(ARBEITNOW_URL, params={"page": page}, timeout=TIMEOUT_SECONDS)
            response.raise_for_status()
        except requests.RequestException as error:
            if not jobs:
                raise
            log.warning("Arbeitnow stopped at page %d, keeping earlier pages: %s", page, error)
            break
        items = response.json().get("data") or []
        jobs.extend(parse_arbeitnow(item) for item in items)
        # Pages are only roughly in date order, so one old page is not enough to stop.
        has_recent = any(item["created_at"] >= cutoff for item in items)
        pages_without_recent_jobs = 0 if has_recent else pages_without_recent_jobs + 1
        if not items or pages_without_recent_jobs == 2:
            break
        time.sleep(PAUSE_BETWEEN_REQUESTS_SECONDS)
    return jobs


def parse_berlin_startup_jobs(feed_xml: bytes) -> list[Job]:
    jobs = []
    for item in ET.fromstring(feed_xml).findall("./channel/item"):
        raw_title = item.findtext("title") or ""
        title, _, company = raw_title.rpartition("//")
        if not title:
            title, company = raw_title, ""
        guid = item.findtext("guid") or item.findtext("link") or raw_title
        published = item.findtext("pubDate")
        jobs.append(
            Job(
                source="berlinstartupjobs",
                native_id=guid.rsplit("=", 1)[-1],
                title=html_to_text(title).strip(),
                company=html_to_text(company).strip(),
                location="Berlin",
                url=item.findtext("link") or "",
                description=html_to_text(item.findtext("description") or ""),
                posted_at=parsedate_to_datetime(published).astimezone(timezone.utc).isoformat(timespec="seconds") if published else "",
            )
        )
    return jobs


def fetch_berlin_startup_jobs(session: requests.Session) -> list[Job]:
    response = session.get(BERLIN_STARTUP_JOBS_FEED, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    return parse_berlin_startup_jobs(response.content)
