from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

from conftest import DREW, FRIEND, PUBLIC_URL, SECRET, Browser, google_redirect, make_app, path_of
from fake_google import CLIENT_ID

from mailsorter.accounts import SIGN_IN_SECONDS
from mailsorter.google import GMAIL_SCOPE
from mailsorter.secrets_store import SignInSeal
from mailsorter.web import SESSION_COOKIE, SIGN_IN_COOKIE


def authorize_query(start) -> dict:
    return {key: values[0] for key, values in parse_qs(urlsplit(start.headers["Location"]).query).items()}


def test_sign_in_asks_google_for_gmail_modify_only_with_pkce_and_state(browser, google):
    start = browser.get("/auth/google")
    assert start.status_code == 303
    query = authorize_query(start)
    assert start.headers["Location"].startswith(google.endpoints.authorize + "?")
    assert query["scope"] == GMAIL_SCOPE
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == f"{PUBLIC_URL}/auth/callback"
    assert query["response_type"] == "code"
    assert query["code_challenge_method"] == "S256"
    assert len(query["code_challenge"]) == 43
    assert len(query["state"]) >= 40
    assert (query["access_type"], query["prompt"]) == ("offline", "consent")
    cookie = browser.client.get_cookie(SIGN_IN_COOKIE)
    assert cookie.http_only and query["state"] not in cookie.value
    assert SignInSeal(SECRET, google.clock).open(cookie.value, SIGN_IN_SECONDS)[0] == query["state"]


def test_a_full_sign_in_sets_a_session_and_stores_tokens_encrypted(browser, google, store):
    answer = browser.sign_in(DREW)
    assert (answer.status_code, answer.headers["Location"]) == (303, "/")
    session = browser.client.get_cookie(SESSION_COOKIE)
    assert session.http_only and session.same_site == "Lax"
    assert browser.get("/api/session").get_json()["account"] == DREW
    with store.database.connect() as connection:
        row = connection.execute("SELECT refresh_token, access_token FROM accounts").fetchone()
    refresh_token = next(iter(google.refresh_tokens))
    assert refresh_token.encode() not in bytes(row["refresh_token"])
    assert store.tokens(DREW).refresh_token == refresh_token
    assert browser.client.get_cookie(SIGN_IN_COOKIE) is None


def test_the_session_id_is_stored_only_as_a_hash(drew, store):
    session_id = drew.client.get_cookie(SESSION_COOKIE).value
    with store.database.connect() as connection:
        stored = [row["id_hash"] for row in connection.execute("SELECT id_hash FROM sessions")]
    assert session_id not in stored and len(stored) == 1


def test_a_callback_with_a_state_that_does_not_match_the_cookie_is_refused(browser, google):
    start = browser.get("/auth/google")
    callback = google_redirect(start.headers["Location"])
    forged = callback.replace(authorize_query(start)["state"], "x" * 43)
    answer = browser.get(path_of(forged))
    assert answer.headers["Location"] == "/?why=retry"
    assert browser.get("/api/session").status_code == 401


def test_a_callback_without_the_sign_in_cookie_is_refused(browser, app, google):
    start = browser.get("/auth/google")
    callback = google_redirect(start.headers["Location"])
    other_browser = app.test_client()
    answer = other_browser.get(path_of(callback))
    assert answer.headers["Location"] == "/?why=retry"
    assert google.refresh_tokens == {}


def test_a_replayed_callback_signs_nobody_in_again(browser, app, google):
    start = browser.get("/auth/google")
    callback = path_of(google_redirect(start.headers["Location"]))
    cookie = browser.client.get_cookie(SIGN_IN_COOKIE).value
    assert browser.get(callback).headers["Location"] == "/"
    replay = app.test_client()
    replay.set_cookie(SIGN_IN_COOKIE, cookie)
    assert replay.get(callback).headers["Location"] == "/?why=google"
    assert replay.get("/api/session").status_code == 401
    assert len(google.refresh_tokens) == 1


def test_an_old_sign_in_link_is_refused(browser, google, clock):
    start = browser.get("/auth/google")
    callback = google_redirect(start.headers["Location"])
    clock.advance(11 * 60)
    assert browser.get(path_of(callback)).headers["Location"] == "/?why=retry"


