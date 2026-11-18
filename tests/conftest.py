import base64
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

import pytest
from fake_google import CLIENT_ID, CLIENT_SECRET, FakeGoogle

from mailsorter.accounts import AccountStore
from mailsorter.config import Settings
from mailsorter.db import Database
from mailsorter.secrets_store import TokenVault
from mailsorter.web import create_app

START_TIME = 1_763_000_000.0
SECRET = b"s" * 32
TOKEN_KEY = base64.urlsafe_b64encode(b"t" * 32)
DREW = "drew@example.com"
FRIEND = "friend@example.com"
PUBLIC_URL = "http://localhost:8101"
CADDY_ON_THIS_HOST = ("127.0.0.1", "::1")


class FakeClock:
    def __init__(self, start: float = START_TIME):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Sleeper:
    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.clock.advance(seconds)


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def google_redirect(url: str) -> str:
    try:
        build_opener(_NoRedirects).open(url, timeout=10)
    except HTTPError as answer:
        with answer:
            return answer.headers["Location"]
    raise AssertionError(f"the fake Google did not redirect from {url}")


def path_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.path}?{parts.query}" if parts.query else parts.path


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def sleeper(clock):
    return Sleeper(clock)


@pytest.fixture
def google(clock):
    fake = FakeGoogle(clock)
    yield fake
    fake.close()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "data", public_url=PUBLIC_URL, google_client_id=CLIENT_ID,
                    google_client_secret=CLIENT_SECRET, trusted_proxies=CADDY_ON_THIS_HOST)


@pytest.fixture
def store(tmp_path, clock) -> AccountStore:
    return AccountStore(Database(Path(tmp_path) / "data" / "mail-sorter.db"), TokenVault(TOKEN_KEY), clock=clock)


def make_app(settings, store, google, clock, sleeper, **extra):
    app = create_app(settings, store, SECRET, endpoints=google.endpoints, sleep=sleeper, clock=clock, monotonic=clock,
                     **extra)
    app.testing = True
    return app


@pytest.fixture
def app(settings, store, google, clock, sleeper):
    return make_app(settings, store, google, clock, sleeper)


class Browser:
    def __init__(self, app, google: FakeGoogle):
        self.client = app.test_client()
        self.google = google
        self.csrf = ""

    def sign_in(self, account: str = DREW):
        self.google.next_account = account
        start = self.client.get("/auth/google")
        assert start.status_code == 303, start.data
        callback = google_redirect(start.headers["Location"])
        answer = self.client.get(path_of(callback))
        if answer.status_code == 303 and answer.headers["Location"] == "/":
            self.csrf = self.client.get("/api/session").get_json()["csrfToken"]
        return answer

    def get(self, path: str):
        return self.client.get(path)

    def post(self, path: str, headers: dict | None = None, json=None):
        return self.client.post(path, headers={"X-CSRF-Token": self.csrf, **(headers or {})}, json=json)

    def undo(self, message_id: str, action: str):
        return self.post("/api/undo", json={"id": message_id, "action": action})

    def next(self, view: str = "inbox") -> dict:
        answer = self.client.get(f"/api/next?view={view}")
        assert answer.status_code == 200, answer.get_json()
        return answer.get_json()

    def next_subject(self, view: str = "inbox") -> str | None:
        message = self.next(view)["message"]
        return message["subject"] if message else None

    def act(self, message_id: str, action: str):
        return self.post(f"/api/messages/{message_id}/{action}")


@pytest.fixture
def browser(app, google):
    return Browser(app, google)


@pytest.fixture
def drew(browser):
    answer = browser.sign_in(DREW)
    assert answer.headers["Location"] == "/", answer.headers.get("Location")
    return browser
