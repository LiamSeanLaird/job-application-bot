"""Small clients for Google OAuth and Gmail, using plain HTTP to stay light on memory."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import webbrowser
from datetime import datetime, timezone
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from jobbot.config import GOOGLE_CLIENT_FILE, GOOGLE_TOKEN_FILE, OWNER_EMAIL
from jobbot.models import html_to_text

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
TIMEOUT_SECONDS = 30
MAX_ATTEMPTS = 5
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

class GoogleError(RuntimeError):
    pass


def _read_client(client_file: Path) -> dict:
    data = json.loads(client_file.read_text(encoding="utf-8"))
    return data.get("installed") or data["web"]


def authorize(client_file: Path = GOOGLE_CLIENT_FILE, token_file: Path = GOOGLE_TOKEN_FILE) -> None:
    """Run the one-time browser approval on your own computer, and store the resulting token."""
    client = _read_client(client_file)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            result.update({key: values[0] for key, values in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Job bot authorised. You can close this tab.")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}/"
    url = f"{AUTH_URL}?" + urlencode(
        {
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    print(f"Opening your browser. If it does not open, visit:\n{url}")
    webbrowser.open(url)
    server.handle_request()
    server.server_close()

    if result.get("state") != state or "code" not in result:
        raise GoogleError(f"Google authorisation failed: {result.get('error', 'state mismatch')}")
    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": client["client_id"],
            "client_secret": client.get("client_secret", ""),
            "code": result["code"],
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        raise GoogleError(f"Token exchange failed: {response.status_code}")
    token = response.json()
    if "refresh_token" not in token:
        raise GoogleError("Google did not return a refresh token")
    token_file.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"refresh_token": token["refresh_token"]}, handle)
    print(f"Saved Google access to {token_file}")


class GoogleClient:
    def __init__(self, client_file: Path = GOOGLE_CLIENT_FILE, token_file: Path = GOOGLE_TOKEN_FILE) -> None:
        self.client = _read_client(client_file)
        self.refresh_token = json.loads(token_file.read_text(encoding="utf-8"))["refresh_token"]
        self.session = requests.Session()
        self.access_token = ""
        self.expires_at = 0.0

    def _token(self) -> str:
        if time.time() < self.expires_at - 60:
            return self.access_token
        response = self.session.post(
            TOKEN_URL,
            data={
                "client_id": self.client["client_id"],
                "client_secret": self.client.get("client_secret", ""),
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            raise GoogleError(f"Could not refresh Google access: {response.status_code} {response.text[:200]}")
        token = response.json()
        self.access_token = token["access_token"]
        self.expires_at = time.time() + token.get("expires_in", 3600)
        return self.access_token

    def request(self, method: str, url: str, **kwargs) -> dict:
        for attempt in range(MAX_ATTEMPTS):
            response = self.session.request(
                method, url, headers={"Authorization": f"Bearer {self._token()}"}, timeout=TIMEOUT_SECONDS, **kwargs
            )
            if response.status_code < 300:
                return response.json() if response.content else {}
            if response.status_code not in RETRYABLE_STATUS or attempt == MAX_ATTEMPTS - 1:
                raise GoogleError(f"{method} {urlparse(url).path} returned {response.status_code}: {response.text[:300]}")
            time.sleep(2**attempt)
        raise GoogleError("Google request failed")


class Gmail:
    def __init__(self, client: GoogleClient) -> None:
        self.client = client

    def search(self, query: str, max_results: int = 50) -> list[str]:
        data = self.client.request("GET", f"{GMAIL_URL}/messages", params={"q": query, "maxResults": max_results})
        return [message["id"] for message in data.get("messages") or []]

    def read(self, message_id: str) -> dict:
        data = self.client.request("GET", f"{GMAIL_URL}/messages/{message_id}", params={"format": "full"})
        headers = {header["name"].lower(): header["value"] for header in data["payload"].get("headers") or []}
        return {
            "from": headers.get("from", ""),
            "subject": headers.get("subject", ""),
            "text": message_text(data["payload"]),
            "received_at": datetime.fromtimestamp(int(data["internalDate"]) / 1000, timezone.utc),
        }

    def send_to_owner(self, subject: str, text: str, html: str) -> None:
        message = EmailMessage()
        message["To"] = OWNER_EMAIL
        message["From"] = OWNER_EMAIL
        message["Subject"] = subject
        message.set_content(text)
        message.add_alternative(html, subtype="html")
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        self.client.request("POST", f"{GMAIL_URL}/messages/send", json={"raw": raw})


def message_text(payload: dict) -> str:
    html_parts: list[str] = []
    text_parts: list[str] = []

    def walk(part: dict) -> None:
        data = (part.get("body") or {}).get("data")
        if data:
            decoded = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
            if part.get("mimeType") == "text/html":
                html_parts.append(decoded)
            elif part.get("mimeType") == "text/plain":
                text_parts.append(decoded)
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    if html_parts:
        return html_to_text("\n".join(html_parts), keep_links=True)
    return "\n".join(text_parts)