def test_pkce_verifier_is_checked_by_the_token_service(browser, google, clock):
    start = browser.get("/auth/google")
    callback = google_redirect(start.headers["Location"])
    wrong = SignInSeal(SECRET, clock).seal(authorize_query(start)["state"], "wrong-verifier-wrong-verifier-wrong")
    browser.client.set_cookie(SIGN_IN_COOKIE, wrong)
    assert browser.get(path_of(callback)).headers["Location"] == "/?why=google"


def test_a_sign_in_cookie_that_was_changed_or_made_elsewhere_is_refused(browser, google, clock):
    start = browser.get("/auth/google")
    callback = google_redirect(start.headers["Location"])
    state = authorize_query(start)["state"]
    sealed = browser.client.get_cookie(SIGN_IN_COOKIE).value
    for planted in (sealed[:-4] + "AAAA", SignInSeal(b"o" * 32, clock).seal(state, "x" * 43), state):
        browser.client.set_cookie(SIGN_IN_COOKIE, planted)
        assert browser.get(path_of(callback)).headers["Location"] == "/?why=retry"
    assert browser.get("/api/session").status_code == 401


def test_cancelling_at_google_signs_nobody_in(browser, google):
    google.deny = True
    answer = browser.sign_in(DREW)
    assert answer.headers["Location"] == "/?why=denied"
    assert browser.get("/api/session").status_code == 401


def test_unticking_the_gmail_box_is_refused_and_the_grant_revoked(browser, google):
    google.granted_scope = "openid"
    answer = browser.sign_in(DREW)
    assert answer.headers["Location"] == "/?why=scope"
    assert google.revoked and google.refresh_tokens == {}
    assert browser.get("/api/session").status_code == 401


def test_a_grant_without_a_refresh_token_is_not_kept(browser, google, store):
    google.give_refresh_token = False
    assert browser.sign_in(DREW).headers["Location"] == "/?why=google"
    assert store.tokens(DREW) is None


def test_allowed_accounts_limits_who_can_sign_in(settings, store, google, clock, sleeper):
    app = make_app(replace(settings, allowed_accounts=(DREW,)), store, google, clock, sleeper)
    outsider = Browser(app, google)
    assert outsider.sign_in(FRIEND).headers["Location"] == "/?why=account"
    assert store.tokens(FRIEND) is None and google.refresh_tokens == {}
    assert Browser(app, google).sign_in(DREW).headers["Location"] == "/"


def test_sign_out_revokes_the_token_and_deletes_it(drew, google, store):
    refresh_token = store.tokens(DREW).refresh_token
    answer = drew.post("/auth/signout")
    assert answer.status_code == 200
    assert answer.get_json() == {"signedOut": True, "revoked": True}
    assert refresh_token in google.revoked
    assert store.tokens(DREW) is None
    assert drew.get("/api/session").status_code == 401
    with store.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_sign_out_still_deletes_the_tokens_when_google_cannot_be_reached(drew, google, store):
    google.close()
    assert drew.post("/auth/signout").get_json() == {"signedOut": True, "revoked": False}
    assert store.tokens(DREW) is None


def test_sign_out_needs_the_csrf_token(drew, store):
    drew.csrf = "0" * 64
    assert drew.post("/auth/signout").status_code == 403
    assert store.tokens(DREW) is not None


def test_signing_in_again_replaces_the_tokens(drew, google, store):
    first = store.tokens(DREW).refresh_token
    drew.sign_in(DREW)
    assert store.tokens(DREW).refresh_token != first


def test_the_session_ends_after_thirty_days(drew, clock):
    clock.advance(31 * 24 * 3600)
    assert drew.get("/api/session").status_code == 401


def test_a_callback_meant_for_another_browser_is_refused(browser, app, google):
    victim_start = browser.get("/auth/google")
    victim_callback = google_redirect(victim_start.headers["Location"])
    attacker = Browser(app, google)
    attacker.get("/auth/google")
    answer = attacker.get(path_of(victim_callback))
    assert answer.headers["Location"] == "/?why=retry"
    assert attacker.get("/api/session").status_code == 401
