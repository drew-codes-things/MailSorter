import base64
import hashlib
import http.client
import json
import logging
import secrets
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
REQUEST_TIMEOUT_SECONDS = 20
MAX_RESPONSE_BYTES = 6 * 1024 * 1024
READ_LIMIT_BYTES = MAX_RESPONSE_BYTES + 1
PKCE_VERIFIER_BYTES = 64
USER_AGENT = "mail-sorter"

log = logging.getLogger("mailsorter.google")


@dataclass(frozen=True)
class GoogleEndpoints:
    authorize: str = "https://accounts.google.com/o/oauth2/v2/auth"
    token: str = "https://oauth2.googleapis.com/token"
    revoke: str = "https://oauth2.googleapis.com/revoke"
    gmail: str = "https://gmail.googleapis.com/gmail/v1"


class GoogleError(Exception):
    pass


class GoogleUnreachable(GoogleError):
    pass


class SignInExpired(GoogleError):
    pass


class SignInRefused(GoogleError):
    pass


@dataclass(frozen=True)
class HttpAnswer:
    status: int
    headers: dict
    body: bytes

    def json(self) -> dict:
        try:
            data = json.loads(self.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}


@dataclass(frozen=True)
class TokenGrant:
    access_token: str
    expires_at: float
    refresh_token: str | None
    scopes: frozenset


def send(method: str, url: str, headers: dict | None = None, body: bytes | None = None,
         timeout: float = REQUEST_TIMEOUT_SECONDS) -> HttpAnswer:
    request = Request(url, data=body, method=method, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urlopen(request, timeout=timeout) as response:
            return HttpAnswer(response.status, {k.lower(): v for k, v in response.headers.items()},
                              response.read(READ_LIMIT_BYTES))
    except HTTPError as error:
        try:
            body = error.read(READ_LIMIT_BYTES)
        finally:
            error.close()
        return HttpAnswer(error.code, {k.lower(): v for k, v in (error.headers or {}).items()}, body)
    except (URLError, TimeoutError, ConnectionError, http.client.HTTPException) as error:
        raise GoogleUnreachable(f"could not reach Google at {url.split('?')[0]}: {getattr(error, 'reason', error)}") from error


def new_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(PKCE_VERIFIER_BYTES)
    return verifier, pkce_challenge(verifier)


def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


class OAuthClient:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str, endpoints: GoogleEndpoints,
                 clock=time.time, transport=send):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.endpoints = endpoints
        self.clock = clock
        self.transport = transport

    def authorization_url(self, state: str, challenge: str) -> str:
        query = urlencode({
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": GMAIL_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        return f"{self.endpoints.authorize}?{query}"

    def _post_token(self, fields: dict) -> HttpAnswer:
        body = urlencode({"client_id": self.client_id, "client_secret": self.client_secret, **fields}).encode()
        return self.transport("POST", self.endpoints.token, {"Content-Type": "application/x-www-form-urlencoded"}, body)

    def _grant(self, answer: HttpAnswer, refresh_token: str | None) -> TokenGrant:
        data = answer.json()
        access_token = data.get("access_token")
        expires_in = data.get("expires_in")
        if not isinstance(access_token, str) or not access_token or not isinstance(expires_in, (int, float)):
            raise GoogleError(f"Google's token answer had no access token (status {answer.status})")
        new_refresh = data.get("refresh_token")
        return TokenGrant(
            access_token=access_token,
            expires_at=self.clock() + float(expires_in),
            refresh_token=new_refresh if isinstance(new_refresh, str) and new_refresh else refresh_token,
            scopes=frozenset(str(data.get("scope") or "").split()),
        )

    def exchange_code(self, code: str, verifier: str) -> TokenGrant:
        answer = self._post_token({"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                                   "redirect_uri": self.redirect_uri})
        if answer.status != 200:
            reason = answer.json().get("error") or f"status {answer.status}"
            raise SignInRefused(f"Google refused the sign-in code: {reason}")
        return self._grant(answer, None)

    def refresh(self, refresh_token: str) -> TokenGrant:
        answer = self._post_token({"grant_type": "refresh_token", "refresh_token": refresh_token})
        if answer.status in (400, 401) and answer.json().get("error") in ("invalid_grant", "unauthorized_client"):
            raise SignInExpired("Google no longer accepts this sign-in; sign in again")
        if answer.status != 200:
            raise GoogleError(f"Google's token service answered {answer.status} while renewing access")
        return self._grant(answer, refresh_token)

    def revoke(self, token: str) -> bool:
        try:
            answer = self.transport("POST", self.endpoints.revoke, {"Content-Type": "application/x-www-form-urlencoded"},
                                    urlencode({"token": token}).encode())
        except GoogleUnreachable as error:
            log.warning("revoking the Google token failed: %s", error)
            return False
        if answer.status != 200:
            log.warning("Google answered %s to the token revoke", answer.status)
            return False
        return True
