"""SQLite storage: one row per job, with duplicates pointing at a primary job."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from jobbot.models import Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    location TEXT NOT NULL,
    url TEXT NOT NULL,
    other_links TEXT NOT NULL DEFAULT '[]',
    description TEXT NOT NULL DEFAULT '',
    remote INTEGER NOT NULL DEFAULT 0,
    posted_at TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    status TEXT NOT NULL,
    duplicate_of TEXT,
    filter_reason TEXT,
    fit_score INTEGER,
    reasons TEXT NOT NULL DEFAULT '[]',
    gaps TEXT NOT NULL DEFAULT '[]',
    unknowns TEXT NOT NULL DEFAULT '[]',
    assessed_at TEXT,
    sheet_synced_at TEXT,
    notified_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_dedupe_key ON jobs(dedupe_key);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gmail_messages (message_id TEXT PRIMARY KEY, processed_at TEXT NOT NULL);
"""

NEW = "new"
MATCHED = "matched"
FILTERED_OUT = "filtered_out"
DUPLICATE = "duplicate"

# the owner's own progress on a matched job, set in the web app.
TO_REVIEW = "to review"
APPLICATION_STATUSES = [TO_REVIEW, "applied", "interviewing", "rejected", "not interested"]
# Columns added after the first deployment, created on existing databases at startup.
ADDED_COLUMNS = {
    "application_status": "TEXT NOT NULL DEFAULT 'to review'",
    "notes": "TEXT NOT NULL DEFAULT ''",
    "status_changed_at": "TEXT",
    "posting_details": "TEXT NOT NULL DEFAULT '{}'",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # The cron job and the web app write to the same file, so wait for a lock instead of failing.
        self.connection = sqlite3.connect(str(path), timeout=30)
        self.connection.row_factory = sqlite3.Row
        if path != ":memory:":
            self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(SCHEMA)
        existing = {row["name"] for row in self.connection.execute("PRAGMA table_info(jobs)")}
        for column, definition in ADDED_COLUMNS.items():
            if column not in existing:
                self.connection.execute(f"ALTER TABLE jobs ADD COLUMN {column} {definition}")
        self.connection.commit()

    def get(self, job_id: str) -> dict | None:
        row = self.connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def add(self, job: Job) -> str:
        """Record a discovered job. Returns 'new', 'duplicate' or 'seen'."""
        existing = self.get(job.job_id)
        if existing:
            if job.description and not existing["description"]:
                self.connection.execute(
                    "UPDATE jobs SET description = ? WHERE job_id = ?", (job.description, job.job_id)
                )
                self.connection.commit()
            return "seen"

        primary = self.connection.execute(
            "SELECT job_id FROM jobs WHERE dedupe_key = ? AND status != ? ORDER BY first_seen_at LIMIT 1",
            (job.dedupe_key, DUPLICATE),
        ).fetchone()
        duplicate_of = primary["job_id"] if primary else None
        status = DUPLICATE if duplicate_of else NEW
        self.connection.execute(
            """INSERT INTO jobs (job_id, dedupe_key, source, title, company, location, url, description,
                                 remote, posted_at, first_seen_at, status, duplicate_of, posting_details)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job.job_id, job.dedupe_key, job.source, job.title, job.company, job.location, job.url,
                job.description, int(job.remote), job.posted_at, utc_now(), status, duplicate_of, json.dumps(job.details),
            ),
        )
        self.connection.commit()
        if duplicate_of:
            self.add_link(duplicate_of, job.url)
        return status

    def add_link(self, job_id: str, url: str) -> None:
        job = self.get(job_id)
        if not job or not url or url == job["url"]:
            return
        links = json.loads(job["other_links"])
        if url not in links:
            links.append(url)
            self.connection.execute(
                "UPDATE jobs SET other_links = ? WHERE job_id = ?", (json.dumps(links), job_id)
            )
            self.connection.commit()

    def pending(self, limit: int) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY first_seen_at, job_id LIMIT ?", (NEW, limit)
        ).fetchall()
        return [dict(row) for row in rows]

    def primaries(self, excluding: str) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM jobs WHERE status != ? AND job_id != ?", (DUPLICATE, excluding)
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_duplicate(self, job_id: str, primary_id: str) -> None:
        job = self.get(job_id)
        self.connection.execute(
            "UPDATE jobs SET status = ?, duplicate_of = ? WHERE job_id = ?", (DUPLICATE, primary_id, job_id)
        )
        self.connection.commit()
        if job:
            self.add_link(primary_id, job["url"])

    def save_assessment(self, job_id: str, assessment: dict, filter_reason: str | None) -> None:
        self.connection.execute(
            """UPDATE jobs SET status = ?, filter_reason = ?, fit_score = ?, reasons = ?, gaps = ?,
                               unknowns = ?, assessed_at = ? WHERE job_id = ?""",
            (
                FILTERED_OUT if filter_reason else MATCHED,
                filter_reason,
                assessment.get("fit_score"),
                json.dumps(assessment.get("reasons") or []),
                json.dumps(assessment.get("gaps") or []),
                json.dumps(assessment.get("unknowns") or []),
                utc_now(),
                job_id,
            ),
        )
        self.connection.commit()

    def find_by_url(self, url: str) -> str | None:
        """The primary job with this link, as its main link or one of its other links."""
        row = self.connection.execute(
            "SELECT job_id, duplicate_of FROM jobs WHERE url = ? OR instr(other_links, ?) > 0 ORDER BY first_seen_at LIMIT 1",
            (url, json.dumps(url)),
        ).fetchone()
        if not row:
            return None
        return row["duplicate_of"] or row["job_id"]

    def track(self, job_id: str, assessment: dict | None, warning: str | None) -> None:
        """Put a job the owner chose into To review, whatever the AI check said. Its concerns are kept as a warning."""
        if assessment is not None:
            self.save_assessment(job_id, assessment, None)
        self.connection.execute(
            """UPDATE jobs SET status = ?, filter_reason = ?, notified_at = COALESCE(notified_at, ?) WHERE job_id = ?""",
            (MATCHED, warning, utc_now(), job_id),
        )
        self.connection.commit()

    def matches(self, application_statuses: list[str]) -> list[dict]:
        marks = ",".join("?" * len(application_statuses))
        rows = self.connection.execute(
            f"SELECT * FROM jobs WHERE status = ? AND application_status IN ({marks}) ORDER BY first_seen_at DESC, fit_score DESC",
            (MATCHED, *application_statuses),
        ).fetchall()
        return [dict(row) for row in rows]

    def filtered_out(self, limit: int) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY first_seen_at DESC LIMIT ?", (FILTERED_OUT, limit)
        ).fetchall()
        return [dict(row) for row in rows]

    def count_matches(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT application_status, COUNT(*) AS total FROM jobs WHERE status = ? GROUP BY application_status", (MATCHED,)
        )
        return {row["application_status"]: row["total"] for row in rows}

    def set_application_status(self, job_id: str, status: str) -> bool:
        if status not in APPLICATION_STATUSES:
            raise ValueError(f"Unknown status: {status}")
        changed = self.connection.execute(
            "UPDATE jobs SET application_status = ?, status_changed_at = ? WHERE job_id = ? AND status = ?",
            (status, utc_now(), job_id, MATCHED),
        ).rowcount
        self.connection.commit()
        return bool(changed)

    def set_description(self, job_id: str, text: str) -> bool:
        """Save posting text the owner pasted, for jobs whose source gave only a title."""
        changed = self.connection.execute("UPDATE jobs SET description = ? WHERE job_id = ?", (text, job_id)).rowcount
        self.connection.commit()
        return bool(changed)

    def set_notes(self, job_id: str, notes: str) -> bool:
        changed = self.connection.execute("UPDATE jobs SET notes = ? WHERE job_id = ?", (notes, job_id)).rowcount
        self.connection.commit()
        return bool(changed)

    def unnotified_matches(self) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM jobs WHERE status = ? AND notified_at IS NULL ORDER BY fit_score DESC, first_seen_at",
            (MATCHED,),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_notified(self, job_ids: list[str]) -> None:
        now = utc_now()
        self.connection.executemany(
            "UPDATE jobs SET notified_at = ? WHERE job_id = ?", [(now, job_id) for job_id in job_ids]
        )
        self.connection.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.connection.commit()

    def is_message_processed(self, message_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM gmail_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None

    def mark_message_processed(self, message_id: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO gmail_messages (message_id, processed_at) VALUES (?, ?)", (message_id, utc_now())
        )
        self.connection.commit()
