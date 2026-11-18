import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from .accounts import AccountStore
from .gmail import AnswerTooLarge, GmailBusy, GmailClient, GmailError, LabelMissing, MessageGone
from .mime import label_ids
from .present import TOO_LARGE, Presenter, ShownBody, card, unreadable_card

KEPT_LABEL = "Sorted/Kept"
LATER_LABEL = "Sorted/Later"
INBOX = "INBOX"
TRASH = "TRASH"
ACTIONS = ("yes", "no", "later")
VIEWS = ("inbox", "later")
QUEUE_SECONDS = 60
MAX_LIST_PAGES = 20
DOUBLE_TAP_SECONDS = 5
MAX_SKIPS = 25
CACHED_BODIES = 4
CACHED_BODY_CHARACTERS = 4 * 1024 * 1024
IDLE_STATE_SECONDS = 30 * 60

log = logging.getLogger("mailsorter.sorter")


class NothingToUndo(Exception):
    pass


class UndoMismatch(Exception):
    pass


@dataclass
class AccountState:
    lock: threading.RLock = field(default_factory=threading.RLock)
    labels: tuple[str, str] | None = None
    main: list = field(default_factory=list)
    later: list = field(default_factory=list)
    passed_this_round: set = field(default_factory=set)
    truncated: bool = False
    built_at: float | None = None
    front: str | None = None
    bodies: OrderedDict = field(default_factory=OrderedDict)
    used_at: float = 0.0


class StateRegistry:
    def __init__(self, monotonic=time.monotonic, idle_seconds: float = IDLE_STATE_SECONDS):
        self._lock = threading.Lock()
        self._states: dict[str, AccountState] = {}
        self.monotonic = monotonic
        self.idle_seconds = idle_seconds

    def _drop_idle(self, now: float) -> None:
        idle = [email for email, state in self._states.items() if now - state.used_at > self.idle_seconds]
        for email in idle:
            del self._states[email]

    def get(self, email: str) -> AccountState:
        with self._lock:
            now = self.monotonic()
            self._drop_idle(now)
            state = self._states.setdefault(email, AccountState())
            state.used_at = now
            return state

    def drop(self, email: str) -> None:
        with self._lock:
            self._states.pop(email, None)

    def tracked(self) -> int:
        with self._lock:
            return len(self._states)


def _unique(ids) -> list:
    return list(dict.fromkeys(ids))


