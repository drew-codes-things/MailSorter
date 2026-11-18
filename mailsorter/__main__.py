import logging
import sys

from waitress import serve

from .accounts import AccountStore
from .config import ConfigError, load_settings
from .db import Database
from .secrets_store import SecretKeyError, TokenVault, load_or_create_secret, load_or_create_token_key
from .web import MAX_REQUEST_BYTES, create_app

WAITRESS_THREADS = 6

log = logging.getLogger("mailsorter")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = load_settings()
    except ConfigError as error:
        log.error("configuration problem: %s", error)
        return 2
    try:
        secret = load_or_create_secret(settings.data_dir, settings.secret_key)
        vault = TokenVault(load_or_create_token_key(settings.key_path))
    except SecretKeyError as error:
        log.error("key problem: %s", error)
        return 2
    store = AccountStore(Database(settings.database_path), vault)
    app = create_app(settings, store, secret)
    google_state = "ready" if settings.google_problem is None else f"not set up ({settings.google_problem})"
    log.info("Mail Sorter on http://%s:%d for %s, data in %s, Google sign-in %s", settings.bind_host, settings.port,
             settings.public_url, settings.data_dir, google_state)
    serve(app, host=settings.bind_host, port=settings.port, threads=WAITRESS_THREADS,
          max_request_body_size=MAX_REQUEST_BYTES)
    return 0


if __name__ == "__main__":
    sys.exit(main())
