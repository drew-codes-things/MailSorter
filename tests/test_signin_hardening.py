from dataclasses import replace

from conftest import DREW, FRIEND, SECRET, Browser, google_redirect, make_app, path_of

from mailsorter.google import GoogleUnreachable, HttpAnswer, TokenGrant, send
from mailsorter.web import SESSION_COOKIE, create_app

FLOOD_HITS = 500
VICTIM_ADDRESS = "198.51.100.7"
ATTACKER_ADDRESS = "203.0.113.9"
LAN_PEER = "172.16.1.50"
HTTPS_URL = "https://mail.example.test"


def from_address(client, address: str):
    client.environ_base["HTTP_CF_CONNECTING_IP"] = address
    return client


def test_a_flood_of_anonymous_sign_ins_does_not_cancel_one_under_way(app, google, clock):
    victim = Browser(app, google)
    from_address(victim.client, VICTIM_ADDRESS)
    google.next_account = DREW
    callback = google_redirect(victim.client.get("/auth/google").headers["Location"])
    clock.advance(1)
    attacker = from_address(app.test_client(), ATTACKER_ADDRESS)
    for _ in range(FLOOD_HITS):
        attacker.get("/auth/google")
    clock.advance(1)
    assert victim.client.get(path_of(callback)).headers["Location"] == "/"
    assert victim.client.get("/api/session").get_json()["account"] == DREW


def test_sign_in_requests_are_limited_per_address(app, google, clock):
    attacker = from_address(app.test_client(), ATTACKER_ADDRESS)
    answers = [attacker.get("/auth/google").headers["Location"] for _ in range(31)]
    assert all(location.startswith(google.endpoints.authorize) for location in answers[:30])
    assert answers[30] == "/?why=slow"
    neighbour = from_address(app.test_client(), VICTIM_ADDRESS)
    assert neighbour.get("/auth/google").headers["Location"].startswith(google.endpoints.authorize)
    clock.advance(61)
    assert attacker.get("/auth/google").headers["Location"].startswith(google.endpoints.authorize)


def test_the_cloudflare_address_counts_only_when_caddy_on_this_host_sent_the_request(app, google):
    lan = app.test_client()
    lan.environ_base["REMOTE_ADDR"] = LAN_PEER
    for number in range(30):
        lan.environ_base["HTTP_CF_CONNECTING_IP"] = f"198.51.100.{number}"
        assert lan.get("/auth/google").headers["Location"].startswith(google.endpoints.authorize)
    lan.environ_base["HTTP_CF_CONNECTING_IP"] = "198.51.100.200"
    assert lan.get("/auth/google").headers["Location"] == "/?why=slow"


def test_signing_out_is_limited_too(drew):
    from_address(drew.client, ATTACKER_ADDRESS)
    for _ in range(30):
        drew.client.get("/auth/google")
    answer = drew.post("/auth/signout")
    assert answer.status_code == 429 and answer.headers["Retry-After"] == "60"


def test_an_account_taken_off_the_allow_list_is_signed_out_at_start_up(settings, store, google, clock, sleeper):
    open_app = make_app(settings, store, google, clock, sleeper)
    friend, drew = Browser(open_app, google), Browser(open_app, google)
    friend.sign_in(FRIEND)
    drew.sign_in(DREW)
    locked = make_app(replace(settings, allowed_accounts=(DREW,)), store, google, clock, sleeper)
    assert store.tokens(FRIEND) is None and store.tokens(DREW) is not None
    stale = locked.test_client()
    stale.set_cookie(SESSION_COOKIE, friend.client.get_cookie(SESSION_COOKIE).value)
    answer = stale.get("/api/next")
    assert answer.status_code == 401 and answer.get_json()["signIn"] is True
    still = locked.test_client()
    still.set_cookie(SESSION_COOKIE, drew.client.get_cookie(SESSION_COOKIE).value)
    assert still.get("/api/session").get_json()["account"] == DREW


def test_the_allow_list_is_checked_on_every_request(settings, store, google, clock, sleeper):
    locked = make_app(replace(settings, allowed_accounts=(DREW,)), store, google, clock, sleeper)
    store.save_sign_in(FRIEND, TokenGrant("access", clock() + 3600, "refresh", frozenset()))
    planted = store.start_session(FRIEND)
    client = locked.test_client()
    client.set_cookie(SESSION_COOKIE, planted)
    assert client.get("/api/session").status_code == 401
    assert store.session_email(planted) is None


