"""Configuration: secrets from .env / environment, structure from hub.toml."""

from __future__ import annotations

import logging
import os
import secrets
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import dotenv_values
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("hub.config")

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Secrets and deployment knobs. Read from the environment, then .env."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    hub_password: str = ""
    hub_secret_key: str = ""
    hub_base_url: str = "http://localhost:8000"
    hub_toml: str = "hub.toml"
    hub_data_dir: str = "data"
    hub_apps_dir: str = "apps"
    google_client_id: str = ""
    google_client_secret: str = ""


@dataclass
class Config:
    """Everything the host needs to start, resolved to absolute paths."""

    settings: Settings
    toml: dict[str, Any]
    base_dir: Path
    env: dict[str, str] = field(default_factory=dict)

    # -- paths -------------------------------------------------------------
    def _path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else self.base_dir / p

    @property
    def data_dir(self) -> Path:
        return self._path(self.settings.hub_data_dir)

    @property
    def apps_dir(self) -> Path:
        return self._path(self.settings.hub_apps_dir)

    # -- hub.toml sections ---------------------------------------------------
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.toml.get("timezone", "UTC"))

    @property
    def disabled(self) -> list[str]:
        return list(self.toml.get("disabled", []))

    @property
    def ai(self) -> dict[str, Any]:
        return self.toml.get("ai", {})

    @property
    def connections(self) -> dict[str, Any]:
        return self.toml.get("connections", {})

    def app_config(self, app_id: str) -> dict[str, Any]:
        return self.toml.get("apps", {}).get(app_id, {})

    # -- secrets -------------------------------------------------------------
    def secret(self, name: str) -> str:
        """Look up an arbitrary secret (e.g. OPENAI_API_KEY) in env, then .env."""
        return os.environ.get(name) or self.env.get(name) or ""

    def ensure_secret_key(self) -> str:
        """HUB_SECRET_KEY signs sessions and encrypts OAuth tokens.

        If it isn't set, generate one once and keep it in data/ so restarts
        don't log you out or orphan stored tokens.
        """
        if self.settings.hub_secret_key:
            return self.settings.hub_secret_key
        path = self.data_dir / ".secret_key"
        if path.exists():
            key = path.read_text(encoding="utf-8").strip()
        else:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            key = secrets.token_urlsafe(48)
            path.write_text(key, encoding="utf-8")
            log.warning("HUB_SECRET_KEY not set; generated one in %s", path)
        self.settings.hub_secret_key = key
        return key


def load_config(base_dir: Path | None = None, **overrides: Any) -> Config:
    base = Path(base_dir) if base_dir else BASE_DIR
    env_file = base / ".env"
    settings = Settings(_env_file=env_file if env_file.exists() else None, **overrides)
    toml_path = Path(settings.hub_toml)
    if not toml_path.is_absolute():
        toml_path = base / toml_path
    toml = tomllib.loads(toml_path.read_text(encoding="utf-8")) if toml_path.exists() else {}
    env = {k: v for k, v in dotenv_values(env_file, encoding="utf-8").items() if v} if env_file.exists() else {}
    return Config(settings=settings, toml=toml, base_dir=base, env=env)
