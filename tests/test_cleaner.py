import sys
import threading
import time

import pytest

from mailsorter.cleaner import CleanerBusy, IsolatedCleaner, TooComplex
from mailsorter.sanitize import MAX_CLEAN_CHARACTERS, MAX_NESTING_DEPTH, is_too_complex, nesting_depth

SLEEPING_WORKER = (sys.executable, "-c", "import time; time.sleep(60)")
QUICK_ENOUGH_SECONDS = 2.0


@pytest.fixture
def cleaner():
    made = IsolatedCleaner()
    yield made
    made.close()


def test_the_cleaner_process_gives_the_same_answer_as_cleaning_in_place(cleaner):
    cleaned = cleaner.clean('<p>Hi <b>there</b></p><script>alert(1)</script><img src="https://t.example/p.gif">')
    assert cleaned.html == '<p>Hi <b>there</b></p><img src="https://t.example/p.gif">'
    assert cleaned.remote_images == 1


def test_a_worker_is_used_again_for_the_next_email(cleaner):
    cleaner.clean("<p>one</p>")
    first = cleaner._idle[0].process.pid
    cleaner.clean("<p>two</p>")
    assert [worker.process.pid for worker in cleaner._idle] == [first]


@pytest.mark.parametrize("raw", ["<div>" * 32_000, "<div>" * 400_000, "<b><i><u>" * 20_000,
                                 "<table><tr><td>" * 1_000, "<p>" + "x" * (MAX_CLEAN_CHARACTERS + 1)])
def test_nested_or_huge_html_is_refused_before_any_cleaning(cleaner, raw):
    started = time.monotonic()
    with pytest.raises(TooComplex):
        cleaner.clean(raw)
    assert time.monotonic() - started < QUICK_ENOUGH_SECONDS
    assert cleaner._idle == []


def test_ordinary_email_layouts_are_not_counted_as_deep():
    newsletter = "<table><tr><td><div><span>x</span></div><p>a<p>b<br><img src=x></td></tr></table>" * 3_000
    assert nesting_depth(newsletter) == 3
    assert not is_too_complex(newsletter)
    assert nesting_depth("<div>" * MAX_NESTING_DEPTH) == MAX_NESTING_DEPTH


def test_the_nesting_scan_stays_quick_on_broken_tags():
    started = time.monotonic()
    for raw in ("<a" * 500_000, "<div class=x" + "y" * 1_000_000, "<div/>" * 150_000):
        is_too_complex(raw)
    assert time.monotonic() - started < QUICK_ENOUGH_SECONDS


def test_a_stuck_cleaner_is_killed_after_its_time_and_replaced():
    stuck = IsolatedCleaner(command=SLEEPING_WORKER, clean_seconds=0.3)
    started = time.monotonic()
    with pytest.raises(TooComplex):
        stuck.clean("<p>hello</p>")
    assert time.monotonic() - started < QUICK_ENOUGH_SECONDS
    assert stuck._idle == []


def test_a_cleaner_that_dies_counts_as_too_complex():
    dying = IsolatedCleaner(command=(sys.executable, "-c", "raise SystemExit(3)"))
    with pytest.raises(TooComplex):
        dying.clean("<p>hello</p>")


def test_when_every_cleaner_is_busy_the_caller_is_told_quickly():
    busy = IsolatedCleaner(workers=1, command=SLEEPING_WORKER, clean_seconds=1.5, wait_seconds=0.2)
    blocker = threading.Thread(target=lambda: pytest.raises(TooComplex, busy.clean, "<p>a</p>"))
    blocker.start()
    time.sleep(0.2)
    started = time.monotonic()
    with pytest.raises(CleanerBusy):
        busy.clean("<p>b</p>")
    assert time.monotonic() - started < 1.0
    blocker.join()
