import base64

import pytest

from mailsorter.mime import NO_SUBJECT, attachments, body_of, summary


def encoded(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def part(kind: str, raw: bytes, charset: str = "utf-8", **extra) -> dict:
    return {"mimeType": kind, "filename": "", "headers": [{"name": "Content-Type", "value": f"{kind}; charset={charset}"}],
            "body": {"size": len(raw), "data": encoded(raw)}, **extra}


def test_html_is_preferred_and_plain_text_is_kept_too():
    payload = {"mimeType": "multipart/alternative", "parts": [part("text/plain", b"plain"), part("text/html", b"<p>rich</p>")]}
    body = body_of(payload, 1000)
    assert (body.html, body.text) == ("<p>rich</p>", "plain")


def test_the_part_charset_is_used_and_bad_charsets_fall_back():
    latin = part("text/plain", "café".encode("latin-1"), charset="iso-8859-1")
    assert body_of(latin, 1000).text == "café"
    odd = part("text/plain", "café".encode(), charset="no-such-charset")
    assert body_of(odd, 1000).text == "café"


def test_a_text_part_that_is_an_attachment_is_not_the_body():
    attached = part("text/plain", b"secret notes", filename="notes.txt")
    payload = {"mimeType": "multipart/mixed", "parts": [part("text/html", b"<p>hi</p>"), attached]}
    body = body_of(payload, 1000)
    assert (body.html, body.text) == ("<p>hi</p>", None)
    assert attachments(payload) == [{"name": "notes.txt", "type": "text/plain", "size": 12}]


def test_a_body_bigger_than_the_cap_is_reported_not_decoded():
    body = body_of(part("text/html", b"x" * 50), 10)
    assert body.too_large and body.html is None


def test_broken_base64_and_deep_nesting_do_not_break_the_reader():
    broken = {"mimeType": "text/plain", "body": {"data": "@@@"}, "headers": []}
    assert body_of(broken, 1000).text is None
    deep = part("text/plain", b"deep")
    for _ in range(200):
        deep = {"mimeType": "multipart/mixed", "parts": [deep]}
    assert body_of(deep, 1000).text is None


def test_summary_decodes_headers_and_strips_control_characters():
    message = {"id": "abc", "internalDate": "1763000000000", "snippet": "Tom &amp; Jerry&#39;s",
               "payload": {"headers": [{"name": "From", "value": "=?utf-8?b?w4lsw6lhIDxlQGV4YW1wbGUuY29tPg==?="},
                                       {"name": "Subject", "value": "Hello\u0007 there\r\n"}]}}
    found = summary(message)
    assert found["from"] == {"name": "Éléa", "address": "e@example.com"}
    assert found["subject"] == "Hello there"
    assert found["snippet"] == "Tom & Jerry's"
    assert found["date"] == 1763000000000


def test_a_message_with_nothing_still_has_a_subject():
    assert summary({"id": "x"})["subject"] == NO_SUBJECT


HOSTILE_CHARSETS = ["hex", "base64", "rot13", "rot_13", "zip", "zlib", "bz2", "uu", "quopri", "idna", "punycode",
                    "undefined", "unicode_escape", "raw_unicode_escape", "utf-7", "utf-16", "utf-32", "mbcs", "oem",
                    "string_escape", "unicode_internal", "x-user-defined", "utf-8-sig", "cp65001", "hz", "iso2022_jp",
                    "", "\"", "a\nb"]
HOSTILE_BODIES = [b"<p>hello</p>", b"xn--a", b"\xff\xfe\x00", b"abc-", b"+AGE", b"\\ud800", b"+2AA-"]
HOSTILE_HEADERS = ["=?utf-8?b?a?=", "=?utf-8?b?!!!!?=", "=?utf-8?q?=ZZ?=", "=?bogus?q?abc?=", "=?utf-8?x?abc?=",
                   "=?utf-8?b??=", "=?hex?q?abc?=", "=?undefined?q?abc?=", "=?idna?q?xn--a?=", "=?utf-16?b?YQ?=",
                   "=?utf-8?b?AAAAA?=", "\x00\x01", "a" * 100000, "<" * 5000, "(" * 5000, "\"" * 5001,
                   "=?utf-8?q?" + "=" * 50 + "?=", "=?unicode_escape?q?=5Cud800?=", "=?rot13?q?abc?=",
                   "=?punycode?q?abc-?=", "=?utf-7?q?+2AA-?=", "a@b, " * 5000, "=?utf-8?b?4pi6?= <x@y>", "\ud800",
                   "=?utf-8?Q?a?==?utf-8?B?a?="]


def poisoned(subject="hi", sender="A <a@b.example>", content_type="text/html; charset=utf-8", body=b"<p>hello</p>",
             to="me@example.com", filename=""):
    parts = [{"mimeType": content_type.split(";")[0].strip(), "filename": "",
              "headers": [{"name": "Content-Type", "value": content_type}],
              "body": {"size": len(body), "data": encoded(body)}}]
    if filename:
        parts.append({"mimeType": "application/pdf", "filename": filename, "headers": [], "body": {"size": 3}})
    return {"id": "abc", "labelIds": ["INBOX"], "snippet": "s", "internalDate": "1763000000000",
            "payload": {"mimeType": "multipart/mixed", "parts": parts,
                        "headers": [{"name": "From", "value": sender}, {"name": "To", "value": to},
                                    {"name": "Subject", "value": subject}]}}


def has_surrogate(value) -> bool:
    return any(0xD800 <= ord(character) <= 0xDFFF for character in str(value))


def read_whole(message: dict) -> tuple[dict, object]:
    found = summary(message)
    body = body_of(message["payload"], 2 * 1024 * 1024)
    assert not has_surrogate(found) and not has_surrogate(body.html) and not has_surrogate(body.text)
    return found, body


@pytest.mark.parametrize("charset", HOSTILE_CHARSETS)
@pytest.mark.parametrize("raw", HOSTILE_BODIES)
def test_any_charset_decodes_to_text_without_surrogates(charset, raw):
    read_whole(poisoned(content_type=f"text/html; charset={charset}", body=raw))


@pytest.mark.parametrize("field", ["subject", "sender", "to", "filename"])
@pytest.mark.parametrize("value", HOSTILE_HEADERS)
def test_any_header_value_decodes_without_an_error(field, value):
    read_whole(poisoned(**{field: value}))


@pytest.mark.parametrize("content_type", ["text/html; charset*=utf-8''%ff%fe", "text/html; charset*=bogus'en'%41",
                                          "text/html; " + "a=b; " * 20000])
def test_odd_content_type_parameters_fall_back_to_utf8(content_type):
    _, body = read_whole(poisoned(content_type=content_type))
    assert body.html == "<p>hello</p>"


@pytest.mark.parametrize("charset", ["hex", "rot13", "base64", "zlib", "bz2", "uu", "quopri", "idna", "undefined",
                                     "unicode_escape", "raw_unicode_escape", "utf-7"])
def test_non_text_codecs_are_never_used_to_decode(charset):
    _, body = read_whole(poisoned(content_type=f"text/html; charset={charset}", body=b"<p>a\\x41 +AGE-</p>"))
    assert body.html == "<p>a\\x41 +AGE-</p>"
