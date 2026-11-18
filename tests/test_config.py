import pytest

from mailsorter.config import BAD_CLIENT_ID_TEXT, BAD_CLIENT_SECRET_TEXT, NOT_SET_UP_TEXT, ConfigError, load_settings

NAMES = ("PUBLIC_URL", "BIND_HOST", "PORT", "DATA_DIR", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "TOKEN_KEY_FILE",
         "ALLOWED_ACCOUNTS", "SECRET_KEY", "TRUSTED_PROXIES")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)


def test_defaults_listen_on_loopback_port_8101_and_need_google_setup():
    settings = load_settings()
    assert (settings.bind_host, settings.port, settings.public_url) == ("127.0.0.1", 8101, "http://127.0.0.1:8101")
    assert settings.google_problem == NOT_SET_UP_TEXT
    assert settings.redirect_uri == "http://127.0.0.1:8101/auth/callback"
    assert not settings.secure_cookies


def test_a_real_looking_client_is_ready(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "123-abc.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "GOCSPX-abcdefgh")
    monkeypatch.setenv("PUBLIC_URL", "https://mail.example.com/")
    settings = load_settings()
    assert settings.google_problem is None
    assert settings.redirect_uri == "https://mail.example.com/auth/callback"
    assert settings.secure_cookies


@pytest.mark.parametrize("client_id, secret, problem", [
    ("not-a-client-id", "GOCSPX-abcdefgh", BAD_CLIENT_ID_TEXT),
    ("123-abc.apps.googleusercontent.com", "has a space", BAD_CLIENT_SECRET_TEXT),
    ("", "GOCSPX-abcdefgh", NOT_SET_UP_TEXT),
])
def test_a_wrong_client_gives_a_clear_problem_instead_of_stopping(monkeypatch, client_id, secret, problem):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", client_id)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", secret)
    assert load_settings().google_problem == problem


@pytest.mark.parametrize("url", ["http://mail.example.com", "ftp://x.example", "https://x.example/path", "https://", "mail"])
def test_bad_public_urls_stop_the_app(monkeypatch, url):
    monkeypatch.setenv("PUBLIC_URL", url)
    with pytest.raises(ConfigError):
        load_settings()


def test_allowed_accounts_are_checked_and_lower_cased(monkeypatch):
    monkeypatch.setenv("ALLOWED_ACCOUNTS", "Drew@Example.com, friend@example.com")
    assert load_settings().allowed_accounts == ("drew@example.com", "friend@example.com")
    monkeypatch.setenv("ALLOWED_ACCOUNTS", "not-an-address")
    with pytest.raises(ConfigError):
        load_settings()


def test_the_env_file_is_read(tmp_path):
    (tmp_path / ".env").write_text("PORT=9001\n# a note\nGOOGLE_CLIENT_ID='1-x.apps.googleusercontent.com'\n")
    settings = load_settings()
    assert settings.port == 9001
    assert settings.google_client_id == "1-x.apps.googleusercontent.com"


def test_the_token_key_file_defaults_to_the_data_folder(monkeypatch, tmp_path):
    assert load_settings().key_path == tmp_path / "data" / "token.key"
    monkeypatch.setenv("TOKEN_KEY_FILE", "/etc/mail-sorter/token.key")
    assert str(load_settings().key_path) == "/etc/mail-sorter/token.key"


def test_no_proxy_is_trusted_by_default_and_listed_ones_are_kept(monkeypatch):
    assert load_settings().trusted_proxies == ()
    monkeypatch.setenv("TRUSTED_PROXIES", "127.0.0.1, ::1,fd00::/8, 203.0.113.5")
    assert load_settings().trusted_proxies == ("127.0.0.1", "::1", "fd00::/8", "203.0.113.5")


@pytest.mark.parametrize("entry, problem", [
    ("caddy", "not an address or network"),
    ("192.0.2.16/24", "host bits set"),
    ("0.0.0.0/0", "reaches public addresses"),
    ("::/0", "reaches public addresses"),
])
def test_a_trusted_proxy_entry_that_trusts_too_much_stops_the_app(monkeypatch, entry, problem):
    monkeypatch.setenv("TRUSTED_PROXIES", entry)
    with pytest.raises(ConfigError, match=problem):
        load_settings()
