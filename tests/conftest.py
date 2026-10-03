from __future__ import annotations

import re

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hub.config import Config, Settings
from hub.main import create_app
from hub.services.gmail import GmailMessage, NotConnected

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

BASE_TOML = {
    "timezone": "Asia/Kolkata",
    "ai": {
        "providers": {
            "anthropic": {"type": "anthropic"},
            "ollama": {"type": "openai_compat", "base_url": "http://localhost:11434/v1", "key_required": False},
        },
        "aliases": {"default": "anthropic:claude-opus-5-5", "local": "ollama:llama3.1:8b"},
    },
    "apps": {"finance": {"ai": {"allowed_providers": ["ollama"], "default_alias": "local"}}},
}


def make_config(tmp_path: Path, apps_dir: Path | None = None, toml: dict | None = None, **settings) -> Config:
    values = {
        "hub_password": "pw",
        "hub_secret_key": "test-secret",
        "hub_data_dir": str(tmp_path / "data"),
        "hub_apps_dir": str(apps_dir or ROOT / "apps"),
        **settings,
    }
    s = Settings(_env_file=None, **values)
    return Config(settings=s, toml=toml if toml is not None else BASE_TOML, base_dir=tmp_path)


def login(client: TestClient) -> TestClient:
    r = client.post("/login", data={"password": "pw", "next": "/"}, follow_redirects=False)
    assert r.status_code == 303
    return client


@pytest.fixture
def hub_app(tmp_path):
    def factory(apps_dir: Path | None = None, toml: dict | None = None, **settings):
        return create_app(make_config(tmp_path, apps_dir, toml, **settings))

    return factory


@pytest.fixture
def client(hub_app):
    return login(TestClient(hub_app()))


class FakeGmail:
    """Stands in for ctx.gmail: a dict of id -> GmailMessage."""

    def __init__(self, messages: list[GmailMessage] | None = None, connected: bool = True):
        self.messages = {m.id: m for m in messages or []}
        self.connected = connected
        self.queries: list[str] = []
        self.fetched: list[str] = []

    def add(self, mid: str, body: str, when: datetime, subject: str = "Alert", sender="alerts@hdfcbank.net"):
        self.messages[mid] = GmailMessage(mid, mid, when.astimezone(UTC), sender, subject, body, body[:80])

    def search(self, query: str, limit: int = 2000) -> list[str]:
        if not self.connected:
            raise NotConnected("Google is not connected")
        self.queries.append(query)
        wanted = re.search(r"from:\(([^)]*)\)", query)  # like Gmail, only mail from those senders
        senders = {a.strip().lower() for a in wanted.group(1).split(" OR ")} if wanted else None
        found = [m for m in self.messages.values() if senders is None or m.sender in senders]
        return [m.id for m in sorted(found, key=lambda m: m.received_at, reverse=True)]

    def get_message(self, mid: str) -> GmailMessage:
        self.fetched.append(mid)
        return self.messages[mid]


def fixture_email(name: str, folder: str = "finance") -> tuple[str, str]:
    """Fixture files: first line 'Subject: ...', blank line, then the body."""
    text = (FIXTURES / folder / name).read_text(encoding="utf-8")
    head, _, body = text.partition("\n\n")
    return head.removeprefix("Subject: ").strip(), body.strip()
