"""Build the notification email. Posting text is escaped, and only https links become clickable.

Email clients ignore most CSS, so the layout uses tables and inline styles.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from html import escape
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from jobbot.config import TIMEZONE

SOURCE_NAMES = {
    "arbeitnow": "Arbeitnow",
    "berlinstartupjobs": "Berlin Startup Jobs",
    "jsearch": "Google Jobs",
    "linkedin": "LinkedIn",
    "indeed": "Indeed",
    "stepstone": "StepStone",
    "xing": "XING",
    "greenhouse": "Company site",
    "ashby": "Company site",
    "lever": "Company site",
    "smartrecruiters": "Company site",
    "recruitee": "Company site",
    "personio": "Company site",
    "manual": "Added by you",
}

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
INK, MUTED, LINE, PAGE = "#111827", "#6b7280", "#e5e7eb", "#f3f4f6"


def _safe_link(url: str) -> str | None:
    return escape(url, quote=True) if url.startswith("https://") else None


def source_name(source: str) -> str:
    return SOURCE_NAMES.get(source, source)


def posted_label(posted_at: str, now: datetime) -> str:
    """'Today', 'Yesterday' or '3 days ago', with the date, in the profile's timezone. Empty if unknown."""
    if not posted_at:
        return ""
    try:
        posted = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    local_zone = ZoneInfo(TIMEZONE)
    days = (now.astimezone(local_zone).date() - posted.astimezone(local_zone).date()).days
    relative = "Today" if days <= 0 else "Yesterday" if days == 1 else f"{days} days ago"
    return f"{relative} ({posted.astimezone(local_zone):%-d %b})"


def _score_colors(score: int) -> tuple[str, str]:
    if score >= 80:
        return "#dcfce7", "#166534"
    if score >= 65:
        return "#fef3c7", "#92400e"
    return "#f3f4f6", "#374151"


def _other_sites(job: dict) -> list[tuple[str, str]]:
    """Links to the same job on other sites, labelled by their domain."""
    sites = []
    for url in json.loads(job["other_links"]):
        if link := _safe_link(url):
            sites.append((link, escape(urlparse(url).netloc.removeprefix("www."))))
    return sites


def _card(job: dict, now: datetime) -> str:
    score = int(job["fit_score"] or 0)
    background, foreground = _score_colors(score)
    link = _safe_link(job["url"])
    title = escape(job["title"].strip())
    title_html = f'<a href="{link}" style="color:{INK};text-decoration:none">{title}</a>' if link else title
    details = [escape(part) for part in (job["location"], source_name(job["source"]), posted_label(job["posted_at"], now)) if part]
    others = _other_sites(job)
    also = (
        f'<div style="margin-top:6px;font-size:12px;color:{MUTED}">Also on '
        + ", ".join(f'<a href="{href}" style="color:{MUTED}">{label}</a>' for href, label in others)
        + "</div>"
    ) if others else ""
    return (
        f'<tr><td style="padding:0 0 12px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:#ffffff;border:1px solid {LINE};border-radius:12px">'
        f'<tr><td width="64" valign="top" style="padding:16px 0 16px 16px">'
        f'<div style="width:48px;height:48px;line-height:48px;border-radius:10px;text-align:center;'
        f'background:{background};color:{foreground};font-size:18px;font-weight:700">{score}</div></td>'
        f'<td valign="top" style="padding:16px">'
        f'<div style="font-size:16px;font-weight:600;line-height:1.3">{title_html}</div>'
        f'<div style="margin-top:2px;font-size:14px;color:{INK}">{escape(job["company"])}</div>'
        f'<div style="margin-top:6px;font-size:13px;color:{MUTED}">{" &middot; ".join(details)}</div>'
        f"{also}</td></tr></table></td></tr>"
    )


def build_digest(jobs: list[dict], app_url: str | None, source_errors: list[str], now: datetime | None = None) -> tuple[str, str, str]:
    now = now or datetime.now(timezone.utc)
    count = len(jobs)
    subject = f"[Job Bot] {count} new job{'' if count == 1 else 's'} to review"

    text_parts = []
    for job in jobs:
        details = " | ".join(part for part in (job["location"], source_name(job["source"]), posted_label(job["posted_at"], now)) if part)
        text_parts.append(f"{job['fit_score']}  {job['title'].strip()} - {job['company']}\n    {details}\n    {job['url']}")

    footer = []
    if app_url:
        text_parts.append(f"Job tracker: {app_url}")
        footer.append(
            f'<a href="{escape(app_url, quote=True)}" style="display:inline-block;padding:10px 16px;border-radius:8px;'
            f'background:{INK};color:#ffffff;text-decoration:none;font-size:14px;font-weight:600">Open job tracker</a>'
        )
    if source_errors:
        note = "Some sources failed on this run: " + ", ".join(source_errors)
        text_parts.append(note)
        footer.append(f'<div style="margin-top:12px;font-size:12px;color:#b91c1c">{escape(note)}</div>')

    heading = f"{count} new job{'' if count == 1 else 's'}"
    html = (
        f'<div style="margin:0;padding:24px 12px;background:{PAGE};font-family:{FONT}">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;margin:0 auto">'
        f'<tr><td style="padding:0 4px 16px;font-size:20px;font-weight:700;color:{INK}">{heading}'
        f'<span style="font-size:13px;font-weight:400;color:{MUTED}"> &middot; sorted by fit score</span></td></tr>'
        + "".join(_card(job, now) for job in jobs)
        + (f'<tr><td style="padding:8px 4px 0">{"".join(footer)}</td></tr>' if footer else "")
        + "</table></div>"
    )
    return subject, "\n\n".join(text_parts), html


def build_source_warning(source: str, failures: int, error: str) -> tuple[str, str, str]:
    subject = f"[Job Bot] {source} has failed {failures} times in a row"
    body = f"{source} has failed {failures} runs in a row. You may be missing jobs from it.\n\nLast error: {error}"
    return subject, body, f"<pre>{escape(body)}</pre>"
