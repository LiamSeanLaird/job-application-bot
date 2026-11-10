#!/usr/bin/env python3
"""Measure what JSearch adds for this job search. Uses one credit per query, and no AI or saved state."""

import json
import sys
from collections import Counter
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobbot import filters  # noqa: E402
from jobbot.config import DATA_DIR, ENV_FILE, parse_env  # noqa: E402
from jobbot.models import Job  # noqa: E402

CACHE = DATA_DIR / "jsearch_test.json"
URL ="https://api.openwebninja.com/jsearch/search-v2"
QUERIES = [
    "GTM Engineer in Berlin",
    "Go-to-Market Engineer in Berlin",
    "Revenue Operations in Berlin",
    "RevOps in Berlin",
    "AI Automation Engineer in Berlin",
    "CRM Automation in Berlin",
    "AI Enablement in Berlin",
    "Prozessautomatisierung KI in Berlin",
]


def to_job(item: dict) -> Job:
    return Job(
        source="jsearch",
        native_id=item["job_id"],
        title=item.get("job_title") or "",
        company=item.get("employer_name") or "",
        location=item.get("job_location") or item.get("job_city") or "",
        url=item.get("job_apply_link") or "",
        description=item.get("job_description") or "",
        remote=bool(item.get("job_is_remote")),
        posted_at=item.get("job_posted_at_datetime_utc") or "",
    )


def main() -> int:
    api_key = parse_env(ENV_FILE.read_text(encoding="utf-8")).get("JSEARCH_API_KEY", "")
    if not api_key:
        print("Add JSEARCH_API_KEY=your-key to .env first.")
        return 1

    if CACHE.exists() and "--fresh" not in sys.argv:
        items = json.loads(CACHE.read_text(encoding="utf-8"))
        print(f"Using saved results from {CACHE}. Pass --fresh to spend credits on a new search.")
        return report(items, credits_used=0)

    items: dict[str, dict] = {}
    for query in QUERIES:
        response = requests.get(
            URL,
            headers={"x-api-key": api_key},
            params={"query": query, "country": "de", "date_posted": "3days", "num_pages": 1},
            timeout=45,
        )
        quota = {k: v for k, v in response.headers.items() if "limit" in k.lower() or "quota" in k.lower()}
        if response.status_code != 200:
            print(f"{query}: HTTP {response.status_code} {response.text[:300]}")
            continue
        found = response.json().get("data", {}).get("jobs") or []
        print(f"{query}: {len(found)} jobs {quota}")
        items.update({item["job_id"]: item for item in found})

    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(items), encoding="utf-8")
    return report(items, credits_used=len(QUERIES))


def report(items: dict[str, dict], credits_used: int) -> int:
    jobs = [(to_job(item), item) for item in items.values()]
    passing = [(job, item) for job, item in jobs if filters.passes_rules(job)]
    undated = sum(not job.posted_at for job, _ in jobs)

    def publishers(pairs):
        return Counter(item.get("job_publisher") or "unknown" for _, item in pairs).most_common()

    apply_sites = Counter(
        option.get("publisher") or "unknown" for _, item in jobs for option in item.get("apply_options") or []
    )
    print(f"\nCredits used: {credits_used}")
    print(f"Unique jobs: {len(jobs)}; without a posting date: {undated}")
    print("Publishers, all:", publishers(jobs))
    print("Sites offering an apply link:", apply_sites.most_common())
    print("\nAll jobs returned:")
    for job, item in jobs:
        print(f"- {item.get('job_posted_at')} | {job.posted_at[:16]} | {item.get('job_publisher')} | {job.title} | {job.company} | {job.location}")
    print()
    print(f"Passed our rules, including 48 hours: {len(passing)}")
    print("Publishers, passed:", publishers(passing))
    print("\nJobs that passed:")
    for job, item in sorted(passing, key=lambda pair: pair[0].posted_at, reverse=True):
        print(f"- {job.posted_at[:16]} | {item.get('job_publisher')} | {job.title} | {job.company} | {job.location}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
