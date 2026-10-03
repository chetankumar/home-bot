"""Jinja environments: each app gets its own templates/ first, then the hub's."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape

HUB_TEMPLATES = Path(__file__).resolve().parent / "templates"


def make_templates(
    dirs: list[Path], tz: ZoneInfo, globals_: dict[str, Any] | None = None
) -> Jinja2Templates:
    loaders = [FileSystemLoader(str(d)) for d in dirs if d.is_dir()]
    env = Environment(
        loader=ChoiceLoader(loaders + [FileSystemLoader(str(HUB_TEMPLATES))]),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["dt"] = _dt_filter(tz)
    env.globals.update(globals_ or {})
    return Jinja2Templates(env=env)


def _dt_filter(tz: ZoneInfo) -> Callable[..., str]:
    def dt(value: Any, fmt: str = "%d %b %Y, %H:%M") -> str:
        """Format a datetime / ISO string / epoch in the hub timezone."""
        if value in (None, ""):
            return "—"
        if isinstance(value, int | float):
            value = datetime.fromtimestamp(value, tz)
        elif isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                return value
        if value.tzinfo is not None:
            value = value.astimezone(tz)
        return value.strftime(fmt)

    return dt
