"""Move applied jobs to interviewing or rejected, from the replies employers send.

Only emails that mention a company the owner applied to, and arrived after they applied, are read.
The rest of the inbox is never sent to the AI. Statuses only move forward: applied can become
interviewing or rejected, and interviewing can become rejected.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from jobbot.ai import AIError
from jobbot.models import LEGAL_FORMS_RE
from jobbot.store import Store

log = logging.getLogger("jobbot")

TRACKED_STATUSES = ["applied", "interviewing"]
MAX_EMAILS_PER_RUN = 30
LOOKBACK_DAYS = 30
# The bot's own emails mention every company, so they are left out.
OWN_EMAILS = '-subject:"[Job Bot]"'
NEXT_STATUS = {
    ("applied", "interview"): "interviewing",
    ("applied", "rejection"): "rejected",
    ("interviewing", "rejection"): "rejected",
}
EXTRA_NAME_WORDS = {"global", "group", "holding", "deutschland", "germany", "berlin", "international"}


def search_name(company: str) -> str:
    """The name an email would use: 'Siemens Energy Global GmbH & Co. KG' becomes 'Siemens Energy'."""
    words = []
    for word in company.replace("&", " ").split():
        cleaned = word.strip(".,()")
        plain = cleaned.lower().replace(".", "")
        if cleaned and not LEGAL_FORMS_RE.fullmatch(plain) and plain not in EXTRA_NAME_WORDS:
            words.append(cleaned)
    # A second word counts only when it is capitalised as part of the brand, as in "Siemens Energy".
    # "imago stock" or "Kaufland e-commerce" become "imago" and "Kaufland", which is how emails name them.
    if len(words) > 1 and words[1][:1].isupper():
        return " ".join(words[:2])
    return words[0] if words else ""


def build_query(jobs: list[dict]) -> str:
    names = sorted({name for job in jobs if (name := search_name(job["company"]))})
    # The status date is when the owner marked the job applied, which can be after the application went out,
    # so the search reaches back further. The AI then confirms each email concerns that exact application.
    starts = [datetime.fromisoformat(min(filter(None, (job["status_changed_at"], job["first_seen_at"])))) for job in jobs]
    after = min(starts) - timedelta(days=LOOKBACK_DAYS)
    quoted = " OR ".join(f'"{name}"' for name in names)
    return f"after:{after:%Y/%m/%d} {OWN_EMAILS} ({quoted})"


def update_from_emails(gmail, ai, store: Store) -> list[dict]:
    """Read new employer emails and update statuses. Returns the changes made."""
    jobs = store.matches(TRACKED_STATUSES)
    if not jobs:
        return []
    message_ids = [message_id for message_id in gmail.search(build_query(jobs)) if not store.is_status_email_processed(message_id)]
    messages = [(message_id, gmail.read(message_id)) for message_id in message_ids[:MAX_EMAILS_PER_RUN]]
    # Oldest first, so a rejection that follows an interview invitation ends as rejected.
    messages.sort(key=lambda pair: pair[1]["received_at"])
    changes = []
    for message_id, message in messages:
        candidates = store.matches(TRACKED_STATUSES)
        try:
            result = ai.classify_application_email(message, candidates) if candidates else {"job_id": "none", "outcome": "other"}
        except AIError as error:
            log.warning("Could not classify email %s, will retry: %s", message_id, error)
            continue
        store.mark_status_email_processed(message_id)
        log.info("Application email %s: %s for %s", message_id, result["outcome"], result["job_id"])
        job = store.get(result["job_id"]) if result["job_id"] != "none" else None
        if job is None:
            continue
        new_status = NEXT_STATUS.get((job["application_status"], result["outcome"]))
        if new_status is None:
            continue
        received = message["received_at"].astimezone(timezone.utc)
        store.set_application_status(job["job_id"], new_status)
        note = f'{received:%d %b %Y}: moved to {new_status} after an email: "{message["subject"].strip()[:150]}"'
        store.set_notes(job["job_id"], "\n".join(part for part in (job["notes"], note) if part))
        changes.append({"job": job, "status": new_status, "subject": message["subject"], "received_at": received})
        log.info("Status of %s moved to %s from email %s", job["job_id"], new_status, message_id)
    return changes
