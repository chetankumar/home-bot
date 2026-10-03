"""The plugin contract: what an app exports and what the host hands it.

An app is a package under apps/<id>/ exporting:

    manifest = Manifest(id="hello", name="Hello", icon="👋", description="...")

    def setup(ctx: AppContext) -> APIRouter: ...

AppContext is the only way an app touches the host; everything on it is
scoped to that app.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates

if TYPE_CHECKING:
    from hub.services.ai.service import AppAI
    from hub.services.db import AppDB
    from hub.services.gmail import GmailClient
    from hub.services.http import AppHttp
    from hub.services.kv import KV
    from hub.services.scheduler import AppScheduler


@dataclass
class Manifest:
    id: str
    name: str
    icon: str = "🧩"
    description: str = ""
    requires: list[str] = field(default_factory=list)
    version: str = "0.1.0"


@dataclass
class AppContext:
    id: str
    manifest: Manifest
    path: Path
    db: AppDB
    ai: AppAI
    http: AppHttp
    scheduler: AppScheduler
    kv: KV
    templates: Jinja2Templates
    tz: ZoneInfo
    log: logging.Logger
    config: dict[str, Any] = field(default_factory=dict)
    _gmail: GmailClient | None = None

    @property
    def gmail(self) -> GmailClient:
        if self._gmail is None:
            raise AttributeError(
                f"app '{self.id}' did not declare requires=['gmail'] in its manifest"
            )
        return self._gmail

    @property
    def prefix(self) -> str:
        return f"/apps/{self.id}"

    def url(self, path: str = "/") -> str:
        return self.prefix + (path if path.startswith("/") else "/" + path)

    def render(self, request: Request, name: str, status_code: int = 200, headers=None, **context):
        """Render one of the app's templates (falling back to the hub's)."""
        context.setdefault("app", self.manifest)
        return self.templates.TemplateResponse(
            request, name, context, status_code=status_code, headers=headers
        )
