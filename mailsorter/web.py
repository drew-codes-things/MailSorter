import hmac
import html
import logging
import secrets
import time
from pathlib import Path

from flask import Flask, Response, g, jsonify, redirect, request, send_from_directory
from werkzeug.exceptions import HTTPException

from . import __version__
from .accounts import SESSION_SECONDS, SIGN_IN_SECONDS, AccountStore
from .config import Settings
from .cleaner import IsolatedCleaner
from .gmail import AccountAccess, FixedAccess, GmailBusy, GmailClient, GmailError, MessageGone, is_message_id
from .google import (GMAIL_SCOPE, GoogleEndpoints, GoogleError, GoogleUnreachable, OAuthClient, SignInExpired,
                     SignInRefused, TokenGrant, new_pkce_pair, send)
from .limits import RateLimiter, client_address
from .present import Presenter
from .sanitize import as_document
from .secrets_store import SignInSeal, TokenUnreadable, csrf_matches, csrf_token
from .sorter import ACTIONS, VIEWS, NothingToUndo, Sorter, StateRegistry, UndoMismatch

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_REQUEST_BYTES = 64 * 1024
SESSION_COOKIE = "mail_sorter_session"
SIGN_IN_COOKIE = "mail_sorter_signin"
HOST_ONLY_PREFIX = "__Host-"
STATE_BYTES = 32
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
AUTH_REQUESTS_PER_WINDOW = 30
AUTH_WINDOW_SECONDS = 60
HSTS_VALUE = "max-age=31536000"
SIGN_IN_REASONS = {
    "expired": "Your Google sign-in ran out or was taken back. Sign in again to carry on.",
    "denied": "Google sign-in was cancelled, so nothing changed.",
    "scope": "Mail Sorter needs the Gmail permission to read and sort your mail. Sign in again and tick the Gmail box.",
    "retry": "That sign-in link was old or used already. Start again.",
    "google": "Google did not finish the sign-in. Try again in a moment.",
    "account": "That Google account is not allowed on this Mail Sorter.",
    "signedout": "You are signed out, and Mail Sorter's access to your Gmail was taken back.",
    "notrevoked": ("You are signed out and Mail Sorter deleted its copy of your Google sign-in, but Google could not "
                   "be reached to take the access back. To be sure, remove Mail Sorter at "
                   "https://myaccount.google.com/connections."),
    "again": "Google ended your earlier sign-in together with this one. Sign in once more to carry on.",
    "slow": "Too many sign-in attempts from your network. Wait a minute and try again.",
}
PAGE_POLICY = "; ".join((
    "default-src 'self'", "script-src 'self'", "style-src 'self'", "font-src 'self'", "img-src 'self' data:",
    "connect-src 'self'", "frame-src 'self'", "object-src 'none'", "frame-ancestors 'none'", "base-uri 'none'",
    "form-action 'self'",
))
EMAIL_POLICY_PARTS = ("default-src 'none'", "style-src 'unsafe-inline'", "font-src 'none'", "frame-ancestors 'self'",
                      "form-action 'none'", "base-uri 'none'", "sandbox allow-popups allow-popups-to-escape-sandbox")
BLOCKED_IMAGES = "img-src data:"
SHOWN_IMAGES = "img-src data: https:"

log = logging.getLogger("mailsorter.web")


def email_policy(show_images: bool) -> str:
    return "; ".join((*EMAIL_POLICY_PARTS, SHOWN_IMAGES if show_images else BLOCKED_IMAGES))


def _page(name: str, **values: str) -> Response:
    text = (STATIC_DIR / name).read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", html.escape(value))
    response = Response(text, mimetype="text/html")
    response.headers["Cache-Control"] = "no-store"
    return response


def _error(message: str, status: int, **extra):
    return jsonify({"error": message, **extra}), status


def cookie_name(name: str, secure: bool) -> str:
    return f"{HOST_ONLY_PREFIX}{name}" if secure else name


