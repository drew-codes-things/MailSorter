import mailsorter.__main__ as entry
from mailsorter.web import MAX_REQUEST_BYTES

NAMES = ("PUBLIC_URL", "BIND_HOST", "PORT", "DATA_DIR", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "TOKEN_KEY_FILE",
         "ALLOWED_ACCOUNTS", "SECRET_KEY")


def clean(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))


def test_the_app_starts_without_a_google_client(monkeypatch, tmp_path):
    clean(monkeypatch, tmp_path)
    served = {}
    monkeypatch.setattr(entry, "serve", lambda app, **options: served.update(options))
    assert entry.main() == 0
    assert (served["host"], served["port"]) == ("127.0.0.1", 8101)
    assert served["max_request_body_size"] == MAX_REQUEST_BYTES
    assert (tmp_path / "data" / "token.key").exists()


def test_a_configuration_mistake_exits_with_status_2(monkeypatch, tmp_path):
    clean(monkeypatch, tmp_path)
    monkeypatch.setenv("PUBLIC_URL", "http://mail.example.com")
    assert entry.main() == 2


def test_an_unreadable_token_key_exits_with_status_2(monkeypatch, tmp_path):
    clean(monkeypatch, tmp_path)
    key = tmp_path / "token.key"
    key.write_text("not a key")
    monkeypatch.setenv("TOKEN_KEY_FILE", str(key))
    assert entry.main() == 2
