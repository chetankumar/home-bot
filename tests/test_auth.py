from fastapi.testclient import TestClient


def test_redirects_to_login(hub_app):
    c = TestClient(hub_app())
    r = c.get("/apps/hello/?x=1", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Fapps%2Fhello%2F%3Fx%3D1"


def test_htmx_gets_401_with_redirect_header(hub_app):
    c = TestClient(hub_app())
    r = c.post("/apps/hello/notes", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.status_code == 401 and r.headers["HX-Redirect"].startswith("/login")


def test_public_paths(hub_app):
    c = TestClient(hub_app())
    assert c.get("/login").status_code == 200
    assert c.get("/static/hub.css").status_code == 200
    assert c.get("/healthz").json() == {"ok": True}


def test_login_logout(hub_app):
    c = TestClient(hub_app())
    bad = c.post("/login", data={"password": "nope", "next": "/admin"}, follow_redirects=False)
    assert bad.status_code == 401
    ok = c.post("/login", data={"password": "pw", "next": "/admin"}, follow_redirects=False)
    assert ok.status_code == 303 and ok.headers["location"] == "/admin"
    assert c.get("/admin").status_code == 200
    c.post("/logout")
    assert c.get("/admin", follow_redirects=False).status_code == 303


def test_open_redirect_blocked(hub_app):
    c = TestClient(hub_app())
    r = c.post("/login", data={"password": "pw", "next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_no_password_means_no_login(hub_app):
    c = TestClient(hub_app(hub_password=""))
    r = c.post("/login", data={"password": "", "next": "/"}, follow_redirects=False)
    assert r.status_code == 401
