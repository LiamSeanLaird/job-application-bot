"""Public job feeds of the employers listed in the profile's companies file."""

from __future__ import annotations

import csv
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests

from jobbot import filters
from jobbot.config import COMPANIES_FILE, REMOTE_REGIONS
from jobbot.models import Job, html_to_text

log = logging.getLogger("jobbot")

TIMEOUT_SECONDS = 25
PAUSE_BETWEEN_REQUESTS_SECONDS = 1
SMARTRECRUITERS_PAGE_SIZE = 100
SMARTRECRUITERS_MAX_PAGES = 10

REMOTE_WORDS_RE = re.compile(r"\b(?:remote|anywhere|worldwide|global)\b|[^a-z]+", re.IGNORECASE)
# Regions from the profile. Short codes such as "eu" or "de" must match as whole words.
REACHABLE_REMOTE_RE = re.compile(
    "|".join(re.escape(region) if len(region) > 3 else rf"\b{re.escape(region)}\b" for region in REMOTE_REGIONS) or r"(?!x)x",
    re.IGNORECASE,
)


class RateLimited(Exception):
    """The feed's system asked us to slow down, so it is left alone until the next run."""


@dataclass(frozen=True)
class Feed:
    company: str
    ats: str
    slug: str


def _get(session: requests.Session, url: str, **kwargs) -> requests.Response:
    response = session.get(url, timeout=TIMEOUT_SECONDS, **kwargs)
    time.sleep(PAUSE_BETWEEN_REQUESTS_SECONDS)
    if response.status_code == 429:
        raise RateLimited(url)
    response.raise_for_status()
    return response


def _join(places) -> str:
    return "; ".join(dict.fromkeys(place.strip() for place in places if place and place.strip()))


def _remote_from_here(is_remote: bool, where: str) -> bool:
    """A remote job only counts if it is open to someone in the profile's remote regions."""
    return is_remote and (not REMOTE_WORDS_RE.sub("", where) or bool(REACHABLE_REMOTE_RE.search(where)))


def parse_greenhouse(item: dict, feed: Feed) -> Job:
    location = _join([(item.get("location") or {}).get("name")] + [office.get("location") or office.get("name") for office in item.get("offices") or []])
    return Job(
        source="greenhouse",
        native_id=f"{feed.slug}:{item['id']}",
        title=item["title"],
        company=feed.company,
        location=location,
        url=item["absolute_url"],
        description=html_to_text(item.get("content") or ""),
        remote=_remote_from_here("remote" in location.lower(), location),
        # updated_at changes on every edit, which would make an old job look new.
        posted_at=item.get("first_published") or item.get("updated_at") or "",
    )


def parse_ashby(item: dict, feed: Feed) -> Job:
    location = _join([item.get("location")] + [place.get("location") for place in item.get("secondaryLocations") or []])
    country = ((item.get("address") or {}).get("postalAddress") or {}).get("addressCountry") or ""
    return Job(
        source="ashby",
        native_id=f"{feed.slug}:{item['id']}",
        title=item["title"],
        company=feed.company,
        location=location,
        url=item["jobUrl"],
        description=item.get("descriptionPlain") or html_to_text(item.get("descriptionHtml") or ""),
        remote=_remote_from_here(bool(item.get("isRemote")) or item.get("workplaceType") == "Remote", f"{location} {country}"),
        posted_at=item.get("publishedAt") or "",
    )


def parse_lever(item: dict, feed: Feed) -> Job:
    categories = item.get("categories") or {}
    location = _join([categories.get("location")] + list(categories.get("allLocations") or []))
    lists = "\n".join(f"{part.get('text', '')}\n{html_to_text(part.get('content') or '')}" for part in item.get("lists") or [])
    return Job(
        source="lever",
        native_id=f"{feed.slug}:{item['id']}",
        title=item["text"],
        company=feed.company,
        location=location,
        url=item["hostedUrl"],
        description="\n".join(part for part in (item.get("descriptionPlain"), lists, item.get("additionalPlain")) if part),
        remote=_remote_from_here(item.get("workplaceType") == "remote", f"{location} {item.get('country') or ''}"),
        posted_at=datetime.fromtimestamp(item["createdAt"] / 1000, timezone.utc).isoformat(timespec="seconds"),
    )


def parse_recruitee(item: dict, feed: Feed) -> Job:
    location = _join([item.get("location")] + [place.get("city") for place in item.get("locations") or []])
    return Job(
        source="recruitee",
        native_id=f"{feed.slug}:{item['id']}",
        title=item["title"],
        company=feed.company,
        location=location,
        url=item["careers_url"],
        description=html_to_text(f"{item.get('description') or ''}<br>{item.get('requirements') or ''}"),
        remote=_remote_from_here(bool(item.get("remote")), f"{location} {item.get('country') or ''}"),
        posted_at=(item.get("published_at") or item.get("created_at") or "").replace(" UTC", "+00:00"),
    )


def parse_smartrecruiters(item: dict, feed: Feed) -> Job:
    place = item.get("location") or {}
    location = place.get("fullLocation") or place.get("city") or ""
    return Job(
        source="smartrecruiters",
        native_id=f"{feed.slug}:{item['id']}",
        title=item["name"],
        company=feed.company,
        location=location,
        url=f"https://jobs.smartrecruiters.com/{feed.slug}/{item['id']}",
        remote=_remote_from_here(bool(place.get("remote")), f"{location} {place.get('country') or ''}"),
        posted_at=item.get("releasedDate") or "",
    )


