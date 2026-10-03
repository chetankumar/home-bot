"""Read-only Gmail connector built on the OAuth service."""

from __future__ import annotations

import base64
import html
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parseaddr
from typing import Any

import httpx

from hub.services.oauth import NotConnected, OAuthService

__all__ = ["GMAIL_SCOPE", "GmailClient", "GmailMessage", "NotConnected"]

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
API = "https://gmail.googleapis.com/gmail/v1/users/me"


@dataclass
class GmailMessage:
    id: str
    thread_id: str
    received_at: datetime  # UTC, from Gmail's internalDate
    sender: str  # bare address, lower-cased
    subject: str
    body: str  # decoded plain text (HTML stripped if that's all there was)
    snippet: str


class GmailClient:
    def __init__(self, oauth: OAuthService, provider: str = "google", http: httpx.Client | None = None):
        self._oauth = oauth
        self._provider = provider
        self._http = http or httpx.Client(timeout=30)

    @property
    def connected(self) -> bool:
        return self._oauth.is_connected(self._provider)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in range(2):
            token = self._oauth.access_token(self._provider)
            resp = self._http.get(
                API + path, params=params, headers={"Authorization": f"Bearer {token}"}
            )
            if resp.status_code == 401 and attempt == 0:
                self._oauth.refresh(self._provider)
                continue
            resp.raise_for_status()
            return resp.json()
        raise NotConnected("Gmail rejected the refreshed token; reconnect Google")

    def profile(self) -> dict[str, Any]:
        return self._get("/profile")

    def iter_ids(self, query: str, page_size: int = 100) -> Iterator[str]:
        page: str | None = None
        while True:
            params: dict[str, Any] = {"q": query, "maxResults": page_size}
            if page:
                params["pageToken"] = page
            data = self._get("/messages", params)
            for m in data.get("messages", []):
                yield m["id"]
            page = data.get("nextPageToken")
            if not page:
                return

    def search(self, query: str, limit: int = 2000) -> list[str]:
        """Message ids matching a Gmail search query, newest first."""
        ids = []
        for mid in self.iter_ids(query):
            ids.append(mid)
            if len(ids) >= limit:
                break
        return ids

    def get_message(self, message_id: str) -> GmailMessage:
        data = self._get(f"/messages/{message_id}", {"format": "full"})
        payload = data.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        return GmailMessage(
            id=data["id"],
            thread_id=data.get("threadId", ""),
            received_at=datetime.fromtimestamp(int(data.get("internalDate", 0)) / 1000, UTC),
            sender=parseaddr(headers.get("from", ""))[1].lower(),
            subject=headers.get("subject", ""),
            body=extract_body(payload),
            snippet=html.unescape(data.get("snippet", "")),
        )


def _b64(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def _walk(part: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield part
    for sub in part.get("parts", []) or []:
        yield from _walk(sub)


def extract_body(payload: dict[str, Any]) -> str:
    plain, rich = [], []
    for part in _walk(payload):
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        mime = part.get("mimeType", "")
        if mime == "text/plain":
            plain.append(_b64(data))
        elif mime == "text/html":
            rich.append(html_to_text(_b64(data)))
    text = "\n".join(plain) if any(p.strip() for p in plain) else "\n".join(rich)
    return normalize_ws(text)


_DROP = re.compile(r"<(script|style|head)\b.*?</\1>", re.S | re.I)
_BREAK = re.compile(r"<\s*(br|/p|/div|/tr|/li|/h\d)\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")


def html_to_text(markup: str) -> str:
    text = _DROP.sub(" ", markup)
    text = _BREAK.sub("\n", text)
    text = _TAG.sub(" ", text)
    return html.unescape(text)


def normalize_ws(text: str) -> str:
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.splitlines()]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()
