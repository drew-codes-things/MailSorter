import threading

import pytest
from conftest import DREW, Browser

from mailsorter.gmail import (FIRST_WAIT_SECONDS, LONGEST_WAIT_SECONDS, MAX_ATTEMPTS, FixedAccess, GmailClient, GmailError,
                              is_message_id)
from mailsorter.google import MAX_RESPONSE_BYTES, GoogleEndpoints, HttpAnswer
from mailsorter.sorter import QUEUE_SECONDS

RATE_LIMITED = {"error": {"code": 403, "message": "User-rate limit exceeded",
                          "errors": [{"reason": "userRateLimitExceeded"}]}}
RESOURCE_EXHAUSTED = {"error": {"code": 429, "message": "Too many requests", "status": "RESOURCE_EXHAUSTED"}}


def one_email(google) -> str:
    return google.add_message(DREW, "hello")


def test_a_rate_limit_is_waited_out_and_retried(drew, google, sleeper):
    message_id = one_email(google)
    drew.next()
    google.fail("POST", "/modify", 403, times=2, body=RATE_LIMITED)
    assert drew.act(message_id, "yes").status_code == 200
    assert sleeper.waits == [FIRST_WAIT_SECONDS, FIRST_WAIT_SECONDS * 2]


def test_retry_after_is_followed_but_capped(drew, google, sleeper):
    message_id = one_email(google)
    drew.next()
    google.fail("POST", "/trash", 429, body=RESOURCE_EXHAUSTED, headers={"Retry-After": "3"})
    google.fail("POST", "/trash", 429, body=RESOURCE_EXHAUSTED, headers={"Retry-After": "600"})
    assert drew.act(message_id, "no").status_code == 200
    assert sleeper.waits == [3, LONGEST_WAIT_SECONDS]


def test_a_rate_limit_that_does_not_clear_answers_busy_and_changes_nothing(drew, google, sleeper):
    message_id = one_email(google)
    drew.next()
    google.fail("POST", "/modify", 429, times=MAX_ATTEMPTS, body=RESOURCE_EXHAUSTED)
    answer = drew.act(message_id, "yes")
    assert answer.status_code == 503
    assert answer.get_json()["retry"] is True
    assert len(sleeper.waits) == MAX_ATTEMPTS - 1
    assert drew.undo(message_id, "yes").status_code == 409
    assert drew.next()["message"]["id"] == message_id
    assert drew.act(message_id, "yes").status_code == 200


def test_server_errors_from_gmail_are_retried(drew, google):
    one_email(google)
    google.fail("GET", "/messages", 503, times=1)
    google.fail("GET", "/labels", 500, times=1)
    assert drew.next()["message"]["subject"] == "hello"


def test_a_plain_permission_error_is_not_retried(drew, google, sleeper):
    message_id = one_email(google)
    drew.next()
    google.fail("POST", "/modify", 403, body={"error": {"code": 403, "message": "Insufficient Permission",
                                                         "errors": [{"reason": "insufficientPermissions"}]}})
    answer = drew.act(message_id, "yes")
    assert answer.status_code == 502
    assert "Insufficient Permission" in answer.get_json()["error"]
    assert sleeper.waits == []


def test_an_access_token_that_runs_out_mid_session_is_renewed(drew, google, clock, store):
    message_id = one_email(google)
    drew.next()
    first = store.tokens(DREW).access_token
    google.expire_access_tokens()
    assert drew.act(message_id, "yes").status_code == 200
    assert store.tokens(DREW).access_token != first


def test_an_access_token_about_to_run_out_is_renewed_before_use(drew, google, clock, store):
    one_email(google)
    first = store.tokens(DREW).access_token
    clock.advance(3600 - 30)
    assert drew.next()["message"]["subject"] == "hello"
    assert store.tokens(DREW).access_token != first


def test_a_refresh_google_refuses_signs_the_account_out(drew, google, store):
    one_email(google)
    google.refresh_tokens.clear()
    google.expire_access_tokens()
    answer = drew.get("/api/next")
    assert answer.status_code == 401
    assert answer.get_json()["signIn"] is True
    assert store.tokens(DREW) is None
    assert drew.get("/api/session").status_code == 401


def test_google_unreachable_answers_a_retryable_error(drew, google):
    google.close()
    answer = drew.get("/api/next")
    assert answer.status_code == 502
    assert answer.get_json()["retry"] is True


def test_a_queue_of_many_pages_is_read_whole(drew, google):
    for number in range(1203):
        google.add_message(DREW, f"mail {number}")
    answer = drew.next()
    assert answer["counts"] == {"left": 1203, "later": 0, "more": False}
    assert answer["message"]["subject"] == "mail 1202"


def test_two_requests_needing_a_new_token_at_once_share_one_renewal(app, drew, google, clock):
    one_email(google)
    drew.next()
    google.expire_access_tokens()
    clock.advance(QUEUE_SECONDS + 1)
    issued_before = len(google.access_tokens)
    phone = Browser(app, google)
    phone.client.set_cookie("mail_sorter_session", drew.client.get_cookie("mail_sorter_session").value)
    answers = []
    requests = [threading.Thread(target=lambda client=client: answers.append(client.get("/api/counts").status_code))
                for client in (drew, phone)]
    for thread in requests:
        thread.start()
    for thread in requests:
        thread.join()
    assert answers == [200, 200]
    assert len(google.access_tokens) == issued_before + 1


def lone_client(answer: HttpAnswer) -> GmailClient:
    return GmailClient(FixedAccess("token"), GoogleEndpoints(), transport=lambda *arguments, **keywords: answer,
                       sleep=lambda seconds: None)


@pytest.mark.parametrize("body", [b"<html>proxy page</html>", b"", b'{"labels": [', b"[1, 2]", b"\xff\xfe"])
def test_a_successful_answer_that_is_not_a_json_object_is_an_error(body):
    with pytest.raises(GmailError):
        lone_client(HttpAnswer(200, {}, body)).labels()


def test_a_cut_off_answer_is_an_error_not_an_empty_one():
    cut_off = b'{"labels": [' + b" " * MAX_RESPONSE_BYTES
    with pytest.raises(GmailError):
        lone_client(HttpAnswer(200, {}, cut_off)).labels()


def test_message_ids_must_match_whole():
    assert is_message_id("18c2f0a1b2c3d4e5")
    for bad in ("abc\n", "abc\r\n", "abc def", "", "a" * 65, None):
        assert not is_message_id(bad), bad