class Services:
    def __init__(self, settings: Settings, store: AccountStore, endpoints: GoogleEndpoints, transport, sleep, clock,
                 monotonic, cleaner):
        self.settings = settings
        self.store = store
        self.endpoints = endpoints
        self.transport = transport
        self.sleep = sleep
        self.clock = clock
        self.monotonic = monotonic
        self.states = StateRegistry(monotonic)
        self.presenter = Presenter(cleaner)
        self.oauth = OAuthClient(settings.google_client_id, settings.google_client_secret, settings.redirect_uri,
                                 endpoints, clock=clock, transport=transport)

    def sorter_for(self, email: str) -> Sorter:
        state = self.states.get(email)
        access = AccountAccess(self.store, self.oauth, email, clock=self.clock)
        gmail = GmailClient(access, self.endpoints, transport=self.transport, sleep=self.sleep)
        return Sorter(gmail, self.store, state, email, self.presenter, clock=self.clock, monotonic=self.monotonic)

    def email_of(self, access_token: str) -> str:
        return GmailClient(FixedAccess(access_token), self.endpoints, transport=self.transport,
                           sleep=self.sleep).profile_email()


def _forget_unlisted_accounts(settings: Settings, store: AccountStore) -> None:
    if not settings.allowed_accounts:
        return
    removed = store.forget_accounts_except(settings.allowed_accounts)
    if removed:
        log.info("removed the tokens and sessions of %d account(s) no longer in ALLOWED_ACCOUNTS", removed)


