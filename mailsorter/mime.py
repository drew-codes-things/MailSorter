import base64
import binascii
import codecs
import html
import re
from dataclasses import dataclass
from email.errors import HeaderParseError
from email.header import decode_header
from email.message import Message
from email.utils import getaddresses

MAX_PART_DEPTH = 20
MAX_PARTS = 500
MAX_HEADER_LENGTH = 1000
MAX_ATTACHMENTS = 50
MAX_SNIPPET_LENGTH = 400
DEFAULT_CHARSET = "utf-8"
NO_SUBJECT = "(no subject)"
REFUSED_CODECS = frozenset({"hex", "rot-13", "base64", "zlib", "bz2", "uu", "quopri", "idna", "punycode", "undefined",
                            "unicode-escape", "raw-unicode-escape", "utf-7"})
DECODING_ERRORS = (LookupError, UnicodeError, ValueError, HeaderParseError, binascii.Error, TypeError)
SURROGATES = re.compile("[\ud800-\udfff]")
REPLACEMENT_CHARACTER = "\ufffd"
UNENCODED_WORD_CODEC = "raw-unicode-escape"


@dataclass(frozen=True)
class Body:
    html: str | None
    text: str | None
    too_large: bool = False


def without_surrogates(text: str) -> str:
    return SURROGATES.sub(REPLACEMENT_CHARACTER, text)


def text_codec(charset: str | None) -> str:
    try:
        info = codecs.lookup(charset or DEFAULT_CHARSET)
    except DECODING_ERRORS:
        return DEFAULT_CHARSET
    if info.name in REFUSED_CODECS or not getattr(info, "_is_text_encoding", True):
        return DEFAULT_CHARSET
    return info.name


def decode_text(raw: bytes, charset: str | None) -> str:
    try:
        decoded = raw.decode(text_codec(charset), errors="replace")
    except DECODING_ERRORS:
        decoded = raw.decode(DEFAULT_CHARSET, errors="replace")
    return without_surrogates(decoded)


def _decoded_chunk(chunk: bytes | str, charset: str | None) -> str:
    if isinstance(chunk, str):
        return chunk
    if charset is None:
        try:
            return chunk.decode(UNENCODED_WORD_CODEC, errors="replace")
        except DECODING_ERRORS:
            return chunk.decode(DEFAULT_CHARSET, errors="replace")
    return decode_text(chunk, charset)


def _decoded_words(value: str) -> str:
    try:
        chunks = decode_header(value)
    except DECODING_ERRORS:
        return value
    return "".join(_decoded_chunk(chunk, charset) for chunk, charset in chunks)


def _readable(value: str) -> str:
    decoded = without_surrogates(_decoded_words(without_surrogates(value)))
    cleaned = "".join(character for character in decoded if character.isprintable() or character == " ")
    return cleaned.strip()[:MAX_HEADER_LENGTH]


def _headers(part: dict) -> dict:
    found = {}
    for header in part.get("headers") or []:
        if isinstance(header, dict) and isinstance(header.get("name"), str) and isinstance(header.get("value"), str):
            found.setdefault(header["name"].lower(), header["value"])
    return found


def _walk(part: dict, depth: int = 0):
    stack = [(part, depth)]
    seen = 0
    while stack and seen < MAX_PARTS:
        current, level = stack.pop()
        if not isinstance(current, dict):
            continue
        seen += 1
        yield current
        if level < MAX_PART_DEPTH:
            children = [child for child in current.get("parts") or [] if isinstance(child, dict)]
            stack.extend((child, level + 1) for child in reversed(children))


def _sender(value: str) -> dict:
    readable = _readable(value)
    pairs = getaddresses([readable])
    name, address = pairs[0] if pairs else ("", "")
    return {"name": name.strip() or address or readable, "address": address}


def _charset_of(content_type: str) -> str:
    message = Message()
    try:
        message["Content-Type"] = content_type or "text/plain"
        charset = message.get_content_charset()
    except DECODING_ERRORS:
        return DEFAULT_CHARSET
    return text_codec(charset)


def _decoded_data(part: dict) -> str | None:
    data = (part.get("body") or {}).get("data")
    if not isinstance(data, str):
        return None
    try:
        raw = base64.b64decode(data + "=" * (-len(data) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        return None
    return decode_text(raw, _charset_of(_headers(part).get("content-type", "")))


def _is_attachment(part: dict) -> bool:
    disposition = _headers(part).get("content-disposition", "").lower()
    return bool(part.get("filename")) or disposition.startswith("attachment")


def attachments(payload: dict) -> list[dict]:
    found = []
    for part in _walk(payload):
        name = part.get("filename")
        if not isinstance(name, str) or not name.strip():
            continue
        size = (part.get("body") or {}).get("size")
        found.append({"name": _readable(name), "type": str(part.get("mimeType") or "")[:100],
                      "size": size if isinstance(size, int) and size >= 0 else None})
        if len(found) >= MAX_ATTACHMENTS:
            break
    return found


def body_of(payload: dict, max_bytes: int) -> Body:
    html_body = text_body = None
    too_large = False
    for part in _walk(payload):
        if _is_attachment(part):
            continue
        kind = str(part.get("mimeType") or "").lower()
        if kind not in ("text/html", "text/plain"):
            continue
        size = (part.get("body") or {}).get("size")
        if isinstance(size, int) and size > max_bytes:
            too_large = True
            continue
        content = _decoded_data(part)
        if content is None:
            continue
        if kind == "text/html" and html_body is None:
            html_body = content
        elif kind == "text/plain" and text_body is None:
            text_body = content
    return Body(html=html_body, text=text_body, too_large=too_large and html_body is None and text_body is None)


def summary(message: dict) -> dict:
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    headers = _headers(payload)
    internal_date = message.get("internalDate")
    return {
        "id": message.get("id"),
        "from": _sender(headers.get("from", "")),
        "to": _readable(headers.get("to", "")),
        "subject": _readable(headers.get("subject", "")) or NO_SUBJECT,
        "date": int(internal_date) if isinstance(internal_date, str) and internal_date.isdigit() else None,
        "snippet": _readable(html.unescape(str(message.get("snippet") or "")))[:MAX_SNIPPET_LENGTH],
        "attachments": attachments(payload),
    }


def label_ids(message: dict) -> set[str]:
    return {label for label in message.get("labelIds") or [] if isinstance(label, str)}
