import sys
import threading
import time

import pytest
from conftest import DREW, Browser, make_app

from mailsorter import gmail, google as google_module
from mailsorter.sorter import AccountState, StateRegistry

QUICK_ENOUGH_SECONDS = 2.0
TOO_COMPLEX_WORDS = b"too complex to show here"
CANNOT_SHOW_WORDS = b"cannot be shown here"
TOO_LARGE_WORDS = b"too large to show here"
HEALTH_SECONDS = 0.5
NESTED_BYTES = 240_000
SLEEPING_WORKER = (sys.executable, "-c", "import time; time.sleep(60)")


def isolated_cleaner(**options):
    from mailsorter.cleaner import IsolatedCleaner
    return IsolatedCleaner(**options)


class CountingCleaner:
    def __init__(self):
        self.inner = isolated_cleaner()
        self.calls = 0

    def clean(self, raw):
        self.calls += 1
        return self.inner.clean(raw)


def timed(call):
    started = time.monotonic()
    answer = call()
    return answer, time.monotonic() - started


def set_charset(google, message_id, kind, charset):
    parts = google.mailbox(DREW).messages[message_id]["payload"]["parts"][0]["parts"]
    for part in parts:
        if part["mimeType"] == kind:
            part["headers"] = [{"name": "Content-Type", "value": f"{kind}; charset={charset}"}]


