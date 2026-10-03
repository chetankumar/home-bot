from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from hub.services.db import hub_database
from hub.services.gmail import GmailClient, extract_body
from hub.services.oauth import (
    NotConnected,
    OAuthService,
    TokenStore,
    fernet_from_secret,
    google_provider,
)


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def setup(tmp_path):
    requests = []
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/token":
            form = parse_qs(request.content.decode())
            state["n"] += 1
            if form["grant_type"] == ["authorization_code"]:
                return httpx.Response(200, json={"access_token": "at-1", "refresh_token": "rt-secret", "expires_in": 3600})
            if form["refresh_token"] == ["rt-secret"]:
                return httpx.Response(200, json={"access_token": f"at-{state['n']}", "expires_in": 3600})
            return httpx.Response(400, json={"error": "invalid_grant"})
        if request.url.host == "gmail.googleapis.com":
            return httpx.Response(200, json={"emailAddress": "me@example.com", "auth": request.headers["authorization"]})
        return httpx.Response(404)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    db = hub_database(tmp_path)
    clock = Clock()
    svc = OAuthService(TokenStore(db, fernet_from_secret("k1")), "http://localhost:8000", http=http, clock=clock)
    p = google_provider("cid", "csecret", ["scope-a"])
    p.token_url = "https://oauth2.example/token"
    svc.register(p)
    return svc, db, clock, requests, http


def test_authorization_url(setup):
    svc, *_ = setup
    url = urlparse(svc.authorization_url("google", "st"))
    q = parse_qs(url.query)
    assert q["redirect_uri"] == ["http://localhost:8000/connections/google/callback"]
    assert q["access_type"] == ["offline"] and q["state"] == ["st"] and q["scope"] == ["scope-a"]


def test_tokens_are_encrypted_at_rest(setup):
    svc, db, *_ = setup
    svc.exchange_code("google", "code-1")
    with db() as conn:
        blob = conn.execute("SELECT data FROM oauth_tokens").fetchone()[0]
    assert b"rt-secret" not in blob and b"at-1" not in blob
    assert svc.store.get("google")["refresh_token"] == "rt-secret"
    # a different secret key can't read it
    other = TokenStore(db, fernet_from_secret("k2"))
    assert other.get("google") is None


def test_refresh_on_expiry_keeps_refresh_token(setup):
    svc, _, clock, requests, _ = setup
    svc.exchange_code("google", "code-1")
    assert svc.access_token("google") == "at-1"
    assert len(requests) == 1  # still fresh, no refresh
    clock.t += 3600 - 30  # inside the refresh margin
    assert svc.access_token("google") == "at-2"
    assert svc.store.get("google")["refresh_token"] == "rt-secret"
    assert svc.is_connected("google")


def test_not_connected(setup):
    svc, *_ = setup
    with pytest.raises(NotConnected):
        svc.access_token("google")
    svc.store.put("google", {"access_token": "x", "refresh_token": "revoked", "expires_at": 0})
    with pytest.raises(NotConnected):
        svc.access_token("google")


def test_gmail_uses_bearer_token(setup):
    svc, _, _, _, http = setup
    svc.exchange_code("google", "code-1")
    assert GmailClient(svc, http=http).profile()["auth"] == "Bearer at-1"


def test_gmail_body_prefers_plain_text_and_strips_html():
    import base64

    def b64(s):
        return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")

    html_only = {"mimeType": "text/html", "body": {"data": b64("<style>x{}</style><p>Rs.10&nbsp;debited</p><br>Thanks")}}
    assert extract_body(html_only) == "Rs.10 debited\n\nThanks"
    multi = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": b64("plain  text")}},
            {"mimeType": "text/html", "body": {"data": b64("<b>html</b>")}},
        ],
    }
    assert extract_body(multi) == "plain text"


def test_connections_page_and_state_check(hub_app):
    from fastapi.testclient import TestClient

    from tests.conftest import login

    app = hub_app(google_client_id="cid", google_client_secret="cs")
    c = login(TestClient(app, base_url="http://localhost:8000"))
    assert "Connect Google" in c.get("/connections").text
    r = c.get("/connections/google/start", follow_redirects=False)
    assert r.headers["location"].startswith("https://accounts.google.com/")
    bad = c.get("/connections/google/callback?code=x&state=wrong", follow_redirects=False)
    assert "state+mismatch" in bad.headers["location"]

    # Off the host (LAN IP), connecting is refused with an explanation.
    lan = login(TestClient(app, base_url="http://192.168.1.20:8000"))
    r = lan.get("/connections/google/start", follow_redirects=False)
    assert "host+PC" in r.headers["location"]
