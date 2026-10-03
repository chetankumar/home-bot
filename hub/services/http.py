"""Named outbound API connections with static credentials, from hub.toml.

    [connections.weather]
    base_url = "https://api.openweathermap.org/data/2.5"
    auth = { type = "query", name = "appid", env = "OPENWEATHER_API_KEY" }
    headers = { Accept = "application/json" }
    timeout = 20

auth types: "bearer" (Authorization: Bearer), "header" (custom header name),
"query" (query parameter), "basic" (env = user, password_env = password).
"""

from __future__ import annotations

from typing import Any

import httpx

from hub.config import Config


class UnknownConnection(KeyError):
    pass


class HttpConnections:
    def __init__(self, config: Config):
        self._config = config
        self.specs: dict[str, dict[str, Any]] = dict(config.connections)

    def client(self, name: str, **overrides: Any) -> httpx.Client:
        spec = self.specs.get(name)
        if spec is None:
            raise UnknownConnection(f"no [connections.{name}] in hub.toml")
        headers = dict(spec.get("headers", {}))
        params: dict[str, str] = {}
        auth: Any = None
        a = spec.get("auth") or {}
        kind = a.get("type")
        value = self._config.secret(a["env"]) if a.get("env") else a.get("value", "")
        if kind == "bearer":
            headers["Authorization"] = f"Bearer {value}"
        elif kind == "header":
            headers[a["name"]] = value
        elif kind == "query":
            params[a["name"]] = value
        elif kind == "basic":
            auth = (value, self._config.secret(a.get("password_env", "")))
        elif kind is not None:
            raise ValueError(f"unknown auth type {kind!r} for connection {name!r}")
        kwargs: dict[str, Any] = {
            "base_url": spec.get("base_url", ""),
            "headers": headers,
            "params": params,
            "auth": auth,
            "timeout": spec.get("timeout", 30),
        }
        kwargs.update(overrides)
        return httpx.Client(**kwargs)


class AppHttp:
    """Per-app view. If [apps.<id>] lists `connections`, only those are allowed."""

    def __init__(self, connections: HttpConnections, app_id: str, allowed: list[str] | None):
        self._connections = connections
        self.app_id = app_id
        self.allowed = allowed

    def client(self, name: str, **overrides: Any) -> httpx.Client:
        if self.allowed is not None and name not in self.allowed:
            raise PermissionError(f"app '{self.app_id}' may not use connection '{name}'")
        return self._connections.client(name, **overrides)
