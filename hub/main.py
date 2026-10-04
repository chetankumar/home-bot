"""The host: build services, discover and mount apps, serve the shell UI.

Run with:  uv run uvicorn hub.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from hub import auth
from hub.config import Config, load_config
from hub.registry import REQUIREMENTS, Registry
from hub.services.ai.service import AIService
from hub.services.charts import Charts
from hub.services.db import Database, hub_database
from hub.services.gmail import GmailClient
from hub.services.http import HttpConnections
from hub.services.oauth import (
    OAuthError,
    OAuthService,
    TokenStore,
    fernet_from_secret,
    google_provider,
)
from hub.services.scheduler import Scheduler
from hub.templating import make_templates

log = logging.getLogger("hub")
STATIC_DIR = Path(__file__).resolve().parent / "static"


@dataclass
class Hub:
    """All host services, reachable from routes as request.app.state.hub."""

    config: Config
    db: Database
    ai: AIService
    http: HttpConnections
    oauth: OAuthService
    scheduler: Scheduler
    templates: Jinja2Templates = field(init=False)
    charts: Charts = field(init=False)
    registry: Registry = field(init=False)

    def template_globals(self) -> dict[str, Any]:
        return {"hub_nav": self.nav, "hub_tz": self.config.tz}

    def nav(self) -> list[Any]:
        return [a for a in self.registry.apps.values() if a.status == "loaded"]


def build_hub(config: Config) -> Hub:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    secret = config.ensure_secret_key()
    db = hub_database(config.data_dir)
    if db.applied:
        log.info("hub.db: applied migrations %s", ", ".join(db.applied))
    oauth = OAuthService(TokenStore(db, fernet_from_secret(secret)), config.settings.hub_base_url)
    hub = Hub(
        config=config,
        db=db,
        ai=AIService(config, db),
        http=HttpConnections(config),
        oauth=oauth,
        scheduler=Scheduler(db, config.tz),
    )
    hub.registry = Registry(hub)
    oauth.register(
        google_provider(
            config.settings.google_client_id,
            config.settings.google_client_secret,
            hub.registry.scopes_for("google"),
        )
    )
    hub.templates = make_templates([], config.tz, hub.template_globals())
    hub.charts = Charts(hub.templates.env)
    return hub


def create_app(config: Config | None = None) -> FastAPI:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        for noisy in ("httpx", "httpx2", "apscheduler"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    config = config or load_config()
    hub = build_hub(config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        hub.scheduler.start()
        try:
            yield
        finally:
            hub.scheduler.shutdown()

    app = FastAPI(title="Home Hub", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.hub = hub
    # Order matters: the last middleware added is outermost, and auth needs the session.
    app.add_middleware(auth.AuthMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.settings.hub_secret_key,
        session_cookie="hub_session",
        max_age=30 * 24 * 3600,
        same_site="lax",
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(auth.build_router(lambda: config.settings.hub_password))

    hub.registry.load_all(app)
    _host_routes(app, hub)
    return app


def _host_routes(app: FastAPI, hub: Hub) -> None:
    render = hub.templates.TemplateResponse

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def launcher(request: Request):
        return render(request, "index.html", {"apps": hub.registry.launcher()})

    @app.get("/admin", response_class=HTMLResponse)
    def admin(request: Request):
        return render(
            request,
            "admin.html",
            {
                "apps": list(hub.registry.apps.values()),
                "registry": hub.registry,
                "jobs": hub.scheduler.jobs(),
                "usage": hub.ai.usage_summary(),
                "providers": hub.ai.status(),
                "aliases": hub.ai.aliases,
                "connections": _connections(hub),
            },
        )

    @app.post("/admin/jobs/{app_id}/{job_id}/run")
    def admin_run_job(app_id: str, job_id: str):
        try:
            hub.scheduler.run_now(app_id, job_id)
        except KeyError:
            raise HTTPException(404, "no such job") from None
        return RedirectResponse("/admin", status_code=303)

    # -- connections ---------------------------------------------------------------
    @app.get("/connections", response_class=HTMLResponse)
    def connections(request: Request, error: str | None = None, ok: str | None = None):
        return render(
            request,
            "connections.html",
            {
                "connections": _connections(hub),
                "error": error,
                "ok": ok,
                "on_host": _on_host(request, hub),
                "base_url": hub.config.settings.hub_base_url,
            },
        )

    @app.get("/connections/{provider}/start")
    def connect_start(request: Request, provider: str):
        if provider not in hub.oauth.providers:
            raise HTTPException(404)
        if not _on_host(request, hub):
            return _back(
                error=f"Open {hub.config.settings.hub_base_url}/connections on the host PC "
                "to connect: Google only redirects back to localhost."
            )
        state = secrets.token_urlsafe(24)
        request.session["oauth_state"] = {"provider": provider, "state": state}
        try:
            url = hub.oauth.authorization_url(provider, state)
        except OAuthError as e:
            return _back(error=str(e))
        return RedirectResponse(url, status_code=303)

    @app.get("/connections/{provider}/callback")
    def connect_callback(
        request: Request,
        provider: str,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ):
        expected = request.session.pop("oauth_state", None) or {}
        if error:
            return _back(error=f"Consent failed: {error}")
        if (
            not code
            or expected.get("provider") != provider
            or not secrets.compare_digest(expected.get("state", ""), state or "")
        ):
            return _back(error="OAuth state mismatch; try again.")
        try:
            hub.oauth.exchange_code(provider, code)
        except OAuthError as e:
            return _back(error=str(e))
        if provider == "google":
            try:
                email = GmailClient(hub.oauth).profile().get("emailAddress")
                hub.oauth.annotate("google", account=email)
            except Exception:
                log.warning("connected Google but couldn't read the Gmail profile", exc_info=True)
        return _back(ok=provider)

    @app.post("/connections/{provider}/disconnect")
    def connect_disconnect(provider: str):
        if provider not in hub.oauth.providers:
            raise HTTPException(404)
        hub.oauth.disconnect(provider)
        return RedirectResponse("/connections", status_code=303)

    # -- unknown / failed apps: registered last so real app routes win ---------
    @app.get("/apps/{app_id}", response_class=HTMLResponse)
    @app.get("/apps/{app_id}/{rest:path}", response_class=HTMLResponse)
    def app_fallback(request: Request, app_id: str, rest: str = ""):
        entry = hub.registry.apps.get(app_id)
        if entry is None or entry.status == "loaded":
            raise HTTPException(404)
        return render(request, "app_error.html", {"entry": entry}, status_code=503)


def _back(**params: str) -> RedirectResponse:
    return RedirectResponse("/connections?" + urlencode(params), status_code=303)


def _on_host(request: Request, hub: Hub) -> bool:
    """Google only accepts localhost/https redirect URIs, so consent must start there."""
    base_host = urlparse(hub.config.settings.hub_base_url).hostname
    return request.url.hostname == base_host


def _connections(hub: Hub) -> list[dict[str, Any]]:
    out = []
    for name, provider in hub.oauth.providers.items():
        users = [
            a
            for a in hub.registry.apps.values()
            if a.manifest
            and any(REQUIREMENTS[r].oauth_provider == name for r in a.manifest.requires if r in REQUIREMENTS)
        ]
        out.append(
            {
                "name": name,
                "label": provider.label,
                "configured": provider.configured,
                "scopes": provider.scopes,
                "redirect_uri": hub.oauth.redirect_uri(name),
                "used_by": users,
                **hub.oauth.info(name),
            }
        )
    return out


def __getattr__(name: str) -> Any:
    # `uvicorn hub.main:app` builds the app lazily, so importing this module
    # (e.g. in tests) doesn't start a hub against the real data/ directory.
    if name == "app":
        globals()["app"] = create_app()
        return globals()["app"]
    raise AttributeError(name)
