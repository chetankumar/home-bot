"""Discover apps/<id>/ packages, load each in isolation, and mount them.

A broken app is recorded as failed (with its traceback) and shown on the
launcher and /admin; the host and every other app still start.
"""

from __future__ import annotations

import importlib
import logging
import re
import sys
import traceback
import types
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, FastAPI
from fastapi.staticfiles import StaticFiles

from hub.plugin import AppContext, Manifest
from hub.services.db import Database
from hub.services.gmail import GMAIL_SCOPE, GmailClient
from hub.services.http import AppHttp
from hub.services.kv import KV
from hub.services.scheduler import AppScheduler
from hub.templating import make_templates

if TYPE_CHECKING:
    from hub.main import Hub

log = logging.getLogger("hub.registry")

PACKAGE = "hub_apps"  # synthetic parent package for loaded apps
APP_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass
class Requirement:
    """A host-provided connection an app can declare in Manifest.requires."""

    name: str
    label: str
    oauth_provider: str
    scopes: list[str]


REQUIREMENTS = {
    "gmail": Requirement("gmail", "Gmail", "google", [GMAIL_SCOPE]),
}


@dataclass
class LoadedApp:
    id: str
    path: Path
    status: str  # loaded | failed | disabled
    manifest: Manifest | None = None
    ctx: AppContext | None = None
    error: str | None = None

    @property
    def name(self) -> str:
        return self.manifest.name if self.manifest else self.id

    @property
    def icon(self) -> str:
        return self.manifest.icon if self.manifest else "⚠️"


class Registry:
    def __init__(self, hub: Hub):
        self.hub = hub
        self.apps: dict[str, LoadedApp] = {}

    # -- discovery -----------------------------------------------------------------
    @staticmethod
    def discover(apps_dir: Path) -> list[Path]:
        if not apps_dir.is_dir():
            return []
        return sorted(
            p
            for p in apps_dir.iterdir()
            if p.is_dir() and not p.name.startswith(("_", ".")) and (p / "__init__.py").exists()
        )

    def _prepare_package(self, apps_dir: Path) -> None:
        for name in [m for m in sys.modules if m == PACKAGE or m.startswith(PACKAGE + ".")]:
            del sys.modules[name]
        pkg = types.ModuleType(PACKAGE)
        pkg.__path__ = [str(apps_dir)]
        sys.modules[PACKAGE] = pkg

    def load_all(self, app: FastAPI) -> None:
        apps_dir = self.hub.config.apps_dir
        self._prepare_package(apps_dir)
        disabled = set(self.hub.config.disabled)
        for path in self.discover(apps_dir):
            app_id = path.name
            if app_id in disabled:
                self.apps[app_id] = LoadedApp(app_id, path, "disabled")
                log.info("app %s disabled in hub.toml", app_id)
                continue
            self.apps[app_id] = self._load(app, app_id, path)

    # -- loading -------------------------------------------------------------------
    def _load(self, app: FastAPI, app_id: str, path: Path) -> LoadedApp:
        entry = LoadedApp(app_id, path, "failed")
        try:
            if not APP_ID_RE.match(app_id):
                raise ValueError(f"folder name {app_id!r} must match {APP_ID_RE.pattern}")
            module = importlib.import_module(f"{PACKAGE}.{app_id}")
            manifest = getattr(module, "manifest", None)
            setup = getattr(module, "setup", None)
            if not isinstance(manifest, Manifest):
                raise TypeError("module must define `manifest = Manifest(...)`")
            if not callable(setup):
                raise TypeError("module must define `setup(ctx) -> APIRouter`")
            if manifest.id != app_id:
                raise ValueError(f"manifest id {manifest.id!r} must match folder {app_id!r}")
            entry.manifest = manifest
            unknown = [r for r in manifest.requires if r not in REQUIREMENTS]
            if unknown:
                raise ValueError(f"unknown requirement(s) {unknown}; known: {list(REQUIREMENTS)}")

            ctx = self._context(manifest, path)
            entry.ctx = ctx
            router = setup(ctx)
            if not isinstance(router, APIRouter):
                raise TypeError("setup(ctx) must return a fastapi.APIRouter")

            static = path / "static"
            if static.is_dir():
                app.mount(
                    f"/apps/{app_id}/static",
                    StaticFiles(directory=static),
                    name=f"{app_id}-static",
                )
            app.include_router(router, prefix=f"/apps/{app_id}")
            entry.status = "loaded"
            log.info("loaded app %s", app_id)
        except Exception:
            entry.status = "failed"
            entry.error = traceback.format_exc()
            self.hub.scheduler.remove_app(app_id)  # drop jobs a half-finished setup added
            log.error("app %s failed to load:\n%s", app_id, entry.error)
        return entry

    def _context(self, manifest: Manifest, path: Path) -> AppContext:
        hub = self.hub
        app_id = manifest.id
        app_cfg = hub.config.app_config(app_id)
        db = Database(hub.config.data_dir / "apps" / f"{app_id}.db", path / "migrations")
        prefix = f"/apps/{app_id}"
        templates = make_templates(
            [path / "templates"],
            hub.config.tz,
            {
                **hub.template_globals(),
                "app": manifest,
                "app_url": lambda p="/": prefix + (p if p.startswith("/") else "/" + p),
                "app_static": lambda p: f"{prefix}/static/{p.lstrip('/')}",
            },
        )
        gmail = None
        if "gmail" in manifest.requires:
            gmail = GmailClient(hub.oauth, REQUIREMENTS["gmail"].oauth_provider)
        return AppContext(
            id=app_id,
            manifest=manifest,
            path=path,
            db=db,
            ai=hub.ai.for_app(app_id),
            http=AppHttp(hub.http, app_id, app_cfg.get("connections")),
            scheduler=AppScheduler(hub.scheduler, app_id),
            kv=KV(hub.db, app_id),
            templates=templates,
            tz=hub.config.tz,
            log=logging.getLogger(f"apps.{app_id}"),
            config={k: v for k, v in app_cfg.items() if k != "ai"},
            _gmail=gmail,
        )

    # -- queries -------------------------------------------------------------------
    def missing_requirements(self, entry: LoadedApp) -> list[Requirement]:
        if not entry.manifest:
            return []
        return [
            REQUIREMENTS[r]
            for r in entry.manifest.requires
            if r in REQUIREMENTS and not self.hub.oauth.is_connected(REQUIREMENTS[r].oauth_provider)
        ]

    def launcher(self) -> list[dict[str, Any]]:
        out = []
        for entry in self.apps.values():
            if entry.status == "disabled":
                continue
            out.append({"entry": entry, "missing": self.missing_requirements(entry)})
        return out

    def scopes_for(self, oauth_provider: str) -> list[str]:
        """Union of scopes every requirement on this provider needs."""
        scopes: list[str] = []
        for req in REQUIREMENTS.values():
            if req.oauth_provider == oauth_provider:
                scopes += [s for s in req.scopes if s not in scopes]
        return scopes
