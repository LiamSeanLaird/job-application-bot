"""The job tracker web app: review matches and record application progress.

It listens on localhost only. nginx in front of it provides HTTPS and the password prompt.
Posting text is untrusted, so everything is escaped and only https links are clickable.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from zoneinfo import ZoneInfo

from jobbot import pipeline
from jobbot.ai import AIError, OpenAIClient
from jobbot.config import APP_URL, DATABASE, TARGET_ROLE_FILE, TIMEZONE, WEB_PORT, load_settings
from jobbot.notify import posted_label, source_name
from jobbot.sources import manual, public
from jobbot.store import APPLICATION_STATUSES, TO_REVIEW, Store

log = logging.getLogger("jobbot")

MAX_BODY_BYTES = 200_000
MAX_NOTES_CHARS = 5_000
FILTERED_LIMIT = 200

NOTICES = {
    "added": ("ok", "Job added. Its details are open below."),
    "already": ("ok", "This job is already tracked. Its details are open below."),
    "invalid_url": ("error", "That doesn't look like a link. Paste the full https:// address."),
    "closed_site": ("error", "The bot doesn't open LinkedIn, Indeed, StepStone or XING pages. Paste the job text under the link and add it again."),
    "private_address": ("error", "That address isn't a public website, so the server won't open it."),
    "fetch_failed": ("error", "The page couldn't be loaded. Paste the job text under the link and add it again."),
    "not_a_job": ("error", "No job posting was found on that page. Paste the job text under the link and add it again."),
    "ai_failed": ("error", "The page couldn't be read because the AI service failed. Try again in a minute."),
}

# After these, the link stays in the box and the text box opens, so the owner only has to paste the text.
NEEDS_TEXT = {"closed_site", "fetch_failed", "not_a_job"}

# Each tab shows matches with these application statuses. "filtered" lists what the AI rejected.
VIEWS = {
    "review": ("To review", ["to review"]),
    "active": ("Applied", ["applied", "interviewing"]),
    "closed": ("Closed", ["rejected", "not interested"]),
    "filtered": ("Filtered out", []),
}

CSS = """
:root {
  --page: #f4f5f7; --card: #ffffff; --ink: #111827; --muted: #6b7280; --line: #e5e7eb; --hover: #f9fafb;
  --panel: #f9fafb; --accent: #111827; --accent-ink: #ffffff; --green-bg: #dcfce7; --green: #166534;
  --amber-bg: #fef3c7; --amber: #92400e; --grey-bg: #f3f4f6; --grey: #374151;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --page: #0f1115; --card: #181b21; --ink: #f3f4f6; --muted: #9ca3af; --line: #2a2f38; --hover: #1e2229;
    --panel: #14171c; --accent: #f3f4f6; --accent-ink: #111827; --green-bg: #12351f; --green: #86efac;
    --amber-bg: #3a2a0c; --amber: #fcd34d; --grey-bg: #262a32; --grey: #d1d5db;
  }
}
* { box-sizing: border-box; }
/* The phone layout makes table rows blocks, which would otherwise override the hidden attribute. */
[hidden] { display: none !important; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
main { max-width: 1240px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 22px; margin: 0 0 16px; }
nav { display: flex; gap: 6px; margin: 0 -16px 18px; padding: 0 16px; overflow-x: auto; scrollbar-width: none; }
nav::-webkit-scrollbar { display: none; }
nav a { flex: none; padding: 7px 12px; border-radius: 999px; text-decoration: none; color: var(--muted);
  border: 1px solid var(--line); font-size: 14px; white-space: nowrap; }
nav a[aria-current="page"] { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }

table { width: 100%; border-collapse: separate; border-spacing: 0; background: var(--card);
  border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }
th { text-align: left; font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .04em;
  color: var(--muted); padding: 10px 12px; border-bottom: 1px solid var(--line); white-space: nowrap; }
td { padding: 12px; vertical-align: top; border-top: 1px solid var(--line); }
tbody.job:first-of-type tr.row td { border-top: 0; }
tr.row { cursor: pointer; }
tr.row:hover td { background: var(--hover); }
tbody.job.open tr.row td { background: var(--hover); }
.toggle { width: 28px; height: 28px; padding: 0; border: 0; background: none; color: var(--muted);
  border-radius: 6px; display: grid; place-items: center; cursor: pointer; transition: transform .15s; }
.toggle svg { width: 16px; height: 16px; }
tbody.job.open .toggle { transform: rotate(90deg); }
.score { width: 40px; height: 32px; border-radius: 8px; display: grid; place-items: center; font-weight: 700; font-size: 15px; }
.high { background: var(--green-bg); color: var(--green); }
.mid { background: var(--amber-bg); color: var(--amber); }
.low { background: var(--grey-bg); color: var(--grey); }
.role { min-width: 220px; }
.title { font-weight: 600; color: var(--ink); text-decoration: none; overflow-wrap: anywhere; }
.title:hover { text-decoration: underline; }
.company { color: var(--muted); margin-top: 1px; }
.location { max-width: 200px; overflow-wrap: anywhere; }
.nowrap { white-space: nowrap; }
.muted { color: var(--muted); }
.mobile-meta { display: none; }
.has-notes { display: inline-block; margin-left: 6px; font-size: 11px; padding: 1px 6px; border-radius: 999px;
  background: var(--grey-bg); color: var(--grey); vertical-align: 1px; }

.actions { display: flex; gap: 6px; flex-wrap: wrap; margin: 0; }
textarea, button { font: inherit; color: var(--ink); background: var(--card); border: 1px solid var(--line); border-radius: 8px; }
.actions button { padding: 4px 10px; font-size: 13px; cursor: pointer; white-space: nowrap; }
button:hover { border-color: var(--muted); }
.actions button.current { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }
.actions button.undo { border-color: transparent; color: var(--muted); padding: 4px 4px; }
.reason { max-width: 340px; }
@media (min-width: 1100px) { .actions { flex-wrap: nowrap; } }

tr.detail td { padding: 0; border-top: 0; }
.panel { background: var(--panel); border-top: 1px dashed var(--line); padding: 16px 16px 18px 64px;
  display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 18px 28px; }
.panel h3 { margin: 0 0 6px; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
.panel ul { margin: 0; padding-left: 18px; }
.panel li { margin-bottom: 4px; }
.panel dl { margin: 0; display: grid; grid-template-columns: auto 1fr; gap: 4px 12px; }
.panel dt { color: var(--muted); }
.panel dd { margin: 0; overflow-wrap: anywhere; }
.panel a { color: var(--ink); }
.panel .wide { grid-column: 1 / -1; }
textarea { width: 100%; min-height: 72px; padding: 8px; resize: vertical; }
.notes-form button { margin-top: 8px; padding: 6px 12px; cursor: pointer; }
.empty { color: var(--muted); padding: 32px 0; text-align: center; }
details.saved summary { cursor: pointer; color: var(--muted); }
.posting { margin-top: 10px; max-height: 480px; overflow-y: auto; white-space: pre-wrap; overflow-wrap: anywhere;
  background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; font-size: 13px; line-height: 1.55; }
.add { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 12px; margin-bottom: 16px; }
.add-row { display: flex; gap: 8px; }
.add input[type=url] { flex: 1; min-width: 0; font: inherit; color: var(--ink); background: var(--page);
  border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; }
.add .primary { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); padding: 8px 14px;
  cursor: pointer; white-space: nowrap; font-weight: 600; }
.add .primary:disabled { opacity: .6; cursor: progress; }
.add details { margin-top: 8px; font-size: 13px; }
.add summary { cursor: pointer; color: var(--muted); }
.add textarea { margin-top: 8px; min-height: 140px; background: var(--page); }
.notice { border-radius: 10px; padding: 10px 12px; margin-bottom: 14px; font-size: 14px; border: 1px solid; }
.notice.ok { background: var(--green-bg); color: var(--green); border-color: transparent; }
.notice.error { background: var(--amber-bg); color: var(--amber); border-color: transparent; }
.warning { margin-top: 4px; font-size: 12px; color: var(--amber); }

@media (max-width: 760px) {
  table, tbody, tr, td { display: block; }
  thead { display: none; }
  table { border-radius: 12px; }
  tr.row { display: grid; grid-template-columns: 28px 44px 1fr; column-gap: 8px; padding: 12px; }
  tr.row td { border: 0; padding: 0; background: none !important; }
  tbody.job + tbody.job tr.row { border-top: 1px solid var(--line); }
  td.hide-mobile { display: none; }
  td.status, td.reason { grid-column: 2 / -1; margin-top: 10px; max-width: none; }
  .mobile-meta { display: block; color: var(--muted); font-size: 13px; margin-top: 4px; }
  .role { min-width: 0; }
  .panel { padding: 14px 12px 16px; grid-template-columns: 1fr; }
}
"""

# Expands a job's detail row. Clicking anywhere on the row works, except on links and buttons inside it.
SCRIPT = """
function toggle(job, open) {
  var isOpen = open === undefined ? !job.classList.contains("open") : open;
  job.classList.toggle("open", isOpen);
  job.querySelector("tr.detail").hidden = !isOpen;
  job.querySelector(".toggle").setAttribute("aria-expanded", String(isOpen));
}
document.querySelectorAll("tbody.job").forEach(function (job) {
  job.querySelector("tr.row").addEventListener("click", function (event) {
    if (event.target.closest("a, form")) { return; }
    toggle(job);
  });
});
var addForm = document.querySelector("form.add");
if (addForm) {
  addForm.addEventListener("submit", function () {
    var button = addForm.querySelector("button.primary");
    button.disabled = true;
    button.textContent = "Adding... this can take a minute";
  });
}
// After saving notes the server links back to "#<job>-detail", so that job opens again where it was.
if (location.hash.endsWith("-detail")) {
  var job = document.getElementById(location.hash.slice(1, -"-detail".length));
  if (job) { toggle(job, true); job.scrollIntoView({block: "start"}); }
}
"""


def _link(url: str) -> str | None:
    return escape(url, quote=True) if url.startswith("https://") else None


def _score_class(score: int) -> str:
    return "high" if score >= 80 else "mid" if score >= 65 else "low"


def _anchor(job_id: str) -> str:
    return "job-" + "".join(char if char.isalnum() else "-" for char in job_id)


def _date(iso: str) -> str:
    """'10 Nov 2026, 14:05' in the profile's timezone, or the raw value if it is only a date."""
    if not iso:
        return ""
    try:
        moment = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return escape(iso)
    if moment.tzinfo is None:
        return f"{moment:%-d %b %Y}"
    return f"{moment.astimezone(ZoneInfo(TIMEZONE)):%-d %b %Y, %H:%M}"


def _list(items: list[str], empty: str) -> str:
    if not items:
        return f'<p class="muted">{empty}</p>'
    return "<ul>" + "".join(f"<li>{escape(item)}</li>" for item in items) + "</ul>"


def _status_form(job: dict, view: str) -> str:
    # One button per action, so a single click saves. The current status is highlighted.
    current = job["application_status"]
    buttons = "".join(
        f'<button type="submit" name="status" value="{escape(status, quote=True)}"'
        f'{" class=current aria-pressed=true" if status == current else ""}>{escape(status.capitalize())}</button>'
        for status in APPLICATION_STATUSES if status != TO_REVIEW
    )
    if current != TO_REVIEW:
        buttons += f'<button type="submit" name="status" value="{TO_REVIEW}" class="undo">Back to review</button>'
    return (
        f'<form class="actions" method="post" action="/status" aria-label="Status">'
        f'<input type="hidden" name="job_id" value="{escape(job["job_id"], quote=True)}"><input type="hidden" name="view" value="{view}">'
        f"{buttons}</form>"
    )


def _panel(job: dict, view: str) -> str:
    link = _link(job["url"])
    others = [(href, escape(urlparse(url).netloc.removeprefix("www."))) for url in json.loads(job["other_links"]) if (href := _link(url))]
    links = [f'<a href="{link}" target="_blank" rel="noopener noreferrer">Job posting</a>'] if link else []
    links += [f'<a href="{href}" target="_blank" rel="noopener noreferrer">{label}</a>' for href, label in others]
    sections = []
    if view != "filtered" and job["filter_reason"]:
        sections.append(f'<section class="wide"><h3>Warning</h3><p>{escape(job["filter_reason"])}</p></section>')
    if view == "filtered":
        sections.append(f'<section class="wide"><h3>Why it was filtered out</h3><p>{escape(job["filter_reason"] or "")}</p></section>')
    sections += [
        f'<section><h3>Why it fits</h3>{_list(json.loads(job["reasons"]), "No reasons recorded.")}</section>',
        f'<section><h3>Gaps</h3>{_list(json.loads(job["gaps"]), "None noted.")}</section>',
        f'<section><h3>Unknowns</h3>{_list(json.loads(job["unknowns"]), "None noted.")}</section>',
        '<section><h3>Details</h3><dl>'
        f'<dt>Company</dt><dd>{escape(job["company"])}</dd>'
        f'<dt>Location</dt><dd>{escape(job["location"] or "Not stated")}</dd>'
        f'<dt>Source</dt><dd>{escape(source_name(job["source"]))}</dd>'
        f'<dt>Posted</dt><dd>{_date(job["posted_at"]) or "Unknown"}</dd>'
        f'<dt>First seen</dt><dd>{_date(job["first_seen_at"])}</dd>'
        + "".join(f"<dt>{escape(label)}</dt><dd>{escape(value)}</dd>" for label, value in json.loads(job["posting_details"] or "{}").items())
        + f'<dt>Links</dt><dd>{" &middot; ".join(links) or "None"}</dd>'
        "</dl></section>",
    ]
    if job["description"].strip():
        sections.append(
            f'<section class="wide"><h3>Saved posting</h3><details class="saved"><summary>Show the posting as saved on {_date(job["first_seen_at"])}</summary>'
            f'<div class="posting">{escape(job["description"])}</div></details></section>'
        )
    else:
        sections.append(
            '<section class="wide"><h3>Saved posting</h3>'
            '<p class="muted">This source gave only the title. Paste the posting text to keep a copy in case it is taken down.</p>'
            f'<form class="notes-form" method="post" action="/description">'
            f'<input type="hidden" name="job_id" value="{escape(job["job_id"], quote=True)}"><input type="hidden" name="view" value="{view}">'
            f'<textarea name="text" maxlength="{manual.MAX_DESCRIPTION_CHARS}" aria-label="Posting text" placeholder="Paste the job posting text"></textarea>'
            '<button type="submit">Save posting</button></form></section>'
        )
    if view != "filtered":
        sections.append(
            '<section class="wide"><h3>Notes</h3>'
            f'<form class="notes-form" method="post" action="/notes">'
            f'<input type="hidden" name="job_id" value="{escape(job["job_id"], quote=True)}"><input type="hidden" name="view" value="{view}">'
            f'<textarea name="notes" maxlength="{MAX_NOTES_CHARS}" aria-label="Notes" placeholder="Add notes">{escape(job["notes"] or "")}</textarea>'
            '<button type="submit">Save notes</button></form></section>'
        )
    return f'<div class="panel">{"".join(sections)}</div>'


def render_job(job: dict, view: str) -> str:
    score = job["fit_score"]
    link = _link(job["url"])
    title = escape(job["title"].strip())
    warning = f'<div class="warning">&#9888; {escape(job["filter_reason"])}</div>' if view != "filtered" and job["filter_reason"] else ""
    title_html = f'<a class="title" href="{link}" target="_blank" rel="noopener noreferrer">{title}</a>' if link else f'<span class="title">{title}</span>'
    notes_badge = '<span class="has-notes">Notes</span>' if job["notes"] else ""
    posted = posted_label(job["posted_at"], datetime.now(timezone.utc))
    source = source_name(job["source"])
    mobile_meta = " &middot; ".join(escape(part) for part in (job["location"], posted, source) if part)
    anchor = _anchor(job["job_id"])
    last = (
        f'<td class="reason">{escape(job["filter_reason"] or "")}</td>' if view == "filtered"
        else f'<td class="status">{_status_form(job, view)}</td>'
    )
    return (
        f'<tbody class="job" id="{anchor}">'
        f'<tr class="row">'
        f'<td><button type="button" class="toggle" aria-expanded="false" aria-controls="{anchor}-detail" aria-label="Show details"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 3.5 10.5 8 6 12.5"/></svg></button></td>'
        f'<td><div class="score {_score_class(score or 0)}" title="Fit score">{"&ndash;" if score is None else score}</div></td>'
        f'<td class="role">{title_html}{notes_badge}<div class="company">{escape(job["company"])}</div>{warning}<div class="mobile-meta">{mobile_meta}</div></td>'
        f'<td class="location hide-mobile">{escape(job["location"])}</td>'
        f'<td class="nowrap hide-mobile">{escape(posted) or "<span class=muted>Unknown</span>"}</td>'
        f'<td class="nowrap hide-mobile">{escape(source)}</td>'
        f"{last}</tr>"
        f'<tr class="detail" id="{anchor}-detail" hidden><td colspan="7">{_panel(job, view)}</td></tr>'
        "</tbody>"
    )


def render_page(store: Store, view: str, nonce: str, notice: str = "", pasted_url: str = "") -> str:
    counts = store.count_matches()
    tabs = []
    for key, (label, statuses) in VIEWS.items():
        count = f" ({sum(counts.get(status, 0) for status in statuses)})" if statuses else ""
        current = ' aria-current="page"' if key == view else ""
        tabs.append(f'<a href="/?view={key}"{current}>{escape(label)}{count}</a>')
    label, statuses = VIEWS[view]
    jobs = store.filtered_out(FILTERED_LIMIT) if view == "filtered" else store.matches(statuses)
    last_header = "Reason" if view == "filtered" else "Status"
    if jobs:
        body = (
            '<table><thead><tr><th aria-label="Expand"></th><th>Fit</th><th>Role</th><th>Location</th><th>Posted</th>'
            f'<th>Source</th><th>{last_header}</th></tr></thead>'
            + "".join(render_job(job, view) for job in jobs)
            + "</table>"
        )
    else:
        body = '<p class="empty">Nothing here yet.</p>'
    notice_html = ""
    if notice in NOTICES:
        kind, text = NOTICES[notice]
        notice_html = f'<p class="notice {kind}" role="status">{escape(text)}</p>'
    add_form = (
        '<form class="add" method="post" action="/add">'
        f'<div class="add-row"><input type="url" name="url" required value="{escape(pasted_url, quote=True)}" placeholder="Paste a link to any job posting" aria-label="Job link">'
        '<button type="submit" class="primary">Add job</button></div>'
        f"<details{' open' if notice in NEEDS_TEXT else ''}><summary>Paste the job text instead (needed for LinkedIn, Indeed, StepStone and XING)</summary>"
        f'<textarea name="text" maxlength="{manual.MAX_DESCRIPTION_CHARS}" aria-label="Job text" placeholder="Copy the whole job posting here"></textarea></details>'
        "</form>"
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex, nofollow">'
        f"<title>Job Tracker</title><style>{CSS}</style></head>"
        f'<body><main><h1>Job Tracker</h1>{add_form}{notice_html}<nav aria-label="Views">{"".join(tabs)}</nav>{body}</main>'
        f'<script nonce="{nonce}">{SCRIPT}</script></body></html>'
    )


def _default_ai() -> OpenAIClient:
    settings = load_settings()
    return OpenAIClient(settings.openai_key, settings.openai_model)


def _view_for(status: str) -> str:
    return next((key for key, (_, statuses) in VIEWS.items() if status in statuses), "review")


class Handler(BaseHTTPRequestHandler):
    database: Path | str = DATABASE
    allowed_origins: set[str] = {APP_URL}
    make_ai = staticmethod(_default_ai)
    make_session = staticmethod(public.make_session)

    def log_message(self, format, *args):  # Keep request logs out of the bot's log file.
        pass

    def _send(self, status: HTTPStatus, body: str = "", headers: dict | None = None, nonce: str = "") -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # "same-origin" keeps this address private from job sites, and still lets the browser send Origin on our own
        # form posts. "no-referrer" would make Chrome send "Origin: null", which the cross-site check rejects.
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header(
            "Content-Security-Policy",
            f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/healthz":
            self._send(HTTPStatus.OK, "ok")
            return
        if url.path != "/":
            self._send(HTTPStatus.NOT_FOUND, "Not found")
            return
        query = parse_qs(url.query)
        view = (query.get("view") or ["review"])[0]
        if view not in VIEWS:
            view = "review"
        nonce = secrets.token_urlsafe(16)
        notice = (query.get("notice") or [""])[0]
        pasted_url = (query.get("url") or [""])[0][:2000]
        self._send(HTTPStatus.OK, render_page(Store(self.database), view, nonce, notice, pasted_url), nonce=nonce)

    def do_POST(self) -> None:
        # Browsers send Origin on form posts. Checking it stops another site from submitting forms here.
        if self.headers.get("Origin") not in self.allowed_origins:
            self._send(HTTPStatus.FORBIDDEN, "Forbidden")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Too large")
            return
        form = {key: values[0] for key, values in parse_qs(self.rfile.read(length).decode("utf-8")).items()}
        job_id = form.get("job_id", "")
        view = form.get("view") if form.get("view") in VIEWS else "review"
        store = Store(self.database)
        if self.path == "/add":
            self._add_job(store, form)
            return
        try:
            if self.path == "/status":
                found = store.set_application_status(job_id, form.get("status", ""))
            elif self.path == "/notes":
                found = store.set_notes(job_id, form.get("notes", "")[:MAX_NOTES_CHARS])
            elif self.path == "/description":
                found = store.set_description(job_id, form.get("text", "").strip()[: manual.MAX_DESCRIPTION_CHARS])
            else:
                self._send(HTTPStatus.NOT_FOUND, "Not found")
                return
        except ValueError:
            self._send(HTTPStatus.BAD_REQUEST, "Unknown status")
            return
        if not found:
            self._send(HTTPStatus.NOT_FOUND, "Job not found")
            return
        # Stay on the same tab. A job whose status moved it to another tab simply disappears from this one.
        fragment = _anchor(job_id) + ("-detail" if self.path in ("/notes", "/description") else "")
        self._send(HTTPStatus.SEE_OTHER, headers={"Location": f"/?view={quote(view)}#{fragment}"})


    def _add_job(self, store: Store, form: dict) -> None:
        ai = self.make_ai()
        try:
            job = manual.build_job(form.get("url", ""), form.get("text", ""), self.make_session(), ai)
            job_id, already = pipeline.track_manual_job(job, store, ai, TARGET_ROLE_FILE.read_text(encoding="utf-8"))
        except manual.ManualJobError as error:
            url = quote(form.get("url", "")[:2000], safe="")
            self._send(HTTPStatus.SEE_OTHER, headers={"Location": f"/?view=review&notice={error.code}&url={url}"})
            return
        except AIError as error:
            log.warning("Could not read a pasted job: %s", error)
            self._send(HTTPStatus.SEE_OTHER, headers={"Location": "/?view=review&notice=ai_failed"})
            return
        view = _view_for(store.get(job_id)["application_status"])
        notice = "already" if already else "added"
        self._send(HTTPStatus.SEE_OTHER, headers={"Location": f"/?view={view}&notice={notice}#{_anchor(job_id)}-detail"})


def serve(port: int = WEB_PORT) -> None:
    Handler.allowed_origins = {APP_URL, f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    log.info("Web app listening on 127.0.0.1:%d", port)
    server.serve_forever()
