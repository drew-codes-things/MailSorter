import json
import logging
import re
import time
from urllib.parse import quote, urlencode

from .accounts import AccountStore
from .google import MAX_RESPONSE_BYTES, GoogleEndpoints, HttpAnswer, OAuthClient, SignInExpired, send

REFRESH_MARGIN_SECONDS = 60
MAX_ATTEMPTS = 4
FIRST_WAIT_SECONDS = 1.0
LONGEST_WAIT_SECONDS = 8.0
PAGE_SIZE = 500
RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
RATE_LIMIT_STATUSES = frozenset({"RESOURCE_EXHAUSTED"})
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
MESSAGE_ID = re.compile(r"[A-Za-z0-9]{1,64}")
SHOWN_HEADERS = ("From", "To", "Subject")
INVALID_LABEL_TEXT = "invalid label"
CONFLICT = 409

log = logging.getLogger("mailsorter.gmail")


class GmailError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class GmailBusy(GmailError):
    pass


class MessageGone(GmailError):
    pass


class LabelMissing(GmailError):
    pass


class AnswerTooLarge(GmailError):
    pass


def is_message_id(value) -> bool:
    return isinstance(value, str) and MESSAGE_ID.fullmatch(value) is not None


def _successful_data(answer: HttpAnswer) -> dict:
    try:
        data = json.loads(answer.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise GmailError(f"Gmail answered {answer.status} with something that is not JSON", answer.status) from error
    if not isinstance(data, dict):
        raise GmailError(f"Gmail answered {answer.status} with JSON that is not an object", answer.status)
    return data


class AccountAccess:
    def __init__(self, store: AccountStore, oauth: OAuthClient, email: str, clock=time.time):
        self.store = store
        self.oauth = oauth
        self.email = email
        self.clock = clock

    def _is_fresh(self, expires_at: float) -> bool:
        return expires_at - REFRESH_MARGIN_SECONDS > self.clock()

    def token(self) -> str:
        tokens = self.store.tokens(self.email)
        if tokens is None:
            raise SignInExpired("this account is signed out")
        if self._is_fresh(tokens.access_expires_at):
            return tokens.access_token
        return self.refresh()

    def refresh(self) -> str:
        tokens = self.store.tokens(self.email)
        if tokens is None:
            raise SignInExpired("this account is signed out")
        try:
            grant = self.oauth.refresh(tokens.refresh_token)
        except SignInExpired:
            if not self.store.forget_account_if_refresh_token_is(self.email, tokens.refresh_token):
                log.info("Google refused an old refresh token, but the account signed in again meanwhile")
                return self.token()
            log.info("Google refused the refresh token for an account, so it was signed out")
            raise
        self.store.save_access(self.email, grant)
        return grant.access_token

    def give_up(self) -> None:
        self.store.forget_account(self.email)


class FixedAccess:
    def __init__(self, access_token: str):
        self.access_token = access_token

    def token(self) -> str:
        return self.access_token

    def refresh(self) -> str:
        raise SignInExpired("Google refused the access it had just granted")

    def give_up(self) -> None:
        return None


def _error_details(body: dict) -> tuple[str, set, str]:
    error = body.get("error")
    if not isinstance(error, dict):
        return "", set(), ""
    reasons = {item.get("reason") for item in error.get("errors") or [] if isinstance(item, dict)}
    return str(error.get("message") or ""), reasons, str(error.get("status") or "")


def _is_rate_limited(status: int, body: dict) -> bool:
    if status in RETRYABLE_STATUSES:
        return True
    if status != 403:
        return False
    _, reasons, error_status = _error_details(body)
    return bool(reasons & RATE_LIMIT_REASONS) or error_status in RATE_LIMIT_STATUSES


def _wait_before(attempt: int, retry_after: str | None) -> float:
    if retry_after and retry_after.strip().isdigit():
        return min(float(retry_after.strip()), LONGEST_WAIT_SECONDS)
    return min(FIRST_WAIT_SECONDS * (2 ** attempt), LONGEST_WAIT_SECONDS)


class GmailClient:
    def __init__(self, access: AccountAccess, endpoints: GoogleEndpoints, transport=send, sleep=time.sleep):
        self.access = access
        self.endpoints = endpoints
        self.transport = transport
        self.sleep = sleep

    def _url(self, path: str, query: dict | None) -> str:
        url = f"{self.endpoints.gmail}/users/me/{path}"
        return f"{url}?{urlencode(query, doseq=True)}" if query else url

    def _call(self, method: str, path: str, query: dict | None = None, payload: dict | None = None) -> dict:
        url = self._url(path, query)
        body = json.dumps(payload).encode() if payload is not None else (b"" if method == "POST" else None)
        token = self.access.token()
        refreshed = False
        attempt = 0
        while True:
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            answer = self.transport(method, url, headers, body)
            if len(answer.body) > MAX_RESPONSE_BYTES:
                raise AnswerTooLarge(f"Gmail's answer was over {MAX_RESPONSE_BYTES // (1024 * 1024)} MB", answer.status)
            if 200 <= answer.status < 300:
                return _successful_data(answer)
            data = answer.json()
            if answer.status == 401 and not refreshed:
                token = self.access.refresh()
                refreshed = True
                continue
            if answer.status == 401:
                self.access.give_up()
                raise SignInExpired("Google keeps refusing this account's access; sign in again")
            if _is_rate_limited(answer.status, data):
                if attempt + 1 >= MAX_ATTEMPTS:
                    raise GmailBusy(f"Gmail is busy (status {answer.status} after {MAX_ATTEMPTS} tries)")
                self.sleep(_wait_before(attempt, answer.headers.get("retry-after")))
                attempt += 1
                continue
            message, _, _ = _error_details(data)
            if answer.status == 404:
                raise MessageGone(message or "not found")
            if answer.status == 400 and INVALID_LABEL_TEXT in message.lower():
                raise LabelMissing(message)
            raise GmailError(f"Gmail answered {answer.status}: {message or 'no message'}", answer.status)

    def profile_email(self) -> str:
        email = self._call("GET", "profile").get("emailAddress")
        if not isinstance(email, str) or "@" not in email:
            raise GmailError("Gmail's profile answer had no email address")
        return email.lower()

    def labels(self) -> list[dict]:
        return [label for label in self._call("GET", "labels").get("labels") or [] if isinstance(label, dict)]

    def create_label(self, name: str) -> dict | None:
        try:
            return self._call("POST", "labels", payload={"name": name, "labelListVisibility": "labelShow",
                                                         "messageListVisibility": "show"})
        except GmailError as error:
            if error.status == CONFLICT:
                return None
            raise

    def message_ids(self, label_id: str, max_pages: int) -> tuple[list[str], bool]:
        ids: list[str] = []
        page_token = None
        for _ in range(max_pages):
            query = {"labelIds": label_id, "maxResults": PAGE_SIZE}
            if page_token:
                query["pageToken"] = page_token
            page = self._call("GET", "messages", query)
            ids.extend(item["id"] for item in page.get("messages") or [] if is_message_id(item.get("id")))
            page_token = page.get("nextPageToken")
            if not page_token:
                return ids, False
        return ids, True

    def message(self, message_id: str) -> dict:
        return self._call("GET", f"messages/{quote(message_id)}", {"format": "full"})

    def message_headers(self, message_id: str) -> dict:
        return self._call("GET", f"messages/{quote(message_id)}",
                          {"format": "metadata", "metadataHeaders": list(SHOWN_HEADERS)})

    def message_labels(self, message_id: str) -> dict:
        return self._call("GET", f"messages/{quote(message_id)}", {"format": "minimal"})

    def modify(self, message_id: str, add: list[str] = (), remove: list[str] = ()) -> dict:
        return self._call("POST", f"messages/{quote(message_id)}/modify",
                          payload={"addLabelIds": list(add), "removeLabelIds": list(remove)})

    def trash(self, message_id: str) -> dict:
        return self._call("POST", f"messages/{quote(message_id)}/trash")

    def untrash(self, message_id: str) -> dict:
        return self._call("POST", f"messages/{quote(message_id)}/untrash")
