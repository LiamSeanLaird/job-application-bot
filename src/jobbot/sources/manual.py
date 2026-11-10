"""Jobs the owner adds himself by pasting a link, or the job text for sites the bot must not visit.

The server fetches a page only when the address is public, so a pasted link cannot reach the server itself,
its private network or the cloud provider's internal services.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
from urllib.parse import urljoin, urlparse

import requests

from jobbot.models import Job, html_to_text

TIMEOUT_SECONDS = 20
MAX_PAGE_BYTES = 3_000_000
MAX_REDIRECTS = 5
MAX_DESCRIPTION_CHARS = 30_000
# Sites whose terms prohibit automated access. For these, the owner pastes the job text instead.
CLOSED_SITES = ("linkedin.com", "indeed.com", "indeed.de", "stepstone.de", "stepstone.com", "xing.com", "glassdoor.com", "glassdoor.de")
JSON_LD_RE = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)


class ManualJobError(Exception):
    """A problem the owner can act on. `code` selects the message shown in the app."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def is_closed_site(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(host == site or host.endswith("." + site) for site in CLOSED_SITES)


def _is_public(host: str) -> bool:
    try:
        addresses = {info[4][0].split("%")[0] for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)}
    except (socket.gaierror, UnicodeError):
        raise ManualJobError("fetch_failed")
    return bool(addresses) and all(ipaddress.ip_address(address).is_global for address in addresses)


def check_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise ManualJobError("invalid_url")
    return parsed.geturl()


def fetch_page(session: requests.Session, url: str) -> str:
    """Fetch a public https page, checking every redirect, and return its HTML."""
    for _ in range(MAX_REDIRECTS + 1):
        url = check_url(url)
        host = urlparse(url).hostname or ""
        if is_closed_site(host):
            raise ManualJobError("closed_site")
        if not _is_public(host):
            raise ManualJobError("private_address")
        try:
            response = session.get(url, timeout=TIMEOUT_SECONDS, allow_redirects=False, stream=True)
        except requests.RequestException:
            raise ManualJobError("fetch_failed")
        with response:
            if response.is_redirect:
                url = urljoin(url, response.headers.get("Location", ""))
                continue
            if response.status_code != 200:
                raise ManualJobError("fetch_failed")
            content = response.raw.read(MAX_PAGE_BYTES, decode_content=True)
            return content.decode(response.encoding or "utf-8", errors="replace")
    raise ManualJobError("fetch_failed")


def _location(value) -> str:
    places = value if isinstance(value, list) else [value]
    names = []
    for place in places:
        address = (place or {}).get("address") if isinstance(place, dict) else None
        if isinstance(address, dict):
            names.append(", ".join(part for part in (address.get("addressLocality"), address.get("addressCountry")) if isinstance(part, str) and part))
        elif isinstance(address, str):
            names.append(address)
    return "; ".join(dict.fromkeys(name for name in names if name))


def _salary(value) -> str:
    """'EUR 60,000-75,000 per year' from a schema.org MonetaryAmount, or empty."""
    if not isinstance(value, dict):
        return ""
    amount = value.get("value") if isinstance(value.get("value"), dict) else {"value": value.get("value")}
    low, high, single = amount.get("minValue"), amount.get("maxValue"), amount.get("value")
    def number(raw) -> str:
        try:
            return f"{float(raw):,.0f}"
        except (TypeError, ValueError):
            return ""
    figures = "-".join(part for part in (number(low), number(high)) if part) or number(single)
    if not figures:
        return ""
    unit = str(amount.get("unitText") or "").lower()
    return " ".join(part for part in (str(value.get("currency") or ""), figures, f"per {unit}" if unit else "") if part)


def _details(item: dict) -> dict[str, str]:
    kinds = item.get("employmentType")
    details = {
        "Employment type": ", ".join(kinds) if isinstance(kinds, list) else str(kinds or ""),
        "Salary": _salary(item.get("baseSalary")),
        "Closing date": str(item.get("validThrough") or "")[:10],
    }
    return {label: html_to_text(text).replace("_", " ").title() if label == "Employment type" else text for label, text in details.items() if text}


def parse_json_ld(page: str) -> dict | None:
    """Read the schema.org JobPosting that most job pages include for Google Jobs."""
    for block in JSON_LD_RE.findall(page):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in items:
            if not isinstance(item, dict) or "JobPosting" not in (item.get("@type") if isinstance(item.get("@type"), list) else [item.get("@type")]):
                continue
            organisation = item.get("hiringOrganization")
            company = organisation.get("name") if isinstance(organisation, dict) else organisation if isinstance(organisation, str) else ""
            return {
                "title": html_to_text(str(item.get("title") or "")),
                "company": html_to_text(str(company or "")),
                "location": _location(item.get("jobLocation")),
                "remote": str(item.get("jobLocationType") or "").upper() == "TELECOMMUTE",
                "posted_date": str(item.get("datePosted") or ""),
                "description": html_to_text(str(item.get("description") or "")),
                "details": _details(item),
            }
    return None


def build_job(url: str, pasted_text: str, session: requests.Session, ai) -> Job:
    """Turn a link, or a link plus pasted job text, into a Job. Raises ManualJobError for problems the owner can fix."""
    url = check_url(url)
    if pasted_text.strip():
        fields, description = ai.extract_posting(pasted_text), pasted_text.strip()
    elif is_closed_site(urlparse(url).hostname or ""):
        raise ManualJobError("closed_site")
    else:
        page = fetch_page(session, url)
        fields = parse_json_ld(page) or {}
        description = fields.get("description", "")
        # Some pages publish only a title and date as structured data. Then the page text is the posting,
        # and the AI fills in whatever the structured data left out.
        if not description.strip():
            description = html_to_text(page)
        if not all(fields.get(key) for key in ("title", "company", "location")):
            extracted = ai.extract_posting(description)
            if fields and extracted.get("is_job_posting") is not False:
                extracted["is_job_posting"] = True
            fields = {**extracted, **{key: value for key, value in fields.items() if value}}
    if fields.get("is_job_posting") is False or not fields.get("title"):
        raise ManualJobError("not_a_job")
    return Job(
        source="manual",
        native_id=hashlib.sha256(url.encode()).hexdigest()[:16],
        title=fields["title"].strip(),
        company=fields.get("company", "").strip(),
        location=fields.get("location", "").strip(),
        url=url,
        description=description[:MAX_DESCRIPTION_CHARS],
        remote=bool(fields.get("remote")),
        posted_at=fields.get("posted_date", "").strip(),
        details=fields.get("details") or {},
    )
