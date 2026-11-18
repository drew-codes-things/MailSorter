import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit

import nh3

LINK_SCHEMES = frozenset({"http", "https", "mailto"})
IMAGE_SCHEMES = frozenset({"http", "https", "data", "cid"})
REMOTE_SCHEMES = frozenset({"http", "https"})
LINK_REL = "noopener noreferrer nofollow"
EMAIL_TAGS = frozenset({
    "a", "abbr", "address", "article", "aside", "b", "bdi", "bdo", "big", "blockquote", "br", "caption", "center",
    "cite", "code", "col", "colgroup", "dd", "del", "details", "dfn", "div", "dl", "dt", "em", "figcaption", "figure",
    "font", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "i", "img", "ins", "kbd", "li", "main",
    "mark", "nav", "ol", "p", "pre", "q", "s", "samp", "section", "small", "span", "strike", "strong", "sub",
    "summary", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "time", "tr", "tt", "u", "ul", "var", "wbr",
})
GENERAL_ATTRIBUTES = frozenset({
    "align", "bgcolor", "border", "cellpadding", "cellspacing", "class", "color", "colspan", "dir", "height", "lang",
    "rowspan", "style", "title", "valign", "width",
})
ATTRIBUTES = {
    "*": GENERAL_ATTRIBUTES,
    "a": {"href", "name"},
    "img": {"src", "alt"},
    "font": {"face", "size"},
    "ol": {"start", "type"},
    "ul": {"type"},
    "td": {"nowrap"},
    "th": {"nowrap", "scope"},
    "time": {"datetime"},
}
STYLE_PROPERTIES = frozenset({
    "background", "background-color", "background-image", "background-position", "background-repeat",
    "background-size", "border", "border-bottom", "border-bottom-color", "border-bottom-style", "border-bottom-width",
    "border-collapse", "border-color", "border-left", "border-left-color", "border-left-style", "border-left-width",
    "border-radius", "border-right", "border-right-color", "border-right-style", "border-right-width",
    "border-spacing", "border-style", "border-top", "border-top-color", "border-top-style", "border-top-width",
    "border-width", "color", "direction", "display", "float", "font", "font-family", "font-size", "font-style",
    "font-variant", "font-weight", "height", "letter-spacing", "line-height", "list-style", "list-style-type",
    "margin", "margin-bottom", "margin-left", "margin-right", "margin-top", "max-height", "max-width", "min-height",
    "min-width", "overflow-wrap", "padding", "padding-bottom", "padding-left", "padding-right", "padding-top",
    "table-layout", "text-align", "text-decoration", "text-indent", "text-transform", "vertical-align",
    "white-space", "width", "word-break", "word-spacing", "word-wrap",
})
DROPPED_WITH_CONTENT = frozenset({"script", "style", "title", "template", "iframe", "object", "embed", "noscript", "svg",
                                  "math", "select", "textarea", "applet", "audio", "video"})
MAX_CLEAN_CHARACTERS = 1024 * 1024
MAX_NESTING_DEPTH = 200
TAG_PATTERN = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9-]*)[^<>]*?(/?)>")
UNNESTED_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr",
    "p", "li", "dt", "dd", "tr", "td", "th", "tbody", "thead", "tfoot", "colgroup", "option", "optgroup", "rb", "rt",
    "rp", "rtc",
})
LINK_PATTERN = re.compile(r"https?://(?:(?!&quot;|&#x27;|&lt;|&gt;)[^\s<>\"'])+")
DOCUMENT_STYLE = (
    "html{background:#ffffff}"
    "body{margin:0;padding:16px;color:#1f1f23;background:#ffffff;font:15px/1.5 system-ui,-apple-system,'Segoe UI',"
    "sans-serif;overflow-wrap:anywhere;word-break:normal}"
    "img{max-width:100%;height:auto}table{max-width:100%}"
    "pre.plain{white-space:pre-wrap;font:inherit;margin:0}"
    "a{color:#b3261e}"
)


@dataclass(frozen=True)
class CleanEmail:
    html: str
    remote_images: int


def _scheme(value: str) -> str:
    try:
        return urlsplit(value.strip()).scheme.lower()
    except ValueError:
        return ""


def _keep_attribute(tag: str, attribute: str, value: str):
    if tag == "a" and attribute == "href":
        return value if _scheme(value) in LINK_SCHEMES else None
    if tag == "img" and attribute == "src":
        scheme = _scheme(value)
        if scheme == "data" and not value.strip().lower().startswith("data:image/"):
            return None
        return value if scheme in IMAGE_SCHEMES else None
    return value


class _ImageCounter(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.remote = 0

    def handle_starttag(self, tag, attrs):
        if tag == "img" and _scheme(dict(attrs).get("src") or "") in REMOTE_SCHEMES:
            self.remote += 1


def _count_remote_images(cleaned: str) -> int:
    counter = _ImageCounter()
    counter.feed(cleaned)
    counter.close()
    return counter.remote


def nesting_depth(raw: str) -> int:
    depth = deepest = 0
    for match in TAG_PATTERN.finditer(raw):
        closing, name, self_closing = match.groups()
        if self_closing or name.lower() in UNNESTED_TAGS:
            continue
        depth = max(depth - 1, 0) if closing else depth + 1
        deepest = max(deepest, depth)
    return deepest


def is_too_complex(raw: str) -> bool:
    return len(raw) > MAX_CLEAN_CHARACTERS or nesting_depth(raw) > MAX_NESTING_DEPTH


def clean_email_html(raw: str) -> CleanEmail:
    cleaned = nh3.clean(
        raw or "",
        tags=set(EMAIL_TAGS),
        clean_content_tags=set(DROPPED_WITH_CONTENT),
        attributes={tag: set(names) for tag, names in ATTRIBUTES.items()},
        attribute_filter=_keep_attribute,
        url_schemes=set(LINK_SCHEMES | IMAGE_SCHEMES),
        link_rel=LINK_REL,
        set_tag_attribute_values={"a": {"target": "_blank"}},
        filter_style_properties=set(STYLE_PROPERTIES),
        strip_comments=True,
    )
    return CleanEmail(html=cleaned, remote_images=_count_remote_images(cleaned))


def _linked(escaped_line: str) -> str:
    def link(match: re.Match) -> str:
        address = match.group(0)
        return f'<a href="{address}" target="_blank" rel="{LINK_REL}">{address}</a>'
    return LINK_PATTERN.sub(link, escaped_line)


def plain_text_html(text: str) -> str:
    return f'<pre class="plain">{_linked(html.escape(text or "", quote=True))}</pre>'


def as_document(body_html: str) -> str:
    return ("<!doctype html><html><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<style>{DOCUMENT_STYLE}</style></head><body>{body_html}</body></html>")
