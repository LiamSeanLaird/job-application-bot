"""Paths, the user's profile and fixed limits.

Everything specific to one person lives in their profile folder, which git ignores:
- `settings.toml` holds the search, limits and sources.
- One target role document per track (job category), describing the job they want. The AI reads them for every fit check
  and picks the track, and so the CV, that each job fits best.
Keys and server details live in `.env`. `profile.example/` shows both files filled in.
Set JOBBOT_PROFILE to use a profile folder somewhere else.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"
DATA_DIR = REPO_ROOT / "data"
SECRETS_DIR = REPO_ROOT / ".secrets"
DATABASE = DATA_DIR / "jobbot.db"
LOG_FILE = DATA_DIR / "jobbot.log"
GOOGLE_CLIENT_FILE = SECRETS_DIR / "google_client.json"
GOOGLE_TOKEN_FILE = SECRETS_DIR / "google_token.json"

PROFILE_DIR = Path(os.environ.get("JOBBOT_PROFILE") or REPO_ROOT / "profile")
SETTINGS_FILE = PROFILE_DIR / "settings.toml"

DEFAULT_OPENAI_MODEL = "gpt-6-luna"
WEB_PORT = 8090
MAX_AI_ASSESSMENTS_PER_RUN = 60
ARBEITNOW_MIN_MINUTES_BETWEEN_FETCHES = 55
CAREERS_MIN_MINUTES_BETWEEN_FETCHES = 55
TECHTREE_MIN_MINUTES_BETWEEN_FETCHES = 115
SOURCE_FAILURES_BEFORE_WARNING = 6
# JSearch only gives day-level dates, rounded to midnight, so it gets an extra day to still cover "2 days ago".
JSEARCH_EXTRA_AGE_HOURS = 24


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _read_env() -> dict[str, str]:
    return parse_env(ENV_FILE.read_text(encoding="utf-8")) if ENV_FILE.exists() else {}


def _load_profile() -> dict:
    if not SETTINGS_FILE.exists():
        raise SystemExit(
            f"No profile found at {PROFILE_DIR}.\n"
            "Copy the example to start: cp -r profile.example profile, then edit profile/settings.toml and profile/target_role.md."
        )
    with SETTINGS_FILE.open("rb") as handle:
        return tomllib.load(handle)


_profile = _load_profile()
_search, _limits, _sources = _profile.get("search", {}), _profile.get("limits", {}), _profile.get("sources", {})
_jsearch = _sources.get("jsearch", {})

# Notifications only ever go to this address.
OWNER_EMAIL: str = _profile["owner_email"]
TIMEZONE: str = _profile.get("timezone", "UTC")
# The hours of the day, in the profile's timezone, when a scheduled run goes ahead. The server tries every hour.
# Older profiles set a range with active_hours instead, which runs every hour inside it.
RUN_HOURS: list[int] = _profile.get("run_hours") or list(range(*_profile.get("active_hours", [0, 24])))

LOCATIONS: list[str] = [place.lower() for place in _search.get("locations", [])]
REMOTE_REGIONS: list[str] = [region.lower() for region in _search.get("remote_regions", [])]
MAX_JOB_AGE_HOURS: int = _search.get("max_job_age_hours", 48)
EXCLUDED_TITLE_PATTERNS: list[str] = _search.get("excluded_title_patterns", [])
EXCLUDED_COMPANY_NAMES: list[str] = _search.get("excluded_companies", [])

LANGUAGE_LIMIT: str = _limits.get("language", "")
MAX_ON_SITE_DAYS: int | None = _limits.get("max_on_site_days")
MIN_SALARY: int | None = _limits.get("min_salary")
SALARY_CURRENCY: str = _limits.get("salary_currency", "EUR")

ARBEITNOW_ENABLED: bool = _sources.get("arbeitnow", False)
BERLIN_STARTUP_JOBS_ENABLED: bool = _sources.get("berlin_startup_jobs", False)
GMAIL_ALERTS_ENABLED: bool = _sources.get("gmail_alerts", False)
TECHTREE_ENABLED: bool = _sources.get("techtree", False)
TRACK_APPLICATION_EMAILS: bool = _profile.get("track_application_emails", False)
COMPANIES_FILE: Path | None = REPO_ROOT / _sources["companies_file"] if _sources.get("companies_file") else None
JSEARCH_COUNTRY: str = _jsearch.get("country", "")
JSEARCH_RUN_HOURS: tuple[int, ...] = tuple(_jsearch.get("run_hours", []))
SOURCE_MAX_AGE_HOURS = {"jsearch": MAX_JOB_AGE_HOURS + JSEARCH_EXTRA_AGE_HOURS}



@dataclass(frozen=True)
class Track:
    """A job category, with its own titles, target role document, CV and optional salary floor."""

    key: str
    label: str
    target_role_file: Path
    title_patterns: tuple[str, ...]
    jsearch_queries: tuple[str, ...] = ()
    min_salary: int | None = None
    # Titles to skip for this track only, for example seniority levels that are too high.
    excluded_title_patterns: tuple[str, ...] = ()


def _tracks() -> list[Track]:
    # A profile without [[tracks]] is one track, using the older top-level settings.
    entries = _profile.get("tracks") or [{
        "key": "main",
        "label": "CV",
        "target_role": "target_role.md",
        "title_patterns": _search.get("title_patterns", []),
        "jsearch_queries": _jsearch.get("queries", []),
    }]
    return [
        Track(
            key=entry["key"],
            label=entry.get("label", entry["key"]),
            target_role_file=PROFILE_DIR / entry.get("target_role", "target_role.md"),
            title_patterns=tuple(entry["title_patterns"]),
            jsearch_queries=tuple(entry.get("jsearch_queries", [])),
            min_salary=entry.get("min_salary"),
            excluded_title_patterns=tuple(entry.get("excluded_title_patterns", [])),
        )
        for entry in entries
    ]


TRACKS: list[Track] = _tracks()
TRACKS_BY_KEY: dict[str, Track] = {track.key: track for track in TRACKS}
JSEARCH_QUERIES: list[str] = list(dict.fromkeys(query for track in TRACKS for query in track.jsearch_queries))


def load_target_roles() -> dict[str, str]:
    """Each track's target role document, read fresh so edits apply without a restart."""
    return {track.key: track.target_role_file.read_text(encoding="utf-8") for track in TRACKS}


_env = _read_env()
# The job tracker's public address. sslip.io turns a server IP into a free hostname that can get an HTTPS certificate.
APP_URL = f"https://{_env['APP_HOSTNAME']}" if _env.get("APP_HOSTNAME") else f"http://127.0.0.1:{WEB_PORT}"


@dataclass(frozen=True)
class Settings:
    openai_key: str
    openai_model: str
    jsearch_key: str = ""


def load_settings(env_file: Path = ENV_FILE) -> Settings:
    values = parse_env(env_file.read_text(encoding="utf-8")) if env_file.exists() else {}
    return Settings(
        openai_key=values.get("OPENAI_KEY", ""),
        openai_model=values.get("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL,
        jsearch_key=values.get("JSEARCH_API_KEY", ""),
    )
