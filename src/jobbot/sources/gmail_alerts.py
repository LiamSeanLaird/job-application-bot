"""Turn job alert emails from closed platforms into jobs. These sites are never visited directly."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from urllib.parse import urlparse

from jobbot.models import Job

ALERT_QUERY = (
    "newer_than:2d (from:linkedin.com OR from:indeed.com OR from:stepstone.de OR from:xing.com) "
    "(job OR jobs OR stelle OR stellen)"
)
SOURCE_BY_DOMAIN = {"linkedin.com": "linkedin", "indeed.com": "indeed", "stepstone.de": "stepstone", "xing.com": "xing"}
LINKEDIN_JOB_RE = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?\s]*-)?(\d+)")
INDEED_JOB_RE = re.compile(r"[?&](?:jk|vjk)=([0-9a-fA-F]+)")


def source_from_sender(sender: str) -> str | None:
    for domain, source in SOURCE_BY_DOMAIN.items():
        if re.search(rf"[@.]{re.escape(domain)}\b", sender):
            return source
    return None


def native_id_and_url(source: str, url: str) -> tuple[str, str]:
    if source == "linkedin" and (match := LINKEDIN_JOB_RE.search(url)):
        return match.group(1), f"https://www.linkedin.com/jobs/view/{match.group(1)}"
    if source == "indeed" and (match := INDEED_JOB_RE.search(url)):
        return match.group(1), f"https://de.indeed.com/viewjob?jk={match.group(1)}"
    parsed = urlparse(url)
    stable = f"{parsed.netloc}{parsed.path}"
    return hashlib.sha256(stable.encode()).hexdigest()[:20], url


def jobs_from_alert(message: dict, extracted: list[dict]) -> list[Job]:
    """Build jobs from the AI's extraction, keeping only URLs that are really in the email."""
    source = source_from_sender(message["from"])
    if source is None:
        return []
    received_at = message.get("received_at")
    jobs = []
    for item in extracted:
        url = item["url"].strip()
        if not url.startswith("https://") or url not in message["text"] or not item["title"].strip():
            continue
        native_id, canonical_url = native_id_and_url(source, url)
        jobs.append(
            Job(
                source=source,
                native_id=native_id,
                title=item["title"].strip(),
                company=item["company"].strip(),
                location=item["location"].strip(),
                url=canonical_url,
                posted_at=_posted_at(received_at, item.get("days_since_posted")),
            )
        )
    return jobs


def _posted_at(received_at: datetime | None, days_since_posted: int | None) -> str:
    """Use the age stated in the email when there is one, otherwise the time the email arrived."""
    if received_at is None:
        return ""
    return (received_at - timedelta(days=days_since_posted or 0)).isoformat(timespec="seconds")


def fetch_alert_jobs(gmail, ai, store) -> list[Job]:
    jobs: list[Job] = []
    for message_id in gmail.search(ALERT_QUERY):
        if store.is_message_processed(message_id):
            continue
        message = gmail.read(message_id)
        if source_from_sender(message["from"]) is not None:
            jobs.extend(jobs_from_alert(message, ai.extract_alert_jobs(message["text"])))
        store.mark_message_processed(message_id)
    return jobs
