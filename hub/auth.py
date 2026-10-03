"""Single-user password login with a signed session cookie."""

from __future__ import annotations

import hmac
import time
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

PUBLIC_PREFIXES = ("/login", "/static/", "/healthz")


class AuthMiddleware:
    """Everything except /login, /static and /healthz needs a logged-in session.

    Must sit inside SessionMiddleware (i.e. be added before it).
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        if path.startswith(PUBLIC_PREFIXES) or scope.get("session", {}).get("auth"):
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        target = path + (("?" + scope["query_string"].decode()) if scope["query_string"] else "")
        login = "/login?next=" + quote(target, safe="")
        if headers.get("hx-request"):
            # HTMX would swap the login page into a fragment; ask it to navigate instead.
            response: Response = Response(status_code=401, headers={"HX-Redirect": login})
        else:
            response = RedirectResponse(login, status_code=303)
        await response(scope, receive, send)


def safe_next(target: str | None) -> str:
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return "/"


def build_router(password_getter) -> APIRouter:
    router = APIRouter()

    @router.get("/login")
    def login_page(request: Request, next: str = "/"):
        hub = request.app.state.hub
        return hub.templates.TemplateResponse(
            request,
            "login.html",
            {"next": safe_next(next), "error": None, "configured": bool(password_getter())},
        )

    @router.post("/login")
    def login(request: Request, password: str = Form(""), next: str = Form("/")):
        hub = request.app.state.hub
        expected = password_getter()
        if expected and hmac.compare_digest(password.encode(), expected.encode()):
            request.session.clear()
            request.session["auth"] = True
            return RedirectResponse(safe_next(next), status_code=303)
        time.sleep(1)  # slow down guessing; runs in the threadpool
        return hub.templates.TemplateResponse(
            request,
            "login.html",
            {
                "next": safe_next(next),
                "error": "Wrong password." if expected else "HUB_PASSWORD is not set in .env.",
                "configured": bool(expected),
            },
            status_code=401,
        )

    @router.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    return router