def create_app(settings: Settings, store: AccountStore, secret: bytes, endpoints: GoogleEndpoints = GoogleEndpoints(),
               transport=send, sleep=time.sleep, clock=time.time, monotonic=time.monotonic, cleaner=None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
    services = Services(settings, store, endpoints, transport, sleep, clock, monotonic, cleaner or IsolatedCleaner())
    seal = SignInSeal(secret, clock)
    auth_limiter = RateLimiter(AUTH_REQUESTS_PER_WINDOW, AUTH_WINDOW_SECONDS, clock=clock)
    session_cookie = cookie_name(SESSION_COOKIE, settings.secure_cookies)
    sign_in_cookie = cookie_name(SIGN_IN_COOKIE, settings.secure_cookies)
    _forget_unlisted_accounts(settings, store)

    def set_cookie(response: Response, name: str, value: str, max_age: int) -> None:
        response.set_cookie(name, value, max_age=max_age, path="/", httponly=True, samesite="Lax",
                            secure=settings.secure_cookies)

    def clear_session_cookie(response: Response) -> Response:
        response.delete_cookie(session_cookie, path="/", httponly=True, samesite="Lax", secure=settings.secure_cookies)
        return response

    def forget_sign_in_cookie(response: Response) -> Response:
        response.delete_cookie(sign_in_cookie, path="/", httponly=True, samesite="Lax", secure=settings.secure_cookies)
        return response

    def back_to_sign_in(reason: str) -> Response:
        return forget_sign_in_cookie(redirect(f"/?why={reason}", code=303))

    def refuse_signed_out():
        response, status = _error("sign in first", 401, signIn=True)
        return clear_session_cookie(response), status

    def is_same_origin() -> bool:
        origin = request.headers.get("Origin")
        return origin is None or origin.rstrip("/").lower() == settings.public_url

    def needs_session() -> bool:
        return (request.path.startswith("/api/") and request.path != "/api/health") or request.path == "/auth/signout"

    def is_allowed(email: str | None) -> bool:
        return email is not None and (not settings.allowed_accounts or email in settings.allowed_accounts)

    def signed_in_email() -> str | None:
        if settings.google_problem is not None:
            return None
        email = store.session_email(g.session_id)
        if email is not None and not is_allowed(email):
            store.end_session(g.session_id)
            log.info("a session of an account no longer in ALLOWED_ACCOUNTS was ended")
            return None
        return email

    def slow_down():
        if request.method in SAFE_METHODS:
            return back_to_sign_in("slow")
        response, status = _error("Too many sign-in requests. Wait a minute and try again.", 429)
        response.headers["Retry-After"] = str(AUTH_WINDOW_SECONDS)
        return response, status

    @app.before_request
    def identify_and_guard_writes():
        if request.path.startswith("/auth/") and not auth_limiter.allow(client_address(request.remote_addr,
                                                                                       request.headers)):
            return slow_down()
        g.session_id = request.cookies.get(session_cookie) or ""
        g.email = signed_in_email()
        if not needs_session():
            return None
        if settings.google_problem is not None:
            return _error(settings.google_problem, 503, notSetUp=True)
        if g.email is None:
            return refuse_signed_out()
        if request.method in SAFE_METHODS:
            return None
        if not is_same_origin():
            return _error("this request came from another site", 403)
        if not csrf_matches(secret, g.session_id, request.headers.get(CSRF_HEADER)):
            return _error("missing or wrong CSRF token, reload the page", 403)
        return None

    @app.after_request
    def add_security_headers(response: Response) -> Response:
        response.headers.setdefault("Content-Security-Policy", PAGE_POLICY)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if settings.secure_cookies:
            response.headers["Strict-Transport-Security"] = HSTS_VALUE
        if request.path.startswith("/api/") or request.path.startswith("/auth/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(SignInExpired)
    def sign_in_expired(error: SignInExpired):
        services.states.drop(g.get("email") or "")
        response, status = _error("Your Google sign-in ran out. Sign in again.", 401, signIn=True)
        return clear_session_cookie(response), status

    @app.errorhandler(TokenUnreadable)
    def token_unreadable(error: TokenUnreadable):
        log.error("%s; the account was signed out", error)
        if g.get("email"):
            store.forget_account(g.email)
        return sign_in_expired(SignInExpired(str(error)))

    @app.errorhandler(GmailBusy)
    def gmail_busy(error: GmailBusy):
        log.warning("Gmail busy: %s", error)
        return _error("Gmail is busy right now. Wait a moment and try again.", 503, retry=True)

    @app.errorhandler(MessageGone)
    def message_gone(error: MessageGone):
        return _error("That email was moved or deleted somewhere else, so it was skipped.", 410, gone=True)

    @app.errorhandler(NothingToUndo)
    def nothing_to_undo(error: NothingToUndo):
        return _error("There is nothing to undo.", 409)

    @app.errorhandler(UndoMismatch)
    def undo_mismatch(error: UndoMismatch):
        return _error("That choice can no longer be undone: another choice came after it.", 409, stale=True)

    @app.errorhandler(GoogleUnreachable)
    def google_unreachable(error: GoogleUnreachable):
        log.warning("%s", error)
        return _error("Could not reach Google. Check the connection and try again.", 502, retry=True)

    @app.errorhandler(GmailError)
    def gmail_failed(error: GmailError):
        log.warning("Gmail refused a request: %s", error)
        return _error(f"Gmail refused that: {error}", 502)

    @app.errorhandler(GoogleError)
    def google_failed(error: GoogleError):
        log.warning("Google refused a request: %s", error)
        return _error(f"Google refused that: {error}", 502, retry=True)

    @app.errorhandler(HTTPException)
    def http_error(error: HTTPException):
        if request.path.startswith("/api/") or error.code == 413:
            return _error(error.description or error.name, error.code or 500)
        return error

    @app.get("/")
    def index():
        if settings.google_problem is not None:
            return _page("notready.html", problem=settings.google_problem)
        if g.email is None:
            page = _page("signin.html", reason=SIGN_IN_REASONS.get(request.args.get("why", ""), ""))
            return clear_session_cookie(page) if g.session_id else page
        return _page("index.html", account=g.email)

    @app.get("/static/<path:name>")
    def static_file(name: str):
        return send_from_directory(STATIC_DIR, name, max_age=0)

    @app.get("/api/health")
    def health():
        if not store.database.is_reachable():
            log.error("health check: the database could not be read at %s", store.database.path)
            return jsonify({"ok": False, "error": "database unavailable"}), 503
        return jsonify({"ok": True, "googleReady": settings.google_problem is None})

    @app.get("/auth/google")
    def start_sign_in():
        if settings.google_problem is not None:
            return redirect("/", code=303)
        verifier, challenge = new_pkce_pair()
        state = secrets.token_urlsafe(STATE_BYTES)
        response = redirect(services.oauth.authorization_url(state, challenge), code=303)
        set_cookie(response, sign_in_cookie, seal.seal(state, verifier), SIGN_IN_SECONDS)
        return response

    def signed_in(email: str) -> Response:
        session_id = store.start_session(email)
        services.states.drop(email)
        response = forget_sign_in_cookie(redirect("/", code=303))
        set_cookie(response, session_cookie, session_id, SESSION_SECONDS)
        return response

    def stored_refresh_token(email: str) -> str | None:
        try:
            tokens = store.tokens(email)
        except TokenUnreadable:
            return None
        return tokens.refresh_token if tokens else None

    def kept_new_grant_after_revoking(email: str, previous: str, grant: TokenGrant) -> bool:
        if not services.oauth.revoke(previous):
            return True
        try:
            store.save_access(email, services.oauth.refresh(grant.refresh_token))
        except SignInExpired:
            store.forget_account(email)
            log.warning("revoking the earlier Google sign-in also ended the new one, so the person signs in again")
            return False
        except GoogleError as error:
            log.warning("could not check the new sign-in after revoking the earlier one: %s", error)
        return True

    @app.get("/auth/callback")
    def finish_sign_in():
        if settings.google_problem is not None:
            return redirect("/", code=303)
        state = request.args.get("state") or ""
        opened = seal.open(request.cookies.get(sign_in_cookie), SIGN_IN_SECONDS)
        if not state or opened is None or not hmac.compare_digest(state.encode(), opened[0].encode()):
            return back_to_sign_in("retry")
        verifier = opened[1]
        if request.args.get("error"):
            return back_to_sign_in("denied")
        code = request.args.get("code") or ""
        if not code:
            return back_to_sign_in("retry")
        try:
            grant = services.oauth.exchange_code(code, verifier)
        except (SignInRefused, GoogleError) as error:
            log.warning("sign-in did not finish: %s", error)
            return back_to_sign_in("google")
        if GMAIL_SCOPE not in grant.scopes or not grant.refresh_token:
            services.oauth.revoke(grant.refresh_token or grant.access_token)
            return back_to_sign_in("scope" if GMAIL_SCOPE not in grant.scopes else "google")
        try:
            email = services.email_of(grant.access_token)
        except (GmailError, GoogleError) as error:
            log.warning("could not read the signed-in account's address: %s", error)
            services.oauth.revoke(grant.refresh_token)
            return back_to_sign_in("google")
        if not is_allowed(email):
            services.oauth.revoke(grant.refresh_token)
            return back_to_sign_in("account")
        previous = stored_refresh_token(email)
        store.save_sign_in(email, grant)
        if previous and previous != grant.refresh_token and not kept_new_grant_after_revoking(email, previous, grant):
            return back_to_sign_in("again")
        return signed_in(email)

    @app.post("/auth/signout")
    def sign_out():
        tokens = store.tokens(g.email)
        revoked = services.oauth.revoke(tokens.refresh_token) if tokens else False
        store.forget_account(g.email)
        services.states.drop(g.email)
        return clear_session_cookie(jsonify({"signedOut": True, "revoked": revoked}))

    @app.get("/api/session")
    def session_state():
        return jsonify({"account": g.email, "csrfToken": csrf_token(secret, g.session_id), "version": __version__,
                        "lastChoice": services.sorter_for(g.email).last_choice()})

    @app.get("/api/counts")
    def counts():
        return jsonify(services.sorter_for(g.email).counts())

    @app.get("/api/next")
    def next_message():
        view = request.args.get("view", "inbox")
        if view not in VIEWS:
            return _error(f"view must be one of {', '.join(VIEWS)}", 400)
        sorter = services.sorter_for(g.email)
        message = sorter.next_message(view)
        return jsonify({"message": message, "counts": sorter.counts()})

    @app.get("/api/messages/<message_id>/body")
    def message_body(message_id: str):
        if not is_message_id(message_id):
            return _error("that is not a Gmail message id", 404)
        body = services.sorter_for(g.email).body(message_id)
        response = Response(as_document(body.html), mimetype="text/html")
        response.headers["Content-Security-Policy"] = email_policy(request.args.get("images") == "1")
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/messages/<message_id>/<action>")
    def act_on_message(message_id: str, action: str):
        if not is_message_id(message_id):
            return _error("that is not a Gmail message id", 404)
        if action not in ACTIONS:
            return _error(f"action must be one of {', '.join(ACTIONS)}", 404)
        return jsonify(services.sorter_for(g.email).act(message_id, action))

    @app.post("/api/undo")
    def undo():
        choice = request.get_json(silent=True)
        if not isinstance(choice, dict) or not is_message_id(choice.get("id")) or choice.get("action") not in ACTIONS:
            return _error("undo needs the id and the choice to undo, as JSON", 400)
        return jsonify(services.sorter_for(g.email).undo(choice["id"], choice["action"]))

    return app
