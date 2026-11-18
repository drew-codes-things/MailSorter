import threading

import pytest
from conftest import DREW, FRIEND, Browser, make_app

from mailsorter.sorter import KEPT_LABEL, LATER_LABEL, QUEUE_SECONDS


def three_emails(google, account=DREW) -> list[str]:
    return [google.add_message(account, subject) for subject in ("oldest", "middle", "newest")]


def test_emails_come_one_at_a_time_newest_first_with_a_count(drew, google):
    three_emails(google)
    answer = drew.next()
    assert answer["message"]["subject"] == "newest"
    assert answer["counts"] == {"left": 3, "later": 0, "more": False}


def test_the_card_shows_sender_subject_date_and_attachment_names(drew, google):
    google.add_message(DREW, "Invoice 42", sender="=?utf-8?q?Caf=C3=A9_Ltd?= <billing@cafe.example>",
                       attachments=(("invoice-42.pdf", "application/pdf", 20480), ("photo.jpg", "image/jpeg", 3)))
    message = drew.next()["message"]
    assert message["from"] == {"name": "Café Ltd", "address": "billing@cafe.example"}
    assert message["subject"] == "Invoice 42"
    assert isinstance(message["date"], int)
    assert message["attachments"] == [{"name": "invoice-42.pdf", "type": "application/pdf", "size": 20480},
                                      {"name": "photo.jpg", "type": "image/jpeg", "size": 3}]


def test_yes_keeps_it_in_the_inbox_with_the_kept_label_and_moves_on(drew, google):
    oldest, middle, newest = three_emails(google)
    assert drew.act(newest, "yes").get_json() == {"repeated": False}
    box = google.mailbox(DREW)
    assert box.names_of(newest) >= {"INBOX", KEPT_LABEL}
    assert drew.next_subject() == "middle"
    assert drew.next()["counts"]["left"] == 2


def test_no_moves_it_to_the_bin_and_never_deletes_for_good(drew, google):
    oldest, middle, newest = three_emails(google)
    assert drew.act(newest, "no").status_code == 200
    box = google.mailbox(DREW)
    assert "TRASH" in box.names_of(newest) and "INBOX" not in box.names_of(newest)
    assert google.gmail_calls("POST", f"/messages/{newest}/trash")
    assert not google.gmail_calls("PERMANENT-DELETE")
    assert not google.gmail_calls("DELETE")
    assert drew.next_subject() == "middle"


def test_later_goes_to_the_back_and_comes_round_after_the_rest(drew, google):
    oldest, middle, newest = three_emails(google)
    drew.act(newest, "later")
    assert LATER_LABEL in google.mailbox(DREW).names_of(newest)
    assert drew.next()["counts"] == {"left": 2, "later": 1, "more": False}
    drew.act(middle, "yes")
    drew.act(oldest, "yes")
    answer = drew.next()
    assert answer["message"]["subject"] == "newest"
    assert answer["message"]["isLater"] is True


def test_two_later_emails_take_turns(drew, google):
    oldest, middle, newest = three_emails(google)
    drew.act(newest, "later")
    drew.act(middle, "later")
    drew.act(oldest, "yes")
    assert drew.next_subject() == "newest"
    drew.act(newest, "later")
    assert drew.next_subject() == "middle"
    drew.act(middle, "later")
    assert drew.next_subject() == "newest"


def test_later_emails_keep_taking_turns_after_the_queue_is_read_again(drew, google, clock):
    oldest, middle, newest = three_emails(google)
    drew.act(newest, "later")
    drew.act(middle, "later")
    drew.act(oldest, "yes")
    assert drew.next_subject() == "newest"
    drew.act(newest, "later")
    clock.advance(QUEUE_SECONDS + 1)
    assert drew.next_subject() == "middle"


def test_the_later_view_shows_only_later_emails(drew, google):
    oldest, middle, newest = three_emails(google)
    drew.act(middle, "later")
    assert drew.next_subject("later") == "middle"
    assert drew.next_subject("inbox") == "newest"


def test_yes_on_a_later_email_takes_the_later_label_off(drew, google):
    oldest, middle, newest = three_emails(google)
    drew.act(newest, "later")
    drew.act(newest, "yes")
    names = google.mailbox(DREW).names_of(newest)
    assert KEPT_LABEL in names and LATER_LABEL not in names


def test_an_empty_inbox_says_so(drew, google):
    answer = drew.next()
    assert answer == {"message": None, "counts": {"left": 0, "later": 0, "more": False}}


def test_when_everything_is_sorted_the_answer_is_empty(drew, google):
    oldest, middle, newest = three_emails(google)
    for message_id in (newest, middle, oldest):
        drew.act(message_id, "yes")
    assert drew.next()["message"] is None


def test_the_labels_are_made_once(drew, google):
    three_emails(google)
    drew.next()
    drew.next()
    box = google.mailbox(DREW)
    assert box.label_id(KEPT_LABEL) and box.label_id(LATER_LABEL)
    assert len(google.gmail_calls("POST", "/labels")) == 2


def test_labels_made_earlier_elsewhere_are_reused(drew, google):
    box = google.mailbox(DREW)
    box.labels["Label_old_kept"] = KEPT_LABEL
    box.labels["Label_old_later"] = LATER_LABEL
    newest = three_emails(google)[2]
    drew.act(newest, "yes")
    assert "Label_old_kept" in box.messages[newest]["labelIds"]
    assert google.gmail_calls("POST", "/labels") == []


def test_progress_lives_in_gmail_so_a_second_device_continues(app, google, drew):
    oldest, middle, newest = three_emails(google)
    drew.act(newest, "yes")
    drew.act(middle, "later")
    phone = Browser(app, google)
    phone.sign_in(DREW)
    answer = phone.next()
    assert answer["message"]["subject"] == "oldest"
    assert answer["counts"] == {"left": 1, "later": 1, "more": False}


