"""Generic OAuth2 authorization-code flow with an encrypted token store.

Tokens live in hub.db, Fernet-encrypted with a key derived from
HUB_SECRET_KEY. Access tokens are refreshed automatically shortly before
they expire. Implemented directly on httpx so any OAuth2 provider can reuse
it; Google is the first one registered.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken

from hub.services.db import Database

log = logging.getLogger("hub.oauth")

REFRESH_MARGIN = 60  # seconds before expiry at which we refresh


class OAuthError(Exception):
    pass


class NotConnected(OAuthError):
    """No usable token: the one-time consent hasn't been done (or was revoked)."""


@dataclass
class OAuthProvider:
    name: str
    label: str
    auth_url: str
    token_url: str
    client_id: str
    client_secret: str
    scopes: list[str]
    extra_auth_params: dict[str, str] = field(default_factory=dict)
    revoke_url: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


def google_provider(client_id: str, client_secret: str, scopes: list[str]) -> OAuthProvider:
    return OAuthProvider(
        name="google",
        label="Google",
        auth_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        revoke_url="https://oauth2.googleapis.com/revoke",
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes,
        # offline + consent guarantees a refresh token on every connect.
        extra_auth_params={"access_type": "offline", "prompt": "consent"},
    )


def fernet_from_secret(secret: str) -> Fernet:
    digest = hashlib.sha256(("home-hub-oauth:" + secret).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


class TokenStore:
    def __init__(self, db: Database, fernet: Fernet):
        self._db = db
        self._fernet = fernet

    def get(self, provider: str) -> dict[str, Any] | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT data FROM oauth_tokens WHERE provider = ?", (provider,)
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(self._fernet.decrypt(row[0]))
        except InvalidToken:
            log.error("stored %s token can't be decrypted (HUB_SECRET_KEY changed?)", provider)
            return None

    def put(self, provider: str, token: dict[str, Any]) -> None:
        blob = self._fernet.encrypt(json.dumps(token).encode())
        with self._db() as conn:
            conn.execute(
                "INSERT INTO oauth_tokens(provider, data) VALUES (?, ?)"
                " ON CONFLICT(provider) DO UPDATE SET data = excluded.data,"
                " updated_at = datetime('now')",
                (provider, blob),
            )

    def delete(self, provider: str) -> None:
        with self._db() as conn:
            conn.execute("DELETE FROM oauth_tokens WHERE provider = ?", (provider,))


class OAuthService:
    def __init__(
        self,
        store: TokenStore,
        redirect_base: str,
        http: httpx.Client | None = None,
        clock=time.time,
    ):
        self.store = store
        self.redirect_base = redirect_base.rstrip("/")
        self.providers: dict[str, OAuthProvider] = {}
        self._http = http or httpx.Client(timeout=30)
        self._clock = clock

    def register(self, provider: OAuthProvider) -> None:
        self.providers[provider.name] = provider

    def _provider(self, name: str) -> OAuthProvider:
        try:
            return self.providers[name]
        except KeyError:
            raise OAuthError(f"unknown OAuth provider {name!r}") from None

    def redirect_uri(self, name: str) -> str:
        return f"{self.redirect_base}/connections/{name}/callback"

    def authorization_url(self, name: str, state: str) -> str:
        p = self._provider(name)
        if not p.configured:
            raise OAuthError(f"{p.label} OAuth client id/secret are not set in .env")
        params = {
            "response_type": "code",
            "client_id": p.client_id,
            "redirect_uri": self.redirect_uri(name),
            "scope": " ".join(p.scopes),
            "state": state,
            **p.extra_auth_params,
        }
        return f"{p.auth_url}?{urlencode(params)}"

    def _token_request(self, p: OAuthProvider, data: dict[str, str]) -> dict[str, Any]:
        resp = self._http.post(
            p.token_url,
            data={**data, "client_id": p.client_id, "client_secret": p.client_secret},
            headers={"Accept": "application/json"},
        )
        if resp.status_code >= 400:
            raise OAuthError(f"{p.label} token endpoint returned {resp.status_code}: {resp.text}")
        return resp.json()

    def _stamp(self, token: dict[str, Any]) -> dict[str, Any]:
        if "expires_in" in token:
            token["expires_at"] = self._clock() + int(token["expires_in"])
        return token

    def exchange_code(self, name: str, code: str) -> dict[str, Any]:
        p = self._provider(name)
        token = self._token_request(
            p,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri(name),
            },
        )
        token = self._stamp(token)
        token["connected_at"] = self._clock()
        self.store.put(name, token)
        return token

    def refresh(self, name: str) -> dict[str, Any]:
        p = self._provider(name)
        current = self.store.get(name)
        if not current or not current.get("refresh_token"):
            raise NotConnected(f"{p.label} is not connected")
        try:
            fresh = self._token_request(
                p, {"grant_type": "refresh_token", "refresh_token": current["refresh_token"]}
            )
        except OAuthError as e:
            if "invalid_grant" in str(e):  # revoked or expired refresh token
                raise NotConnected(f"{p.label} access was revoked or expired; reconnect") from e
            raise
        # Providers usually omit the refresh token on refresh; keep the old one.
        merged = {**current, **self._stamp(fresh)}
        self.store.put(name, merged)
        return merged

    def access_token(self, name: str) -> str:
        token = self.store.get(name)
        if not token:
            raise NotConnected(f"{self._provider(name).label} is not connected")
        if token.get("expires_at", 0) - REFRESH_MARGIN <= self._clock():
            token = self.refresh(name)
        return token["access_token"]

    def is_connected(self, name: str) -> bool:
        token = self.store.get(name)
        return bool(token and token.get("refresh_token"))

    def info(self, name: str) -> dict[str, Any]:
        token = self.store.get(name) or {}
        return {
            "connected": bool(token.get("refresh_token")),
            "connected_at": token.get("connected_at"),
            "account": token.get("account"),
            "scope": token.get("scope"),
        }

    def annotate(self, name: str, **fields: Any) -> None:
        token = self.store.get(name)
        if token:
            self.store.put(name, {**token, **fields})

    def disconnect(self, name: str) -> None:
        p = self._provider(name)
        token = self.store.get(name)
        if token and p.revoke_url:
            try:
                self._http.post(
                    p.revoke_url, data={"token": token.get("refresh_token") or token["access_token"]}
                )
            except Exception:
                log.warning("revoking %s token failed; deleting locally anyway", name)
        self.store.delete(name)
