"""Command line entry point: `python -m jobbot auth`, `run`, or `run --dry-run`."""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from datetime import datetime
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

from jobbot import application_emails, notify, pipeline, web
from jobbot.ai import OpenAIClient
from jobbot.config import (
    APP_URL,
    ARBEITNOW_ENABLED,
    BERLIN_STARTUP_JOBS_ENABLED,
    DATA_DIR,
    DATABASE,
    GMAIL_ALERTS_ENABLED,
    TECHTREE_ENABLED,
    TRACK_APPLICATION_EMAILS,
    JSEARCH_QUERIES,
    LOG_FILE,
    RUN_HOURS,
    TIMEZONE,
    load_settings,
    load_target_roles,
)
from jobbot.google_api import Gmail, GoogleClient, authorize
from jobbot.sources import careers, gmail_alerts, jsearch, public, techtree
from jobbot.store import Store


def configure_logging() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3)]
    if sys.stdout.isatty():
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)


def is_run_hour(now: datetime | None = None) -> bool:
    return (now or datetime.now(ZoneInfo(TIMEZONE))).hour in RUN_HOURS


def public_sources(session, store: Store) -> dict:
    """Sources that need no keys, switched on in the profile."""
    sources = {}
    if BERLIN_STARTUP_JOBS_ENABLED:
        sources["berlinstartupjobs"] = lambda: public.fetch_berlin_startup_jobs(session)
    now = pipeline.now_utc()
    if ARBEITNOW_ENABLED and pipeline.arbeitnow_is_due(store, now):
        def fetch_arbeitnow():
            jobs = public.fetch_arbeitnow(session)
            store.set_meta("arbeitnow_last_fetch", now.isoformat())
            return jobs
        sources["arbeitnow"] = fetch_arbeitnow
    feeds = careers.load_feeds()
    if feeds and pipeline.careers_is_due(store, now):
        def fetch_careers():
            jobs = careers.fetch_careers(session, feeds)
            store.set_meta("careers_last_fetch", now.isoformat())
            return jobs
        sources["careers"] = fetch_careers
    if TECHTREE_ENABLED and pipeline.techtree_is_due(store, now):
        def fetch_techtree():
            jobs = techtree.fetch_techtree(session, lambda job_id: store.get(job_id) is not None)
            store.set_meta("techtree_last_fetch", now.isoformat())
            return jobs
        sources["techtree"] = fetch_techtree
    return sources


def dry_run() -> int:
    """Fetch public sources and show what would be checked. Uses no AI, Google or saved state."""
    store = Store(":memory:")
    summary = pipeline.RunSummary()
    jobs = pipeline.collect(public_sources(public.make_session(), store), store, None, summary)
    pipeline.record_relevant(jobs, store, summary)
    pending = store.pending(100_000)
    print(f"Discovered {summary.discovered}; passed rules {summary.relevant}; unique {summary.new}; exact duplicates {summary.duplicates}")
    print("Sources:", dict(Counter(job["source"] for job in pending)))
    print("Source errors:", summary.source_errors or "none")
    for job in pending:
        print(f"- [{job['source']}] {job['title']} | {job['company']} | {job['location']}")
    return 0


def run(force: bool, backfill_hours: int | None = None) -> int:
    if not force and not backfill_hours and not is_run_hour():
        return 0
    settings = load_settings()
    if not settings.openai_key:
        logging.error("OPENAI_KEY is empty in .env")
        return 1

    store = Store(DATABASE)
    google = GoogleClient()
    gmail = Gmail(google)
    ai = OpenAIClient(settings.openai_key, settings.openai_model)
    target_roles = load_target_roles()
    if backfill_hours:
        # One-off catch-up: only company feeds and TechTree, with a longer age limit. Known jobs are not checked or emailed again.
        feeds = careers.load_feeds()
        sources = {"careers": lambda: careers.fetch_careers(public.make_session(), feeds, backfill_hours)}
        if TECHTREE_ENABLED:
            known = lambda job_id: store.get(job_id) is not None
            sources["techtree"] = lambda: techtree.fetch_techtree(public.make_session(), known, backfill_hours)
        pipeline.run_once(sources, store, ai, gmail, target_roles, backfill_hours)
        ai.log_usage()
        return 0
    sources = public_sources(public.make_session(), store)
    if GMAIL_ALERTS_ENABLED:
        sources["gmail_alerts"] = lambda: gmail_alerts.fetch_alert_jobs(gmail, ai, store)
    now_local = datetime.now(ZoneInfo(TIMEZONE))
    if settings.jsearch_key and JSEARCH_QUERIES and pipeline.jsearch_is_due(store, now_local):
        def fetch_jsearch():
            found = jsearch.fetch_jsearch(public.make_session(), settings.jsearch_key)
            store.set_meta("jsearch_last_fetch", now_local.isoformat())
            return found
        sources["jsearch"] = fetch_jsearch
    pipeline.run_once(sources, store, ai, gmail, target_roles)
    if TRACK_APPLICATION_EMAILS:
        update_application_statuses(gmail, ai, store)
    ai.log_usage()
    return 0


def update_application_statuses(gmail, ai, store: Store) -> None:
    """Never stops the run: a failure here is logged and retried next time."""
    try:
        changes = application_emails.update_from_emails(gmail, ai, store)
        if changes:
            gmail.send_to_owner(*notify.build_status_update(changes, APP_URL))
    except Exception as error:
        logging.warning("Could not update statuses from emails: %s", error)


def main() -> int:
    parser = argparse.ArgumentParser(prog="jobbot")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("auth", help="Approve Gmail access in a browser.")
    commands.add_parser("web", help="Serve the job tracker web app on localhost.")
    run_parser = commands.add_parser("run", help="Run one search.")
    run_parser.add_argument("--dry-run", action="store_true", help="Show matches from public sources only.")
    run_parser.add_argument("--force", action="store_true", help="Run outside active hours.")
    run_parser.add_argument("--backfill-hours", type=int, help="One-off: read only company feeds and TechTree, keeping jobs up to this many hours old.")
    args = parser.parse_args()

    if args.command == "auth":
        authorize()
        return 0
    if args.command == "web":
        configure_logging()
        web.serve()
        return 0
    if args.dry_run:
        return dry_run()
    configure_logging()
    return run(args.force, args.backfill_hours)
