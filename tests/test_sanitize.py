import pytest

from mailsorter.sanitize import as_document, clean_email_html, plain_text_html


@pytest.mark.parametrize("dangerous", [
    "<script>alert(1)</script>", "<img src=x onerror=alert(1)>", "<a href=\"javascript:alert(1)\">x</a>",
    "<iframe src=\"https://evil.example\"></iframe>", "<form action=\"https://evil.example\"><input name=p></form>",
    "<object data=x></object>", "<svg onload=alert(1)></svg>", "<meta http-equiv=refresh content=\"0;url=https://evil.example\">",
    "<base href=\"https://evil.example/\">", "<p onclick=\"alert(1)\">x</p>", "<a href=\"data:text/html,<script>alert(1)</script>\">x</a>",
])
def test_scripts_handlers_frames_forms_and_bad_links_are_removed(dangerous):
    cleaned = clean_email_html(dangerous).html.lower()
    for word in ("<script", "onerror", "onclick", "onload", "javascript:", "<iframe", "<form", "<object", "<svg", "<meta",
                 "<base", "data:text"):
        assert word not in cleaned


def test_layout_survives_and_positioning_does_not():
    cleaned = clean_email_html('<table width="600" bgcolor="#fff"><tr><td style="color:red;position:fixed;top:0">Hi</td></tr></table>').html
    assert 'width="600"' in cleaned and 'bgcolor="#fff"' in cleaned
    assert "color:red" in cleaned.replace(" ", "")
    assert "position" not in cleaned


def test_links_open_in_a_new_tab_without_a_referrer():
    cleaned = clean_email_html('<a href="https://shop.example/order">order</a>').html
    assert 'target="_blank"' in cleaned and "noreferrer" in cleaned and "noopener" in cleaned


def test_remote_images_are_counted_and_only_image_data_urls_stay():
    cleaned = clean_email_html('<img src="https://t.example/pixel.gif"><img src="http://t.example/a.png">'
                               '<img src="data:image/png;base64,AAAA"><img src="data:text/html,x">')
    assert cleaned.remote_images == 2
    assert "data:image/png" in cleaned.html and "data:text" not in cleaned.html


def test_plain_text_is_escaped_and_links_made_clickable():
    shown = plain_text_html('<b>hi</b> see https://example.com/a?b=1&c=2 "quoted"')
    assert "<b>" not in shown and "&lt;b&gt;" in shown
    assert '<a href="https://example.com/a?b=1&amp;c=2"' in shown


def test_the_document_has_a_white_page_and_the_content():
    document = as_document("<p>x</p>")
    assert document.startswith("<!doctype html>") and "<p>x</p>" in document and "#ffffff" in document


@pytest.mark.parametrize("styled", [
    "<style>@import url(https://t.example/a.css); body{background:url(https://t.example/open.gif)}</style><p>x</p>",
    "<style>.x{position:fixed;top:0;left:0;width:100%;height:100%}</style><p>x</p>",
    "<style>@font-face{font-family:f;src:url(https://t.example/f.woff)}</style><p>x</p>",
    "<style><!--</style><p>x</p>",
])
def test_style_blocks_are_dropped_with_their_content(styled):
    assert clean_email_html(styled).html == "<p>x</p>"