def test_a_deeply_nested_email_is_answered_quickly_and_can_still_be_sorted(drew, google):
    nested = google.add_message(DREW, "Nested", html="<div>" * (NESTED_BYTES // 5), text="the plain version")
    answer, spent = timed(lambda: drew.get("/api/next"))
    assert answer.status_code == 200 and spent < QUICK_ENOUGH_SECONDS
    assert answer.get_json()["message"]["subject"] == "Nested"
    body, spent = timed(lambda: drew.get(f"/api/messages/{nested}/body"))
    assert body.status_code == 200 and spent < QUICK_ENOUGH_SECONDS
    assert b"the plain version" in body.data
    assert drew.act(nested, "yes").status_code == 200


def test_a_nested_email_without_a_plain_part_says_it_is_too_complex(drew, google):
    nested = google.add_message(DREW, "Nested", html="<div>" * (NESTED_BYTES // 5))
    assert TOO_COMPLEX_WORDS in drew.get(f"/api/messages/{nested}/body").data


def test_the_body_is_cleaned_once_per_message(settings, store, google, clock, sleeper):
    counting = CountingCleaner()
    browser = Browser(make_app(settings, store, google, clock, sleeper, cleaner=counting), google)
    browser.sign_in(DREW)
    message_id = google.add_message(DREW, "Sale", html="<p>Sale <img src=https://t.example/a.png></p>")
    assert browser.next()["message"]["remoteImages"] == 1
    for _ in range(3):
        assert browser.get(f"/api/messages/{message_id}/body").status_code == 200
    assert counting.calls == 1
    counting.inner.close()


def test_health_answers_while_a_cleaner_is_stuck(settings, store, google, clock, sleeper):
    stuck = isolated_cleaner(command=SLEEPING_WORKER, clean_seconds=1.0)
    app = make_app(settings, store, google, clock, sleeper, cleaner=stuck)
    browser = Browser(app, google)
    browser.sign_in(DREW)
    message_id = google.add_message(DREW, "Slow", html="<p>slow</p>", text="slow plain text")
    answers = []
    reader = threading.Thread(target=lambda: answers.append(browser.get(f"/api/messages/{message_id}/body")))
    reader.start()
    time.sleep(0.2)
    health, spent = timed(lambda: app.test_client().get("/api/health"))
    reader.join()
    assert health.status_code == 200 and spent < HEALTH_SECONDS
    assert answers[0].status_code == 200 and b"slow plain text" in answers[0].data


@pytest.mark.parametrize("charset", ["hex", "undefined", "idna", "rot13", "base64", "zlib", "uu", "quopri",
                                     "unicode_escape", "raw_unicode_escape", "utf-7"])
@pytest.mark.parametrize("kind", ["text/html", "text/plain"])
def test_an_email_in_a_hostile_charset_still_shows_and_can_be_sorted(drew, google, charset, kind):
    poison = google.add_message(DREW, "Poison", html="<p>\\ud800 +2AA- hello</p>" if kind == "text/html" else None,
                                text="\\ud800 +2AA- hello" if kind == "text/plain" else None)
    set_charset(google, poison, kind, charset)
    assert drew.next()["message"]["subject"] == "Poison"
    body = drew.get(f"/api/messages/{poison}/body")
    assert body.status_code == 200 and b"hello" in body.data
    assert drew.act(poison, "no").status_code == 200


@pytest.mark.parametrize("field", ["subject", "sender", "attachment"])
def test_a_broken_encoded_word_does_not_block_the_queue(drew, google, field):
    broken = "=?utf-8?b?a?="
    if field == "subject":
        poison = google.add_message(DREW, broken, html="<p>x</p>")
    elif field == "sender":
        poison = google.add_message(DREW, "Poison", sender=f"{broken} <a@evil.example>", html="<p>x</p>")
    else:
        poison = google.add_message(DREW, "Poison", html="<p>x</p>", attachments=((broken, "application/pdf", 10),))
    assert drew.next()["message"]["id"] == poison
    assert drew.act(poison, "later").status_code == 200


def test_a_header_with_a_lone_surrogate_is_shown_with_a_replacement(drew, google):
    poison = google.add_message(DREW, "Bad \ud800 subject", sender="\ud800 <a@evil.example>", html="<p>x</p>")
    message = drew.next()["message"]
    assert message["id"] == poison and message["subject"] == "Bad � subject"


def test_any_render_error_gives_a_card_that_can_still_be_sorted(drew, google, monkeypatch):
    def broken(*arguments, **keywords):
        raise RuntimeError("a bug in the reader")

    monkeypatch.setattr("mailsorter.present.summary", broken)
    monkeypatch.setattr("mailsorter.present.body_of", broken)
    message_id = google.add_message(DREW, "Anything", html="<p>x</p>")
    message = drew.next()["message"]
    assert message["id"] == message_id and message["unreadable"] is True
    assert CANNOT_SHOW_WORDS in drew.get(f"/api/messages/{message_id}/body").data
    assert drew.act(message_id, "no").status_code == 200


def test_an_email_too_large_to_fetch_whole_is_shown_by_its_headers(drew, google, monkeypatch):
    monkeypatch.setattr(gmail, "MAX_RESPONSE_BYTES", 20_000)
    big = google.add_message(DREW, "Huge newsletter", sender="Shop <news@shop.example>", html="<p>" + "a" * 30_000)
    message = drew.next()["message"]
    assert (message["id"], message["subject"], message["from"]["address"]) == (big, "Huge newsletter", "news@shop.example")
    assert TOO_LARGE_WORDS in drew.get(f"/api/messages/{big}/body").data
    assert google.gmail_calls("GET", "format=metadata")
    assert drew.act(big, "yes").status_code == 200


def test_answers_from_google_are_capped_well_below_the_service_memory():
    assert google_module.MAX_RESPONSE_BYTES <= 8 * 1024 * 1024
    assert gmail.MAX_RESPONSE_BYTES == google_module.MAX_RESPONSE_BYTES


def test_account_state_keeps_cleaned_bodies_not_whole_messages():
    state = AccountState()
    assert not hasattr(state, "messages")
    assert len(state.bodies) == 0


def test_idle_account_state_is_let_go(clock):
    registry = StateRegistry(clock, idle_seconds=60)
    registry.get(DREW)
    clock.advance(61)
    registry.get("friend@example.com")
    assert registry.tracked() == 1
    clock.advance(30)
    registry.get(DREW)
    assert registry.tracked() == 2
