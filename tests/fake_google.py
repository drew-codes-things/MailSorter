import base64
import hashlib
import itertools
import secrets
import threading
from dataclasses import dataclass, field
from urllib.parse import urlencode

from flask import Flask, jsonify, redirect, request
from werkzeug.serving import make_server

from mailsorter.google import GMAIL_SCOPE, GoogleEndpoints

CLIENT_ID = "1234-fake.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-fake-secret"
SYSTEM_LABELS = ("INBOX", "TRASH", "SPAM", "SENT", "UNREAD", "IMPORTANT")
HOUR_MS = 3_600_000
FIRST_DATE_MS = 1_763_000_000_000


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _part(mime_type: str, content: str, charset: str = "utf-8") -> dict:
    return {"mimeType": mime_type, "filename": "", "headers": [{"name": "Content-Type", "value": f"{mime_type}; charset={charset}"}],
            "body": {"size": len(content.encode()), "data": _b64(content)}}


@dataclass
class Failure:
    method: str
    path_part: str
    status: int
    times: int
    body: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)


@dataclass
class Mailbox:
    email: str
    messages: dict = field(default_factory=dict)
    labels: dict = field(default_factory=dict)
    order: list = field(default_factory=list)

    def label_id(self, name: str) -> str | None:
        return next((label_id for label_id, label_name in self.labels.items() if label_name == name), None)

    def names_of(self, message_id: str) -> set:
        return {self.labels.get(label_id, label_id) for label_id in self.messages[message_id]["labelIds"]}


