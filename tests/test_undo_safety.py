from conftest import DREW, Browser

from mailsorter.sorter import DOUBLE_TAP_SECONDS, KEPT_LABEL, LATER_LABEL


def test_undo_names_the_choice_and_refuses_when_another_came_after(app, google):
    binned = google.add_message(DREW, "Binned on the laptop", html="<p>a</p>")
    kept = google.add_message(DREW, "Kept on the phone", html="<p>b</p>")
    laptop, phone = Browser(app, google), Browser(app, google)
    laptop.sign_in(DREW)
    phone.sign_in(DREW)
    assert laptop.act(binned, "no").status_code == 200
    assert phone.act(kept, "yes").status_code == 200

    refused = laptop.undo(binned, "no")
    assert refused.status_code == 409 and refused.get_json()["stale"] is True
    box = google.mailbox(DREW)
    assert "TRASH" in box.names_of(binned) and KEPT_LABEL in box.names_of(kept)
    assert phone.undo(kept, "yes").get_json() == {"undone": "yes", "id": kept}
    assert KEPT_LABEL not in box.names_of(kept)


def test_undo_with_the_right_email_but_another_choice_is_refused(drew, google):
    message_id = google.add_message(DREW, "Mail", html="<p>x</p>")
    drew.act(message_id, "yes")
    assert drew.undo(message_id, "no").status_code == 409
    assert KEPT_LABEL in google.mailbox(DREW).names_of(message_id)


def test_undo_without_naming_the_choice_is_refused(drew, google):
    message_id = google.add_message(DREW, "Mail", html="<p>x</p>")
    drew.act(message_id, "no")
    for payload in (None, {}, {"id": message_id}, {"id": message_id, "action": "delete"}, {"id": "../x", "action": "no"}):
        assert drew.post("/api/undo", json=payload).status_code == 400, payload
    assert "TRASH" in google.mailbox(DREW).names_of(message_id)


def test_the_session_says_which_choice_undo_would_reverse(drew, google):
    message_id = google.add_message(DREW, "Mail", html="<p>x</p>")
    assert drew.get("/api/session").get_json()["lastChoice"] is None
    drew.act(message_id, "later")
    assert drew.get("/api/session").get_json()["lastChoice"] == {"id": message_id, "action": "later"}


def test_later_pressed_twice_on_one_email_can_still_be_undone(drew, google, clock):
    message_id = google.add_message(DREW, "Mail", html="<p>x</p>")
    assert drew.act(message_id, "later").get_json() == {"repeated": False}
    clock.advance(DOUBLE_TAP_SECONDS + 1)
    assert drew.act(message_id, "later").get_json() == {"repeated": False}
    assert drew.undo(message_id, "later").get_json() == {"undone": "later", "id": message_id}
    assert LATER_LABEL not in google.mailbox(DREW).names_of(message_id)
