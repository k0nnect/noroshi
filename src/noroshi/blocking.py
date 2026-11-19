"""Detect bot-blocking responses and HTML bodies."""
import re
from collections.abc import Mapping
from html.parser import HTMLParser

BLOCK_STATUSES = frozenset({401, 403, 406, 429})
BLOCK_HEADERS = ("cf-mitigated", "x-amzn-waf-action")  # Cloudflare and AWS WAF mark a challenge; any value, any status
_SCAN_BYTES = 32_768
_CHALLENGE_TITLE = re.compile(rb"<title[^>]*>\s*just a moment\.\.\.", re.I)
_MARKERS = (
    b"attention required! | cloudflare",
    b"errors.edgesuite.net",
    b"errors&#46;edgesuite&#46;net",  # the same Akamai address as Akamai's block pages write it, with HTML entities
    b"incapsula incident id",
    b"px-captcha",
    b"pardon our interruption",
    b"spamfirewall",  # CleanTalk's "SpamFireWall is checking your browser" page (fix round 3, K5)
)
# Incapsula's script challenge: a refusal only on a 200 page with fewer than MIN_VISIBLE_CHARS visible characters,
# because Incapsula also appends this script to the ordinary pages it serves
_INCAPSULA_SCRIPT = b"_incapsula_resource"
MIN_VISIBLE_CHARS = 200


class VisibleText(HTMLParser):
    """The visible text of a page (outside title, script and style) and its title."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip:
            return
        (self.title if self._in_title else self.parts).append(data)


def visible_text(html: bytes) -> str:
    """Text outside title, script and style, whitespace collapsed (read as UTF-8)."""
    parser = VisibleText()
    parser.feed(html.decode("utf-8", errors="replace"))
    parser.close()
    return " ".join(" ".join(parser.parts).split())


def block_page(status: int | None, body: bytes) -> bool:
    """The body part of `detect_block`: a `Just a moment...` title or a fixed marker in the first 32 KiB, or
    Incapsula's script on a 200 page with fewer than 200 visible characters."""
    head = body[:_SCAN_BYTES].lower()
    if _CHALLENGE_TITLE.search(head) or any(m in head for m in _MARKERS):
        return True
    return status == 200 and _INCAPSULA_SCRIPT in head and len(visible_text(body)) < MIN_VISIBLE_CHARS


def detect_block(status: int | None, headers: Mapping[str, str], body: bytes) -> bool:
    if status in BLOCK_STATUSES:
        return True
    if any(k.lower() in BLOCK_HEADERS for k in headers):
        return True
    return block_page(status, body)


def looks_like_html(content_type: str | None, body: bytes) -> bool:
    if content_type and "html" in content_type.lower():
        return True
    head = body[:512].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html", b"<head", b"<body"))
