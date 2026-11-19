"""Classify cms-hpt.txt files (CMS hospital price transparency)."""
import re

from noroshi.sectxt_parse import decode_body, precheck

_HTTP_URL = re.compile(r"^https?://\S+", re.IGNORECASE)


def classify_cms_hpt(
    status: int | None,
    final_url: str | None,
    content_type: str | None,
    body: bytes | None,
    blocked: bool,
    error: str | None,
    forbidden: bool = False,
) -> str:
    early = precheck(status, content_type, body, blocked, error, forbidden)
    if early:
        return early
    has_mrf = has_location = False
    for line in decode_body(body or b"").splitlines():
        name, sep, value = line.partition(":")
        if not sep:
            continue
        name, value = name.strip().lower(), value.strip()
        if name == "mrf-url" and _HTTP_URL.match(value):
            has_mrf = True
        elif name == "location-name" and value:
            has_location = True
    return "present_valid" if has_mrf and has_location else "present_invalid"
