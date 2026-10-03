from fastapi.testclient import TestClient

from tests.conftest import FIXTURES, ROOT, login

APPS = FIXTURES / "apps"


def test_broken_apps_fail_alone(hub_app):
    app = hub_app(APPS, toml={"disabled": ["off"]})
    reg = app.state.hub.registry
    status = {k: v.status for k, v in reg.apps.items()}
    assert status == {
        "broken_import": "failed",
        "broken_setup": "failed",
        "good": "loaded",
        "off": "disabled",
    }
    assert "this_module_does_not_exist" in reg.apps["broken_import"].error
    assert "boom in setup" in reg.apps["broken_setup"].error
    # jobs from a setup that later raised are removed
    assert [j["id"] for j in app.state.hub.scheduler.jobs()] == []

    client = login(TestClient(app))
    assert client.get("/apps/good/").json() == {"app": "good", "things": 0}
    home = client.get("/")
    assert home.status_code == 200 and "failed to load" in home.text
    failed = client.get("/apps/broken_setup/")
    assert failed.status_code == 503 and "boom in setup" in failed.text
    assert client.get("/apps/nope/").status_code == 404
    admin = client.get("/admin")
    assert "boom in setup" in admin.text and "disabled" in admin.text
    assert "001_things.sql" in admin.text  # schema version applied by the host


def test_each_app_gets_its_own_database(hub_app, tmp_path):
    hub_app(APPS, toml={"disabled": ["off"]})
    assert (tmp_path / "data" / "apps" / "good.db").exists()
    assert not (tmp_path / "data" / "apps" / "broken_import.db").exists()


def test_copying_hello_adds_an_app_without_host_edits(hub_app, tmp_path):
    import shutil

    apps = tmp_path / "apps"
    shutil.copytree(ROOT / "apps" / "hello", apps / "hello")
    shutil.copytree(ROOT / "apps" / "hello", apps / "second")
    init = apps / "second" / "__init__.py"
    init.write_text(init.read_text(encoding="utf-8").replace('id="hello"', 'id="second"'), encoding="utf-8")
    app = hub_app(apps)
    assert {k: v.status for k, v in app.state.hub.registry.apps.items()} == {
        "hello": "loaded",
        "second": "loaded",
    }
    client = login(TestClient(app))
    client.post("/apps/second/notes", data={"body": "only in second"})
    assert "only in second" in client.get("/apps/second/").text
    assert "only in second" not in client.get("/apps/hello/").text
    assert (tmp_path / "data" / "apps" / "second.db").exists()


def test_manifest_id_must_match_folder(hub_app, tmp_path):
    import shutil

    apps = tmp_path / "apps"
    shutil.copytree(ROOT / "apps" / "hello", apps / "renamed")
    app = hub_app(apps)
    entry = app.state.hub.registry.apps["renamed"]
    assert entry.status == "failed" and "must match folder" in entry.error


def test_launcher_flags_missing_connection(client):
    home = client.get("/")
    assert "Finance" in home.text and "needs Gmail connection" in home.text
