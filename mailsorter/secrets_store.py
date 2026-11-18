import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

SECRET_BYTES = 32
SECRET_FILE_NAME = "secret_key"
CSRF_PURPOSE = b"mail-sorter csrf\x00"
SIGN_IN_PURPOSE = b"mail-sorter sign-in\x00"
MAX_SEALED_LENGTH = 2000


class SecretKeyError(ValueError):
    pass


class TokenUnreadable(ValueError):
    pass


def _write_new_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)


def load_or_create_secret(data_dir: Path, configured: str) -> bytes:
    if configured:
        if len(configured.encode()) < SECRET_BYTES:
            raise SecretKeyError(f"SECRET_KEY holds {len(configured.encode())} bytes, fewer than the {SECRET_BYTES} a key "
                                 "needs; set a longer one or leave it empty so a key file is made")
        return configured.encode()
    path = Path(data_dir) / SECRET_FILE_NAME
    if not path.exists():
        _write_new_file(path, secrets.token_hex(SECRET_BYTES).encode())
    secret = path.read_bytes()
    if len(secret) < SECRET_BYTES:
        raise SecretKeyError(f"{path} holds {len(secret)} bytes, fewer than the {SECRET_BYTES} a key needs; "
                             "stop the app, delete the file and start it again to make a new key")
    return secret


def _is_fernet_key(raw: bytes) -> bool:
    try:
        return len(base64.urlsafe_b64decode(raw)) == SECRET_BYTES
    except (binascii.Error, ValueError):
        return False


def load_or_create_token_key(path: Path) -> bytes:
    path = Path(path)
    if not path.exists():
        _write_new_file(path, Fernet.generate_key())
    try:
        raw = path.read_bytes().strip()
    except OSError as error:
        raise SecretKeyError(f"the token key {path} cannot be read: {error.strerror}") from error
    if not _is_fernet_key(raw):
        raise SecretKeyError(f"{path} is not a token key (44 characters of URL-safe base64); the deploy script makes "
                             "one, or delete it to have a new one made, which signs everyone out")
    return raw


class TokenVault:
    def __init__(self, key: bytes):
        self._fernet = Fernet(key)

    def seal(self, token: str) -> bytes:
        return self._fernet.encrypt(token.encode())

    def open(self, sealed: bytes) -> str:
        try:
            return self._fernet.decrypt(bytes(sealed)).decode()
        except InvalidToken as error:
            raise TokenUnreadable("a stored Google token cannot be decrypted with the current token key") from error


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def csrf_token(secret: bytes, session_id: str) -> str:
    return hmac.new(secret, CSRF_PURPOSE + session_id.encode(), hashlib.sha256).hexdigest()


def csrf_matches(secret: bytes, session_id: str, presented) -> bool:
    if not isinstance(presented, str) or not presented:
        return False
    return hmac.compare_digest(csrf_token(secret, session_id), presented)


class SignInSeal:
    def __init__(self, secret: bytes, clock):
        key = base64.urlsafe_b64encode(hmac.new(secret, SIGN_IN_PURPOSE, hashlib.sha256).digest())
        self._fernet = Fernet(key)
        self.clock = clock

    def seal(self, state: str, verifier: str) -> str:
        sealed = json.dumps({"state": state, "verifier": verifier}).encode()
        return self._fernet.encrypt_at_time(sealed, int(self.clock())).decode()

    def open(self, sealed: str | None, max_age_seconds: int) -> tuple[str, str] | None:
        if not sealed or len(sealed) > MAX_SEALED_LENGTH:
            return None
        try:
            opened = json.loads(self._fernet.decrypt_at_time(sealed.encode(), max_age_seconds, int(self.clock())))
        except (InvalidToken, ValueError, UnicodeError):
            return None
        if not isinstance(opened, dict):
            return None
        state, verifier = opened.get("state"), opened.get("verifier")
        if not isinstance(state, str) or not isinstance(verifier, str) or not state or not verifier:
            return None
        return state, verifier