class FakeGoogle:
    def __init__(self, clock):
        self.clock = clock
        self.mailboxes: dict[str, Mailbox] = {}
        self.codes: dict[str, dict] = {}
        self.access_tokens: dict[str, dict] = {}
        self.refresh_tokens: dict[str, str] = {}
        self.revoked: list[str] = []
        self.failures: list[Failure] = []
        self.calls: list[tuple[str, str]] = []
        self.next_account = "drew@example.com"
        self.granted_scope = GMAIL_SCOPE
        self.deny = False
        self.revoke_ends_grant = False
        self.give_refresh_token = True
        self.access_lifetime = 3600
        self.last_authorize: dict = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self.app = self._build_app()
        self._server = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.base = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self._thread.start()

    @property
    def endpoints(self) -> GoogleEndpoints:
        return GoogleEndpoints(authorize=f"{self.base}/o/oauth2/v2/auth", token=f"{self.base}/token",
                               revoke=f"{self.base}/revoke", gmail=f"{self.base}/gmail/v1")

    def close(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)

    def mailbox(self, email: str) -> Mailbox:
        if email not in self.mailboxes:
            self.mailboxes[email] = Mailbox(email, labels={name: name for name in SYSTEM_LABELS})
        return self.mailboxes[email]

    def add_message(self, email: str, subject: str, sender: str = "Shop <news@shop.example>", html: str | None = None,
                    text: str | None = None, attachments: tuple = (), labels: tuple = ("INBOX", "UNREAD"),
                    snippet: str = "") -> str:
        box = self.mailbox(email)
        number = next(self._ids)
        message_id = f"{number:016x}"
        bodies = []
        if text is not None:
            bodies.append(_part("text/plain", text))
        if html is not None:
            bodies.append(_part("text/html", html))
        alternative = {"mimeType": "multipart/alternative", "filename": "", "headers": [], "body": {"size": 0}, "parts": bodies}
        files = [{"mimeType": kind, "filename": name, "headers": [{"name": "Content-Disposition", "value": f"attachment; filename=\"{name}\""}],
                  "body": {"size": size, "attachmentId": f"att{number}{index}"}} for index, (name, kind, size) in enumerate(attachments)]
        payload = {"mimeType": "multipart/mixed", "filename": "", "body": {"size": 0}, "parts": [alternative, *files],
                   "headers": [{"name": "From", "value": sender}, {"name": "To", "value": email},
                               {"name": "Subject", "value": subject}]}
        box.messages[message_id] = {"id": message_id, "threadId": message_id, "labelIds": list(labels),
                                    "snippet": snippet or subject, "internalDate": str(FIRST_DATE_MS + number * HOUR_MS),
                                    "payload": payload}
        box.order.insert(0, message_id)
        return message_id

    def delete_message(self, email: str, message_id: str) -> None:
        box = self.mailbox(email)
        box.messages.pop(message_id, None)
        box.order.remove(message_id)

    def fail(self, method: str, path_part: str, status: int, times: int = 1, body: dict | None = None,
             headers: dict | None = None) -> None:
        self.failures.append(Failure(method, path_part, status, times, body or {}, headers or {}))

    def expire_access_tokens(self) -> None:
        for token in self.access_tokens.values():
            token["expires_at"] = 0

    def gmail_calls(self, method: str | None = None, path_part: str = "") -> list:
        return [call for call in self.calls if (method is None or call[0] == method) and path_part in call[1]]

    def _injected_failure(self):
        for failure in self.failures:
            if failure.times > 0 and failure.method == request.method and failure.path_part in request.path:
                failure.times -= 1
                answer = jsonify(failure.body or {"error": {"code": failure.status, "message": "injected"}})
                answer.status_code = failure.status
                answer.headers.update(failure.headers)
                return answer
        return None

    def _account_for_request(self) -> str | None:
        header = request.headers.get("Authorization", "")
        token = self.access_tokens.get(header.removeprefix("Bearer "))
        if not header.startswith("Bearer ") or token is None or token["revoked"] or token["expires_at"] <= self.clock():
            return None
        return token["email"]

    def _issue_access(self, email: str) -> str:
        token = secrets.token_urlsafe(16)
        self.access_tokens[token] = {"email": email, "expires_at": self.clock() + self.access_lifetime, "revoked": False}
        return token

    def _build_app(self) -> Flask:
        app = Flask("fake_google")
        google = self

        def gmail_error(status: int, message: str, reason: str = "failed"):
            answer = jsonify({"error": {"code": status, "message": message, "errors": [{"reason": reason}]}})
            answer.status_code = status
            return answer

        @app.get("/o/oauth2/v2/auth")
        def authorize():
            google.last_authorize = dict(request.args)
            redirect_uri = request.args["redirect_uri"]
            state = request.args.get("state", "")
            if google.deny:
                return redirect(f"{redirect_uri}?{urlencode({'error': 'access_denied', 'state': state})}")
            code = secrets.token_urlsafe(12)
            google.codes[code] = {"email": google.next_account, "challenge": request.args.get("code_challenge"),
                                  "method": request.args.get("code_challenge_method"), "redirect_uri": redirect_uri,
                                  "client_id": request.args.get("client_id")}
            return redirect(f"{redirect_uri}?{urlencode({'code': code, 'state': state, 'scope': google.granted_scope})}")

        @app.post("/token")
        def token():
            form = request.form
            if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
                return jsonify({"error": "invalid_client"}), 401
            if form.get("grant_type") == "authorization_code":
                grant = google.codes.pop(form.get("code", ""), None)
                verifier = form.get("code_verifier", "")
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                if (grant is None or grant["method"] != "S256" or grant["challenge"] != challenge
                        or grant["redirect_uri"] != form.get("redirect_uri")):
                    return jsonify({"error": "invalid_grant"}), 400
                answer = {"access_token": google._issue_access(grant["email"]), "expires_in": google.access_lifetime,
                          "scope": google.granted_scope, "token_type": "Bearer"}
                if google.give_refresh_token:
                    refresh = secrets.token_urlsafe(16)
                    google.refresh_tokens[refresh] = grant["email"]
                    answer["refresh_token"] = refresh
                return jsonify(answer)
            if form.get("grant_type") == "refresh_token":
                email = google.refresh_tokens.get(form.get("refresh_token", ""))
                if email is None:
                    return jsonify({"error": "invalid_grant", "error_description": "Token has been expired or revoked."}), 400
                return jsonify({"access_token": google._issue_access(email), "expires_in": google.access_lifetime,
                                "scope": google.granted_scope, "token_type": "Bearer"})
            return jsonify({"error": "unsupported_grant_type"}), 400

        @app.post("/revoke")
        def revoke():
            token_value = request.form.get("token", "")
            google.revoked.append(token_value)
            email = google.refresh_tokens.pop(token_value, None)
            if email is None and token_value not in google.access_tokens:
                return jsonify({"error": "invalid_token"}), 400
            if google.revoke_ends_grant:
                for other in [token for token, owner in google.refresh_tokens.items() if owner == email]:
                    del google.refresh_tokens[other]
            for access in google.access_tokens.values():
                if access["email"] == email:
                    access["revoked"] = True
            return jsonify({})

        @app.before_request
        def gmail_guard():
            if not request.path.startswith("/gmail/v1/"):
                return None
            with google._lock:
                google.calls.append((request.method, request.full_path))
            if request.method == "POST" and request.headers.get("Content-Length") is None:
                return gmail_error(411, "Length required")
            email = google._account_for_request()
            if email is None:
                return gmail_error(401, "Request had invalid authentication credentials.", "authError")
            request.environ["fake.email"] = email
            return google._injected_failure()

        def box() -> Mailbox:
            return google.mailbox(request.environ["fake.email"])

        @app.get("/gmail/v1/users/me/profile")
        def profile():
            return jsonify({"emailAddress": request.environ["fake.email"], "messagesTotal": len(box().messages)})

        @app.get("/gmail/v1/users/me/labels")
        def labels():
            return jsonify({"labels": [{"id": label_id, "name": name, "type": "system" if label_id == name else "user"}
                                       for label_id, name in box().labels.items()]})

        @app.post("/gmail/v1/users/me/labels")
        def create_label():
            name = (request.get_json(silent=True) or {}).get("name")
            if box().label_id(name):
                return gmail_error(409, "Label name exists or conflicts", "duplicate")
            label_id = f"Label_{len(box().labels)}"
            box().labels[label_id] = name
            return jsonify({"id": label_id, "name": name, "type": "user"})

        @app.get("/gmail/v1/users/me/messages")
        def list_messages():
            wanted = request.args.get("labelIds")
            size = int(request.args.get("maxResults", 100))
            start = int(request.args.get("pageToken") or 0)
            ids = [message_id for message_id in box().order
                   if wanted in box().messages[message_id]["labelIds"] and "TRASH" not in box().messages[message_id]["labelIds"]]
            page = ids[start:start + size]
            answer = {"resultSizeEstimate": len(page)}
            if page:
                answer["messages"] = [{"id": message_id, "threadId": message_id} for message_id in page]
            if start + size < len(ids):
                answer["nextPageToken"] = str(start + size)
            return jsonify(answer)

        def message_or_404(message_id: str):
            message = box().messages.get(message_id)
            if message is None:
                return None, gmail_error(404, "Requested entity was not found.", "notFound")
            return message, None

        def in_format(message: dict) -> dict:
            wanted = request.args.get("format", "full")
            if wanted == "minimal":
                return {key: value for key, value in message.items() if key != "payload"}
            if wanted == "metadata":
                names = {name.lower() for name in request.args.getlist("metadataHeaders")}
                headers = [header for header in message["payload"]["headers"] if header["name"].lower() in names]
                return {**message, "payload": {"mimeType": message["payload"]["mimeType"], "headers": headers}}
            return message

        @app.get("/gmail/v1/users/me/messages/<message_id>")
        def get_message(message_id):
            message, missing = message_or_404(message_id)
            return missing or jsonify(in_format(message))

        @app.post("/gmail/v1/users/me/messages/<message_id>/modify")
        def modify(message_id):
            message, missing = message_or_404(message_id)
            if missing:
                return missing
            body = request.get_json(silent=True) or {}
            for label_id in [*body.get("addLabelIds", []), *body.get("removeLabelIds", [])]:
                if label_id not in box().labels:
                    return gmail_error(400, f"Invalid label: {label_id}", "invalidArgument")
            labels = [label for label in message["labelIds"] if label not in body.get("removeLabelIds", [])]
            labels += [label for label in body.get("addLabelIds", []) if label not in labels]
            message["labelIds"] = labels
            return jsonify({"id": message_id, "labelIds": labels})

        @app.post("/gmail/v1/users/me/messages/<message_id>/trash")
        def trash(message_id):
            message, missing = message_or_404(message_id)
            if missing:
                return missing
            message["labelIds"] = [label for label in message["labelIds"] if label != "INBOX"] + ["TRASH"]
            return jsonify({"id": message_id, "labelIds": message["labelIds"]})

        @app.post("/gmail/v1/users/me/messages/<message_id>/untrash")
        def untrash(message_id):
            message, missing = message_or_404(message_id)
            if missing:
                return missing
            message["labelIds"] = [label for label in message["labelIds"] if label != "TRASH"]
            return jsonify({"id": message_id, "labelIds": message["labelIds"]})

        @app.route("/gmail/v1/users/me/messages/<message_id>", methods=["DELETE"])
        @app.post("/gmail/v1/users/me/messages/<message_id>/delete")
        def permanent_delete(message_id):
            google.calls.append(("PERMANENT-DELETE", message_id))
            return gmail_error(403, "Request had insufficient authentication scopes.", "insufficientPermissions")

        return app