def test_a_refused_old_refresh_token_keeps_a_sign_in_made_meanwhile(settings, store, google, clock, sleeper):
    made_meanwhile = {}

    def transport(method, url, headers=None, body=None):
        if url != google.endpoints.token or b"grant_type=refresh_token" not in (body or b"") or made_meanwhile:
            return send(method, url, headers, body)
        google.refresh_tokens["fresh-refresh"] = DREW
        store.save_sign_in(DREW, TokenGrant(google._issue_access(DREW), clock() + 3600, "fresh-refresh", frozenset()))
        made_meanwhile["session"] = store.start_session(DREW)
        return HttpAnswer(400, {}, b'{"error": "invalid_grant"}')

    app = create_app(settings, store, SECRET, endpoints=google.endpoints, transport=transport, sleep=sleeper,
                     clock=clock, monotonic=clock)
    laptop = Browser(app, google)
    laptop.sign_in(DREW)
    google.add_message(DREW, "Mail", html="<p>x</p>")
    google.expire_access_tokens()
    answer = laptop.get("/api/next?view=inbox")
    assert answer.status_code == 200 and answer.get_json()["message"]["subject"] == "Mail"
    assert store.session_email(made_meanwhile["session"]) == DREW
    assert store.tokens(DREW).refresh_token == "fresh-refresh"


def test_signing_in_again_revokes_the_earlier_refresh_token(drew, google, store):
    first = store.tokens(DREW).refresh_token
    google.add_message(DREW, "Mail", html="<p>x</p>")
    assert drew.sign_in(DREW).headers["Location"] == "/"
    assert first in google.revoked and first not in google.refresh_tokens
    assert drew.next()["message"]["subject"] == "Mail"


def test_when_revoking_ends_the_whole_grant_the_person_signs_in_once_more(drew, google, store):
    google.revoke_ends_grant = True
    assert drew.sign_in(DREW).headers["Location"] == "/?why=again"
    assert store.tokens(DREW) is None
    assert drew.sign_in(DREW).headers["Location"] == "/"
    assert drew.next()["message"] is None


def test_the_sign_out_page_tells_the_truth_when_the_revoke_failed(settings, store, google, clock, sleeper):
    def transport(method, url, headers=None, body=None):
        if url == google.endpoints.revoke:
            raise GoogleUnreachable("could not reach Google")
        return send(method, url, headers, body)

    app = create_app(settings, store, SECRET, endpoints=google.endpoints, transport=transport, sleep=sleeper,
                     clock=clock, monotonic=clock)
    browser = Browser(app, google)
    browser.sign_in(DREW)
    assert browser.post("/auth/signout").get_json() == {"signedOut": True, "revoked": False}
    page = browser.get("/?why=notrevoked").get_data(as_text=True)
    assert "could not be reached to take the access back" in page
    assert "https://myaccount.google.com/connections" in page
    assert "was taken back" not in page


def https_app(settings, store, google, clock, sleeper):
    return make_app(replace(settings, public_url=HTTPS_URL), store, google, clock, sleeper)


def set_cookies(answer) -> list[str]:
    return answer.headers.getlist("Set-Cookie")


def test_on_https_cookies_are_host_only_and_hsts_is_sent(settings, store, google, clock, sleeper):
    client = https_app(settings, store, google, clock, sleeper).test_client()
    start = client.get("/auth/google", base_url=HTTPS_URL)
    [sign_in_cookie] = set_cookies(start)
    assert sign_in_cookie.startswith("__Host-mail_sorter_signin=")
    assert "Secure" in sign_in_cookie and "Path=/" in sign_in_cookie and "Domain" not in sign_in_cookie
    assert start.headers["Strict-Transport-Security"] == "max-age=31536000"
    google.next_account = DREW
    done = client.get(path_of(google_redirect(start.headers["Location"])), base_url=HTTPS_URL)
    assert done.headers["Location"] == "/"
    session = [value for value in set_cookies(done) if value.startswith("__Host-mail_sorter_session=")]
    assert session and "Secure" in session[0] and "Path=/" in session[0] and "Domain" not in session[0]
    assert client.get("/api/session", base_url=HTTPS_URL).get_json()["account"] == DREW


def test_a_cookie_without_the_host_prefix_is_ignored_on_https(settings, store, google, clock, sleeper):
    app = https_app(settings, store, google, clock, sleeper)
    store.save_sign_in(DREW, TokenGrant("access", clock() + 3600, "refresh", frozenset()))
    planted = store.start_session(DREW)
    client = app.test_client()
    client.set_cookie(SESSION_COOKIE, planted, domain="mail.example.test")
    assert client.get("/api/session", base_url=HTTPS_URL).status_code == 401
    client.set_cookie(f"__Host-{SESSION_COOKIE}", planted, domain="mail.example.test")
    assert client.get("/api/session", base_url=HTTPS_URL).get_json()["account"] == DREW


def test_local_http_keeps_plain_cookie_names_and_no_hsts(browser):
    start = browser.get("/auth/google")
    assert set_cookies(start)[0].startswith("mail_sorter_signin=") and "Secure" not in set_cookies(start)[0]
    assert "Strict-Transport-Security" not in start.headers
