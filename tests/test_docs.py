"""Keep docs/plugin-guide.md in step with the code it documents."""

import dataclasses
import re

from fastapi.testclient import TestClient

from hub.plugin import AppContext, Manifest
from hub.registry import REQUIREMENTS
from hub.services.scheduler import AppScheduler
from hub.templating import HUB_TEMPLATES
from tests.conftest import ROOT, login

GUIDE = (ROOT / "docs" / "plugin-guide.md").read_text()


def test_every_appcontext_member_is_documented():
    public = {f.name for f in dataclasses.fields(AppContext) if not f.name.startswith("_")}
    public |= {n for n in vars(AppContext) if not n.startswith("_") and n not in public}
    public.add("gmail")
    missing = sorted(n for n in public if f"ctx.{n}" not in GUIDE)
    assert not missing, f"AppContext members missing from the guide: {missing}"


def test_every_manifest_field_is_documented():
    missing = [f.name for f in dataclasses.fields(Manifest) if f"{f.name}=" not in GUIDE]
    assert not missing, missing


def test_every_scheduler_method_is_documented():
    methods = [n for n in vars(AppScheduler) if not n.startswith("_")]
    missing = [m for m in methods if f"ctx.scheduler.{m}(" not in GUIDE]
    assert not missing, missing


def test_every_component_macro_is_documented():
    macros = re.findall(r"{% macro (\w+)\(", (HUB_TEMPLATES / "components.html").read_text())
    assert macros
    missing = [m for m in macros if f"ui.{m}(" not in GUIDE]
    assert not missing, missing


def test_every_requirement_is_documented():
    missing = [r for r in REQUIREMENTS if f'`"{r}"`' not in GUIDE]
    assert not missing, missing


def test_quick_start_plugin_loads_and_serves(hub_app, tmp_path):
    blocks = re.findall(r"<!-- example: ([\w/.-]+) -->\s*```\w*\n(.*?)```", GUIDE, re.S)
    assert len(blocks) >= 3
    apps = tmp_path / "apps"
    for rel, code in blocks:
        path = apps / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code)
    app = hub_app(apps)
    entry = app.state.hub.registry.apps["plants"]
    assert entry.status == "loaded", entry.error

    client = login(TestClient(app))
    assert "No plants yet." in client.get("/apps/plants/").text
    client.post("/apps/plants/plants", data={"name": "Fern"})
    assert "Fern" in client.get("/apps/plants/").text