class Sorter:
    def __init__(self, gmail: GmailClient, store: AccountStore, state: AccountState, email: str, presenter: Presenter,
                 clock=time.time, monotonic=time.monotonic):
        self.gmail = gmail
        self.store = store
        self.state = state
        self.email = email
        self.presenter = presenter
        self.clock = clock
        self.monotonic = monotonic

    def _labels(self) -> tuple[str, str]:
        if self.state.labels:
            return self.state.labels
        existing = {label.get("name"): label.get("id") for label in self.gmail.labels()}
        for name in (KEPT_LABEL, LATER_LABEL):
            if existing.get(name):
                continue
            created = self.gmail.create_label(name)
            if created and created.get("id"):
                existing[name] = created["id"]
            else:
                existing = {label.get("name"): label.get("id") for label in self.gmail.labels()}
        if not existing.get(KEPT_LABEL) or not existing.get(LATER_LABEL):
            raise GmailError(f"Gmail did not make the labels {KEPT_LABEL} and {LATER_LABEL}")
        self.state.labels = (existing[KEPT_LABEL], existing[LATER_LABEL])
        return self.state.labels

    def _is_queue_fresh(self) -> bool:
        return self.state.built_at is not None and self.monotonic() - self.state.built_at < QUEUE_SECONDS

    def _build_queue(self) -> None:
        self.state.labels = None
        kept, later = self._labels()
        inbox, truncated = self.gmail.message_ids(INBOX, MAX_LIST_PAGES)
        kept_ids = set(self.gmail.message_ids(kept, MAX_LIST_PAGES)[0])
        later_ids = set(self.gmail.message_ids(later, MAX_LIST_PAGES)[0])
        self.state.main = [message_id for message_id in inbox if message_id not in kept_ids | later_ids]
        self.state.later = [message_id for message_id in inbox if message_id in later_ids and message_id not in kept_ids]
        self.state.passed_this_round &= set(self.state.later)
        self.state.truncated = truncated
        self.state.built_at = self.monotonic()

    def _ensure_queue(self) -> None:
        if not self._is_queue_fresh():
            self._build_queue()

    def _forget(self, message_id: str) -> None:
        for queue in (self.state.main, self.state.later):
            if message_id in queue:
                queue.remove(message_id)
        self.state.passed_this_round.discard(message_id)
        self.state.bodies.pop(message_id, None)
        if self.state.front == message_id:
            self.state.front = None

    def _candidates(self, view: str) -> list:
        later_waiting = [message_id for message_id in self.state.later if message_id not in self.state.passed_this_round]
        reaches_later = view == "later" or not self.state.main
        if reaches_later and not later_waiting and self.state.later:
            self.state.passed_this_round.clear()
            later_waiting = list(self.state.later)
        order = [self.state.front] if self.state.front else []
        if view == "inbox":
            order += self.state.main
        if reaches_later:
            order += later_waiting
        return _unique(order)

    def _cached_characters(self) -> int:
        return sum(len(body.html) for body in self.state.bodies.values())

    def _remember_body(self, message_id: str, body: ShownBody) -> None:
        if not body.keep or len(body.html) > CACHED_BODY_CHARACTERS:
            return
        self.state.bodies[message_id] = body
        self.state.bodies.move_to_end(message_id)
        while len(self.state.bodies) > CACHED_BODIES or self._cached_characters() > CACHED_BODY_CHARACTERS:
            self.state.bodies.popitem(last=False)

    def _fetch(self, message_id: str) -> tuple[dict, bool]:
        try:
            return self.gmail.message(message_id), True
        except AnswerTooLarge:
            log.info("an email is too large to fetch whole, so only its headers are read")
        try:
            return self.gmail.message_headers(message_id), False
        except AnswerTooLarge:
            return self.gmail.message_labels(message_id), False

    def _shown_body(self, message_id: str, message: dict, whole: bool) -> ShownBody:
        body = self.presenter.body(message_id, message) if whole else TOO_LARGE
        self._remember_body(message_id, body)
        return body

    def body(self, message_id: str) -> ShownBody:
        with self.state.lock:
            cached = self.state.bodies.get(message_id)
            if cached is not None:
                return cached
            try:
                message, whole = self._fetch(message_id)
            except AnswerTooLarge:
                return TOO_LARGE
            return self._shown_body(message_id, message, whole)

    def _unreadable(self, message_id: str) -> dict:
        self._remember_body(message_id, TOO_LARGE)
        return {**unreadable_card(message_id), "isLater": message_id in self.state.later,
                "remoteImages": TOO_LARGE.remote_images}

    def _settle_queue_position(self, message_id: str, labels: set, later: str) -> bool:
        if later in labels and message_id in self.state.main:
            self.state.main.remove(message_id)
            self.state.later.append(message_id)
            return False
        if later not in labels and message_id in self.state.later:
            self.state.later.remove(message_id)
            self.state.passed_this_round.discard(message_id)
            self.state.main.insert(0, message_id)
            return False
        return True

    def next_message(self, view: str) -> dict | None:
        with self.state.lock:
            self._ensure_queue()
            for _ in range(MAX_SKIPS):
                candidates = self._candidates(view)
                if not candidates:
                    return None
                message_id = candidates[0]
                try:
                    message, whole = self._fetch(message_id)
                except MessageGone:
                    self._forget(message_id)
                    continue
                except AnswerTooLarge:
                    return self._unreadable(message_id)
                kept, later = self._labels()
                labels = label_ids(message)
                if INBOX not in labels or TRASH in labels or kept in labels:
                    self._forget(message_id)
                    continue
                if message_id != self.state.front and not self._settle_queue_position(message_id, labels, later):
                    continue
                body = self._shown_body(message_id, message, whole)
                return {**card(message_id, message), "isLater": later in labels, "remoteImages": body.remote_images}
            raise GmailBusy("many emails changed elsewhere while looking for the next one; try again")

    def counts(self) -> dict:
        with self.state.lock:
            self._ensure_queue()
            return {"left": len(self.state.main), "later": len(self.state.later), "more": self.state.truncated}

    def _apply(self, message_id: str, action: str) -> None:
        kept, later = self._labels()
        if action == "yes":
            self.gmail.modify(message_id, add=[kept], remove=[later])
        elif action == "no":
            self.gmail.trash(message_id)
        else:
            self.gmail.modify(message_id, add=[later])

    def _with_fresh_labels(self, step) -> None:
        try:
            step()
        except LabelMissing:
            self.state.labels = None
            step()

    def _had_later(self, message_id: str, action: str) -> bool:
        last = self.store.last_action(self.email)
        if last is not None and last.message_id == message_id and last.action == action:
            return last.had_later
        return message_id in self.state.later

    def _is_double_tap(self, message_id: str, action: str) -> bool:
        last = self.store.last_action(self.email)
        return (last is not None and last.message_id == message_id and last.action == action
                and self.clock() - last.done_at < DOUBLE_TAP_SECONDS)

    def act(self, message_id: str, action: str) -> dict:
        if action not in ACTIONS:
            raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
        with self.state.lock:
            if self._is_double_tap(message_id, action):
                return {"repeated": True}
            self._ensure_queue()
            had_later = self._had_later(message_id, action)
            try:
                self._with_fresh_labels(lambda: self._apply(message_id, action))
            except MessageGone:
                self._forget(message_id)
                raise
            self._forget(message_id)
            if action == "later":
                self.state.later.append(message_id)
                self.state.passed_this_round.add(message_id)
            self.store.record_action(self.email, message_id, action, had_later)
            return {"repeated": False}

    def _reverse(self, message_id: str, action: str, had_later: bool) -> None:
        kept, later = self._labels()
        if action == "yes":
            self.gmail.modify(message_id, add=[later] if had_later else [], remove=[kept])
        elif action == "no":
            self.gmail.untrash(message_id)
            self.gmail.modify(message_id, add=[INBOX, later] if had_later else [INBOX])
        elif not had_later:
            self.gmail.modify(message_id, remove=[later])

    def last_choice(self) -> dict | None:
        last = self.store.last_action(self.email)
        return None if last is None else {"id": last.message_id, "action": last.action}

    def undo(self, message_id: str, action: str) -> dict:
        with self.state.lock:
            last = self.store.last_action(self.email)
            if last is None:
                raise NothingToUndo("there is nothing to undo")
            if (last.message_id, last.action) != (message_id, action):
                raise UndoMismatch("another choice came after that one")
            try:
                self._with_fresh_labels(lambda: self._reverse(last.message_id, last.action, last.had_later))
            except MessageGone:
                self.store.clear_last_action(self.email)
                self._forget(last.message_id)
                raise
            self.store.clear_last_action(self.email)
            self._ensure_queue()
            self._forget(last.message_id)
            if last.had_later:
                self.state.later.insert(0, last.message_id)
            else:
                self.state.main.insert(0, last.message_id)
            self.state.front = last.message_id
            return {"undone": last.action, "id": last.message_id}