def parse_personio(feed_xml: bytes, feed: Feed) -> list[Job]:
    jobs = []
    for position in ET.fromstring(feed_xml).findall("position"):
        location = _join([position.findtext("office")] + [office.text for office in position.findall("additionalOffices/office")])
        sections = (f"{part.findtext('name') or ''}\n{html_to_text(part.findtext('value') or '')}" for part in position.findall("jobDescriptions/jobDescription"))
        native_id = position.findtext("id")
        jobs.append(
            Job(
                source="personio",
                native_id=f"{feed.slug}:{native_id}",
                title=position.findtext("name") or "",
                company=feed.company,
                location=location,
                url=f"https://{feed.slug}.jobs.personio.de/job/{native_id}",
                description="\n".join(sections),
                remote=_remote_from_here(bool(re.search(r"remote|home\s?office", location, re.IGNORECASE)), location),
                posted_at=position.findtext("createdAt") or "",
            )
        )
    return jobs


def _fetch_greenhouse(session, feed: Feed) -> list[Job]:
    data = _get(session, f"https://boards-api.greenhouse.io/v1/boards/{feed.slug}/jobs", params={"content": "true"}).json()
    return [parse_greenhouse(item, feed) for item in data.get("jobs") or []]


def _fetch_ashby(session, feed: Feed) -> list[Job]:
    data = _get(session, f"https://api.ashbyhq.com/posting-api/job-board/{feed.slug}").json()
    return [parse_ashby(item, feed) for item in data.get("jobs") or [] if item.get("isListed", True)]


def _lever_fetcher(host: str) -> Callable[[requests.Session, Feed], list[Job]]:
    def fetch(session, feed: Feed) -> list[Job]:
        data = _get(session, f"https://{host}/v0/postings/{feed.slug}", params={"mode": "json"}).json()
        return [parse_lever(item, feed) for item in data]
    return fetch


def _fetch_recruitee(session, feed: Feed) -> list[Job]:
    data = _get(session, f"https://{feed.slug}.recruitee.com/api/offers/").json()
    return [parse_recruitee(item, feed) for item in data.get("offers") or [] if item.get("status", "published") == "published"]


def _fetch_personio(session, feed: Feed) -> list[Job]:
    return parse_personio(_get(session, f"https://{feed.slug}.jobs.personio.de/xml").content, feed)


def _smartrecruiters_description(session, feed: Feed, job: Job) -> None:
    native_id = job.native_id.split(":", 1)[1]
    detail = _get(session, f"https://api.smartrecruiters.com/v1/companies/{feed.slug}/postings/{native_id}").json()
    sections = ((detail.get("jobAd") or {}).get("sections") or {}).values()
    job.description = "\n".join(html_to_text(section.get("text") or "") for section in sections)
    job.url = detail.get("postingUrl") or job.url


def _fetch_smartrecruiters(session, feed: Feed) -> list[Job]:
    """The list has no descriptions, so only jobs that pass the rules cost a second request."""
    url = f"https://api.smartrecruiters.com/v1/companies/{feed.slug}/postings"
    jobs: list[Job] = []
    for page in range(SMARTRECRUITERS_MAX_PAGES):
        offset = page * SMARTRECRUITERS_PAGE_SIZE
        data = _get(session, url, params={"limit": SMARTRECRUITERS_PAGE_SIZE, "offset": offset, "country": "de"}).json()
        jobs.extend(parse_smartrecruiters(item, feed) for item in data.get("content") or [])
        if offset + SMARTRECRUITERS_PAGE_SIZE >= (data.get("totalFound") or 0):
            break
    for job in jobs:
        if filters.passes_rules(job):
            _smartrecruiters_description(session, feed, job)
    return jobs


SYSTEMS: dict[str, Callable[[requests.Session, Feed], list[Job]]] = {
    "greenhouse": _fetch_greenhouse,
    "ashby": _fetch_ashby,
    "lever": _lever_fetcher("api.lever.co"),
    "lever_eu": _lever_fetcher("api.eu.lever.co"),
    "smartrecruiters": _fetch_smartrecruiters,
    "recruitee": _fetch_recruitee,
    "personio": _fetch_personio,
}


def load_feeds(path: Path | None = COMPANIES_FILE) -> list[Feed]:
    if path is None or not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [Feed(row["company"], row["ats"], row["slug"]) for row in csv.DictReader(handle) if row["ats"] in SYSTEMS and row["slug"]]


def fetch_careers(session: requests.Session, feeds: list[Feed], max_age_hours: int | None = None) -> list[Job]:
    """Return jobs that pass the rules. Descriptions of the rest are dropped to save memory."""
    jobs: list[Job] = []
    rate_limited: set[str] = set()
    read = failed = postings = 0
    for feed in feeds:
        if feed.ats in rate_limited:
            continue
        try:
            found = SYSTEMS[feed.ats](session, feed)
        except RateLimited:
            rate_limited.add(feed.ats)
            log.warning("%s asked us to slow down; skipping it until the next run", feed.ats)
            continue
        except Exception as error:  # A broken feed must not stop the others.
            failed += 1
            log.info("Careers feed failed for %s (%s): %s", feed.company, feed.ats, error)
            continue
        read += 1
        postings += len(found)
        jobs.extend(job for job in found if filters.passes_rules(job, max_age_hours=max_age_hours))
    log.info("Careers feeds: %d read, %d failed, %d postings, %d passed rules, rate limited: %s", read, failed, postings, len(jobs), sorted(rate_limited) or "none")
    if feeds and not read:
        raise RuntimeError(f"No careers feed could be read ({failed} failed, rate limited: {sorted(rate_limited) or 'none'})")
    return jobs
