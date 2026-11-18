import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

ENV_FILE = Path(".env")
DEFAULT_PORT = 8101
DEFAULT_PUBLIC_URL = f"http://127.0.0.1:{DEFAULT_PORT}"
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,200}\.apps\.googleusercontent\.com$")
CLIENT_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=-]{8,200}$")
ACCOUNT_PATTERN = re.compile(r"^[^@\s,]{1,64}@[^@\s,]{1,253}$")
NOT_SET_UP_TEXT = ("Google sign-in is not set up yet: GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are empty. "
                   "Run ./deploy.sh google from the laptop, or see Google setup in the README.")
BAD_CLIENT_ID_TEXT = ("GOOGLE_CLIENT_ID does not look like a Google OAuth client id "
                      "(it ends in .apps.googleusercontent.com). Paste it again with ./deploy.sh google.")
BAD_CLIENT_SECRET_TEXT = ("GOOGLE_CLIENT_SECRET does not look like a Google client secret "
                          "(letters, digits and - _ only, no spaces). Paste it again with ./deploy.sh google.")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    public_url: str = DEFAULT_PUBLIC_URL
    bind_host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    google_client_id: str = ""
    google_client_secret: str = ""
    token_key_file: Path | None = None
    allowed_accounts: tuple[str, ...] = ()
    secret_key: str = ""

    @property
    def database_path(self) -> Path:
        return self.data_dir / "mail-sorter.db"

    @property
    def key_path(self) -> Path:
        return self.token_key_file or self.data_dir / "token.key"

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_url}/auth/callback"

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def google_problem(self) -> str | None:
        if not self.google_client_id or not self.google_client_secret:
            return NOT_SET_UP_TEXT
        if not CLIENT_ID_PATTERN.match(self.google_client_id):
            return BAD_CLIENT_ID_TEXT
        if not CLIENT_SECRET_PATTERN.match(self.google_client_secret):
            return BAD_CLIENT_SECRET_TEXT
        return None


def _text(name: str, default: str) -> str:
    value = os.getenv(name)
    return (default if value is None else value).strip()


def _number(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = _text(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from error
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _public_url(name: str) -> str:
    raw = _text(name, DEFAULT_PUBLIC_URL).rstrip("/")
    try:
        parts = urlsplit(raw)
        host = parts.hostname or ""
        parts.port
    except ValueError as error:
        raise ConfigError(f"{name} is not a web address: {raw!r}") from error
    if parts.scheme not in ("http", "https") or not host:
        raise ConfigError(f"{name} must start with https:// and name a host, got {raw!r}")
    if parts.path or parts.query or parts.fragment or parts.username or parts.password:
        raise ConfigError(f"{name} must be only scheme and host, such as https://mail.example.com, got {raw!r}")
    if parts.scheme == "http" and not is_loopback(host):
        raise ConfigError(f"{name} must use https:// for a host other than localhost, got {raw!r}; "
                          "Google only accepts plain http redirects for localhost")
    return f"{parts.scheme}://{parts.netloc.lower()}"


def _accounts(name: str) -> tuple[str, ...]:
    entries = tuple(part.strip().lower() for part in _text(name, "").split(",") if part.strip())
    for entry in entries:
        if not ACCOUNT_PATTERN.match(entry):
            raise ConfigError(f"{name} has {entry!r}, which is not an email address")
    return entries


def read_env_file(path: Path = ENV_FILE) -> None:
    if not path.is_file():
        return
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not name.strip():
            raise ConfigError(f"{path}:{number} should look like NAME=value")
        os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def load_settings() -> Settings:
    read_env_file()
    key_file = _text("TOKEN_KEY_FILE", "")
    return Settings(
        data_dir=Path(_text("DATA_DIR", "./data")).resolve(),
        public_url=_public_url("PUBLIC_URL"),
        bind_host=_text("BIND_HOST", "127.0.0.1"),
        port=_number("PORT", DEFAULT_PORT, 1, 65535),
        google_client_id=_text("GOOGLE_CLIENT_ID", ""),
        google_client_secret=_text("GOOGLE_CLIENT_SECRET", ""),
        token_key_file=Path(key_file).resolve() if key_file else None,
        allowed_accounts=_accounts("ALLOWED_ACCOUNTS"),
        secret_key=_text("SECRET_KEY", ""),
    )
