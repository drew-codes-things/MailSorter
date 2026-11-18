from dataclasses import replace

import pytest
from conftest import DREW, PUBLIC_URL, Browser, make_app

from mailsorter.config import BAD_CLIENT_ID_TEXT
from mailsorter.web import SESSION_COOKIE

TRACKED_EMAIL = ('<p>Hello <b>Drew</b></p><script>parent.location="https://evil.example"</script>'
                 '<img src="https://track.example/pixel.gif?u=drew"><img src="https://shop.example/logo.png">')


@pytest.fixture
def not_set_up(settings, store, google, clock, sleeper):
    return make_app(replace(settings, google_client_id="", google_client_secret=""), store, google, clock, sleeper).test_client()


def test_without_a_google_client_the_page_says_not_set_up_yet(not_set_up):
    page = not_set_up.get("/")
    assert page.status_code == 200
    assert b"Mail Sorter is not set up yet" in page.data
    assert b"GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are empty" in page.data


def test_without_a_google_client_nothing_else_breaks(not_set_up):
    assert not_set_up.get("/api/health").get_json() == {"ok": True, "googleReady": False}
    assert not_set_up.get("/auth/google").headers["Location"] == "/"
    assert not_set_up.get("/auth/callback?code=x&state=y").headers["Location"] == "/"
    answer = not_set_up.get("/api/next")
    assert answer.status_code == 503 and answer.get_json()["notSetUp"] is True


def test_a_mistyped_client_id_shows_what_is_wrong(settings, store, google, clock, sleeper):
    app = make_app(replace(settings, google_client_id="paste-error"), store, google, clock, sleeper)
    assert BAD_CLIENT_ID_TEXT.encode() in app.test_client().get("/").data


def test_signed_out_visitors_get_the_sign_in_page(browser):
    page = browser.get("/")
    assert page.status_code == 200
    assert b'href="/auth/google"' in page.data and b"Sign in with Google" in page.data


def test_the_sign_in_page_shows_only_known_reasons(browser):
    assert b"Your Google sign-in ran out" in browser.get("/?why=expired").data
    page = browser.get("/?why=<script>alert(1)</script>")
    assert b"<script>" not in page.data and b"alert" not in page.data


def test_signed_in_visitors_get_the_sorter_with_their_address_escaped(drew):
    page = drew.get("/")
    assert b'<script src="/static/app.js" defer></script>' in page.data
    assert f"Signed in as {DREW}".encode() in page.data


def test_pages_carry_a_strict_policy(browser):
    page = browser.get("/")
    policy = page.headers["Content-Security-Policy"]
    assert "script-src 'self'" in policy and "frame-ancestors 'none'" in policy and "'unsafe-inline'" not in policy
    assert page.headers["X-Frame-Options"] == "DENY"
    assert page.headers["Referrer-Policy"] == "no-referrer"


def test_api_without_a_session_answers_401_and_asks_for_sign_in(browser):
    for path in ("/api/session", "/api/next", "/api/counts", "/api/messages/abc/body"):
        answer = browser.get(path)
        assert answer.status_code == 401 and answer.get_json()["signIn"] is True


def test_a_made_up_session_cookie_is_refused_and_cleared(browser):
    browser.client.set_cookie(SESSION_COOKIE, "made-up")
    assert browser.get("/api/next").status_code == 401
    assert browser.client.get_cookie(SESSION_COOKIE) is None


def test_writes_need_the_csrf_token_and_the_same_origin(drew, google):
    message_id = google.add_message(DREW, "x")
    good = drew.csrf
    drew.csrf = ""
    assert drew.act(message_id, "no").status_code == 403
    drew.csrf = good
    assert drew.post(f"/api/messages/{message_id}/no", headers={"Origin": "https://evil.example"}).status_code == 403
    assert "TRASH" not in google.mailbox(DREW).names_of(message_id)
    assert drew.post(f"/api/messages/{message_id}/no", headers={"Origin": PUBLIC_URL}).status_code == 200


def test_the_email_body_is_cleaned_and_framed_with_images_blocked(drew, google):
    message_id = google.add_message(DREW, "Sale", html=TRACKED_EMAIL)
    assert drew.next()["message"]["remoteImages"] == 2
    body = drew.get(f"/api/messages/{message_id}/body")
    assert body.status_code == 200
    assert b"<b>Drew</b>" in body.data and b"<script" not in body.data
    policy = body.headers["Content-Security-Policy"]
    assert "img-src data:" in policy and "https:" not in policy
    assert "default-src 'none'" in policy and "sandbox allow-popups allow-popups-to-escape-sandbox" in policy
    assert "script-src" not in policy
    assert body.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert body.headers["Cache-Control"] == "no-store"


def test_images_load_only_when_asked(drew, google):
    message_id = google.add_message(DREW, "Sale", html=TRACKED_EMAIL)
    shown = drew.get(f"/api/messages/{message_id}/body?images=1")
    assert "img-src data: https:" in shown.headers["Content-Security-Policy"]


def test_a_plain_text_email_is_shown_as_text(drew, google):
    message_id = google.add_message(DREW, "Note", text="<b>not bold</b>\nsee https://example.com")
    body = drew.get(f"/api/messages/{message_id}/body").data
    assert b"&lt;b&gt;not bold&lt;/b&gt;" in body and b'href="https://example.com"' in body


def test_a_bad_message_id_in_the_body_path_is_refused(drew):
    assert drew.get("/api/messages/..%2Fprofile/body").status_code == 404


def test_health_needs_no_sign_in(browser):
    assert browser.get("/api/health").get_json() == {"ok": True, "googleReady": True}


def test_static_files_are_served(browser):
    assert browser.get("/static/app.css").status_code == 200
    assert browser.get("/static/../web.py").status_code == 404


def test_counts_alone_can_be_read(drew, google):
    google.add_message(DREW, "x")
    assert drew.get("/api/counts").get_json() == {"left": 1, "later": 0, "more": False}


def test_sessions_for_two_accounts_live_side_by_side(app, google, drew):
    friend = Browser(app, google)
    friend.sign_in("friend@example.com")
    assert drew.get("/api/session").get_json()["account"] == DREW
    assert friend.get("/api/session").get_json()["account"] == "friend@example.com"


def test_the_email_frame_may_load_no_fonts(drew, google):
    message_id = google.add_message(DREW, "Fonts", html="<p>x</p>")
    for path in (f"/api/messages/{message_id}/body", f"/api/messages/{message_id}/body?images=1"):
        assert "font-src 'none'" in drew.get(path).headers["Content-Security-Policy"]


def test_a_message_id_with_a_trailing_newline_is_refused(drew, google):
    google.add_message(DREW, "x")
    assert drew.get("/api/messages/abc%0A/body").status_code == 404
    assert drew.post("/api/messages/abc%0A/yes").status_code == 404