def test_progress_survives_a_restart_of_the_app(settings, store, google, clock, sleeper, drew):
    oldest, middle, newest = three_emails(google)
    drew.act(newest, "yes")
    restarted = make_app(settings, store, google, clock, sleeper)
    again = Browser(restarted, google)
    again.client.set_cookie("mail_sorter_session", drew.client.get_cookie("mail_sorter_session").value)
    assert again.next_subject() == "middle"


def test_each_person_sees_only_their_own_mail(app, google, drew):
    google.add_message(DREW, "for drew")
    friends = google.add_message(FRIEND, "for friend")
    friend = Browser(app, google)
    friend.sign_in(FRIEND)
    assert drew.next_subject() == "for drew"
    assert friend.next_subject() == "for friend"
    assert drew.act(friends, "no").status_code == 410
    assert "TRASH" not in google.mailbox(FRIEND).names_of(friends)
    assert drew.get(f"/api/messages/{friends}/body").status_code == 410


def test_undo_yes_takes_the_label_off_and_shows_it_again(drew, google):
    oldest, middle, newest = three_emails(google)
    drew.act(newest, "yes")
    assert drew.undo(newest, "yes").get_json() == {"undone": "yes", "id": newest}
    assert KEPT_LABEL not in google.mailbox(DREW).names_of(newest)
    assert drew.next_subject() == "newest"


def test_undo_no_brings_it_back_from_the_bin_to_the_inbox(drew, google):
    oldest, middle, newest = three_emails(google)
    drew.act(middle, "no")
    drew.undo(middle, "no")
    names = google.mailbox(DREW).names_of(middle)
    assert "TRASH" not in names and "INBOX" in names
    assert drew.next_subject() == "middle"


def test_undo_later_takes_the_later_label_off(drew, google):
    oldest, middle, newest = three_emails(google)
    drew.act(newest, "later")
    drew.undo(newest, "later")
    assert LATER_LABEL not in google.mailbox(DREW).names_of(newest)
    assert drew.next()["counts"] == {"left": 3, "later": 0, "more": False}


def test_undo_of_yes_on_a_later_email_puts_the_later_label_back(drew, google):
    oldest, middle, newest = three_emails(google)
    drew.act(newest, "later")
    drew.act(newest, "yes")
    drew.undo(newest, "yes")
    names = google.mailbox(DREW).names_of(newest)
    assert LATER_LABEL in names and KEPT_LABEL not in names


def test_undo_works_once_and_then_there_is_nothing_to_undo(drew, google):
    newest = three_emails(google)[2]
    assert drew.undo(newest, "yes").status_code == 409
    drew.act(newest, "yes")
    assert drew.undo(newest, "yes").status_code == 200
    assert drew.undo(newest, "yes").status_code == 409


def test_undo_survives_a_reload(app, google, drew):
    newest = three_emails(google)[2]
    drew.act(newest, "no")
    laptop = Browser(app, google)
    laptop.sign_in(DREW)
    choice = laptop.get("/api/session").get_json()["lastChoice"]
    assert choice == {"id": newest, "action": "no"}
    assert laptop.undo(choice["id"], choice["action"]).get_json()["undone"] == "no"


def test_a_double_tap_acts_once(drew, google):
    newest = three_emails(google)[2]
    assert drew.act(newest, "no").get_json() == {"repeated": False}
    assert drew.act(newest, "no").get_json() == {"repeated": True}
    assert len(google.gmail_calls("POST", f"/messages/{newest}/trash")) == 1


def test_two_taps_at_the_same_moment_act_once(app, drew, google):
    newest = three_emails(google)[2]
    answers = []

    def tap():
        answers.append(drew.act(newest, "yes").get_json())

    taps = [threading.Thread(target=tap) for _ in range(2)]
    for thread in taps:
        thread.start()
    for thread in taps:
        thread.join()
    assert sorted(answer["repeated"] for answer in answers) == [False, True]
    assert len(google.gmail_calls("POST", f"/messages/{newest}/modify")) == 1


def test_an_email_deleted_elsewhere_is_skipped(drew, google):
    oldest, middle, newest = three_emails(google)
    assert drew.next_subject() == "newest"
    google.delete_message(DREW, newest)
    assert drew.next_subject() == "middle"


def test_acting_on_an_email_deleted_elsewhere_answers_gone_and_moves_on(drew, google):
    oldest, middle, newest = three_emails(google)
    drew.next()
    google.delete_message(DREW, newest)
    answer = drew.act(newest, "yes")
    assert answer.status_code == 410
    assert answer.get_json()["gone"] is True
    assert drew.next_subject() == "middle"


def test_an_email_sorted_on_gmail_itself_is_skipped(drew, google):
    oldest, middle, newest = three_emails(google)
    drew.next()
    google.mailbox(DREW).messages[middle]["labelIds"].remove("INBOX")
    drew.act(newest, "yes")
    assert drew.next_subject() == "oldest"


def test_a_label_deleted_in_gmail_is_made_again(drew, google):
    oldest, middle, newest = three_emails(google)
    drew.next()
    box = google.mailbox(DREW)
    del box.labels[box.label_id(KEPT_LABEL)]
    assert drew.act(newest, "yes").status_code == 200
    assert KEPT_LABEL in box.names_of(newest)


@pytest.mark.parametrize("path", ["/api/messages/not!an!id/yes", "/api/messages/abc/delete", "/api/messages/abc/archive"])
def test_unknown_actions_and_bad_ids_are_refused(drew, path):
    assert drew.post(path).status_code == 404


def test_a_view_that_does_not_exist_is_refused(drew):
    assert drew.get("/api/next?view=spam").status_code == 400
