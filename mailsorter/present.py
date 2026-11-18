import logging
from dataclasses import dataclass

from .cleaner import CleanerBusy, TooComplex
from .mime import body_of, summary
from .sanitize import plain_text_html

MAX_BODY_BYTES = 2 * 1024 * 1024
TOO_LARGE_TEXT = "<p><em>This email is too large to show here. Open it in Gmail to read it.</em></p>"
TOO_COMPLEX_TEXT = ("<p><em>This email's layout is too complex to show here safely. Open it in Gmail to read it; "
                    "you can still sort it.</em></p>")
CANNOT_SHOW_TEXT = ("<p><em>This email cannot be shown here. Open it in Gmail to read it; "
                    "you can still sort it.</em></p>")
EMPTY_BODY_TEXT = "<p><em>This email has no text.</em></p>"
UNREADABLE_SUBJECT = "(this email cannot be shown here)"

log = logging.getLogger("mailsorter.present")


@dataclass(frozen=True)
class ShownBody:
    html: str
    remote_images: int = 0
    keep: bool = True


TOO_LARGE = ShownBody(TOO_LARGE_TEXT)
CANNOT_SHOW = ShownBody(CANNOT_SHOW_TEXT)


def unreadable_card(message_id: str) -> dict:
    return {"id": message_id, "from": {"name": "", "address": ""}, "to": "", "subject": UNREADABLE_SUBJECT,
            "date": None, "snippet": "", "attachments": [], "unreadable": True}


def card(message_id: str, message: dict) -> dict:
    try:
        found = summary(message)
    except Exception:
        log.exception("the header of message %s could not be read, so a plain card is shown", message_id)
        return unreadable_card(message_id)
    return {**found, "id": message_id}


class Presenter:
    def __init__(self, cleaner):
        self.cleaner = cleaner

    def _fallback(self, text: str | None, keep: bool) -> ShownBody:
        if text:
            return ShownBody(plain_text_html(text), keep=keep)
        return ShownBody(TOO_COMPLEX_TEXT, keep=keep)

    def _html(self, raw_html: str, text: str | None) -> ShownBody:
        try:
            cleaned = self.cleaner.clean(raw_html)
        except TooComplex:
            return self._fallback(text, keep=True)
        except CleanerBusy:
            log.warning("every email cleaner was busy, so a simpler view was shown and not kept")
            return self._fallback(text, keep=False)
        return ShownBody(cleaned.html, cleaned.remote_images)

    def _body(self, message: dict) -> ShownBody:
        parts = body_of(message.get("payload") or {}, MAX_BODY_BYTES)
        if parts.html:
            return self._html(parts.html, parts.text)
        if parts.text:
            return ShownBody(plain_text_html(parts.text))
        return TOO_LARGE if parts.too_large else ShownBody(EMPTY_BODY_TEXT)

    def body(self, message_id: str, message: dict) -> ShownBody:
        try:
            return self._body(message)
        except Exception:
            log.exception("the body of message %s could not be shown, so a plain note is shown", message_id)
            return CANNOT_SHOW
