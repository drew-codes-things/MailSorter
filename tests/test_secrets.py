import stat

import pytest

from mailsorter.secrets_store import (SecretKeyError, TokenUnreadable, TokenVault, csrf_matches, csrf_token,
                                      load_or_create_secret, load_or_create_token_key)


def test_a_token_key_is_made_once_with_mode_600(tmp_path):
    path = tmp_path / "keys" / "token.key"
    key = load_or_create_token_key(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_or_create_token_key(path) == key


def test_a_damaged_token_key_stops_the_app(tmp_path):
    path = tmp_path / "token.key"
    path.write_text("short")
    with pytest.raises(SecretKeyError):
        load_or_create_token_key(path)


def test_tokens_are_sealed_and_only_open_with_the_same_key(tmp_path):
    vault = TokenVault(load_or_create_token_key(tmp_path / "a.key"))
    sealed = vault.seal("1//refresh-token")
    assert b"refresh-token" not in sealed
    assert vault.open(sealed) == "1//refresh-token"
    other = TokenVault(load_or_create_token_key(tmp_path / "b.key"))
    with pytest.raises(TokenUnreadable):
        other.open(sealed)


def test_a_short_secret_key_is_refused(tmp_path):
    with pytest.raises(SecretKeyError):
        load_or_create_secret(tmp_path, "short")
    made = load_or_create_secret(tmp_path, "")
    assert len(made) >= 32 and load_or_create_secret(tmp_path, "") == made


def test_csrf_tokens_belong_to_one_session():
    secret = b"x" * 32
    token = csrf_token(secret, "session-a")
    assert csrf_matches(secret, "session-a", token)
    assert not csrf_matches(secret, "session-b", token)
    assert not csrf_matches(secret, "session-a", None)
