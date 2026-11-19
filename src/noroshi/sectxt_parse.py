"""Parse and classify security.txt files (RFC 9116)."""
import codecs
import re
from collections.abc import Callable
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from datetime import datetime

from noroshi.blocking import block_page, looks_like_html
from noroshi.store import NOT_OUTCOMES, FetchRecord, homepage_record, is_answer

VALID_SCHEMES = ("mailto", "https", "tel")
_PGP_BEGIN = "-----BEGIN PGP SIGNED MESSAGE-----"
_ARMOR_HEADER = re.compile(r"^(Hash|Charset|Comment|Version|MessageID|SaltedHash):")
_PGP_SIG = "-----BEGIN PGP SIGNATURE-----"
_MAILTO_ADDR = re.compile(r"^[^@\s]+@[^@\s]*\.[^@\s]+$")
_BARE_EMAIL = re.compile(r"^[^@\s:/]+@[^@\s/]+\.[^@\s/]+$")
_EXPIRES = re.compile(  # a date-time is valid only with a UTC offset: Z, +hh:mm or -hh:mm
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$",
    re.IGNORECASE | re.ASCII,
)


@dataclass
class SecTxtResult:
    fields: dict[str, list[str]] = field(default_factory=dict)
    contacts: list[str] = field(default_factory=list)
    contact_types: frozenset[str] = frozenset()
    has_valid_contact: bool = False
    lenient_contact: bool = False
    expires_status: str = "missing"
    signed: bool = False
    errors: list[str] = field(default_factory=list)
    malformed_contacts: list[str] = field(default_factory=list)


def decode_body(body: bytes) -> str:
    """Decode as UTF-8, dropping a byte-order mark and replacing bad bytes."""
    if body.startswith(codecs.BOM_UTF8):
        body = body[len(codecs.BOM_UTF8):]
    return body.decode("utf-8", errors="replace")


def _unwrap_pgp(lines: list[str]) -> tuple[list[str], bool]:
    """Return the content lines inside a cleartext-signed wrapper, if any."""
    first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first is None or lines[first].strip() != _PGP_BEGIN:
        return lines, False
    i = first + 1
    while i < len(lines) and _ARMOR_HEADER.match(lines[i]):
        i += 1
    if i < len(lines) and not lines[i].strip():  # blank line ends the headers
        i += 1
    content = []
    for ln in lines[i:]:
        if ln.strip() == _PGP_SIG:
            break
        content.append(ln[2:] if ln.startswith("- ") else ln)
    return content, True


def _parse_expires(value: str, now: datetime) -> str:
    if not _EXPIRES.match(value):
        return "invalid"
    try:
        when = datetime.fromisoformat(value.upper().replace(" ", "T"))
    except ValueError:
        return "invalid"
    return "expired" if when < now else "valid"


def _well_formed(scheme: str, rest: str) -> bool:
    if scheme == "mailto":
        return bool(_MAILTO_ADDR.match(rest.split("?", 1)[0]))
    if scheme == "https":
        if not rest.startswith("//") or any(c.isspace() for c in rest):
            return False
        try:
            host = urlsplit("https:" + rest).hostname
        except ValueError:
            return False
        return bool(host) and "." in host and all(host.split("."))
    return any(c.isdigit() for c in rest)  # tel


def _contact_kind(value: str) -> tuple[str | None, bool, bool]:
    """Return (valid scheme or None, lenient, malformed)."""
    scheme, sep, rest = value.partition(":")
    scheme = scheme.lower()
    if sep and scheme in VALID_SCHEMES:
        if _well_formed(scheme, rest):
            return scheme, False, False
        return None, False, True
    if sep and scheme == "http" and rest.strip():
        return None, True, False
    if _BARE_EMAIL.match(value):
        return None, True, False
    return None, False, False


def parse_security_txt(body: bytes, now: datetime) -> SecTxtResult:
    lines = decode_body(body).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines, signed = _unwrap_pgp(lines)
    result = SecTxtResult(signed=signed)
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition(":")
        name = name.strip().lower()
        if not sep or not name:
            result.errors.append(f"not a field line: {line[:80]}")
            continue
        result.fields.setdefault(name, []).append(value.strip())

    result.contacts = list(result.fields.get("contact", []))
    types = set()
    for value in result.contacts:
        scheme, lenient, malformed = _contact_kind(value)
        if scheme:
            types.add(scheme)
        if malformed:
            result.malformed_contacts.append(value)
        result.lenient_contact = result.lenient_contact or lenient
    result.contact_types = frozenset(types)
    result.has_valid_contact = bool(types)

    expires = result.fields.get("expires", [])
    if not expires:
        result.expires_status = "missing"
    elif len(expires) > 1:
        result.expires_status = "multiple"
    else:
        result.expires_status = _parse_expires(expires[0], now)
    return result


FORBIDDEN = "forbidden"
FORBIDDEN_STATUSES = (401, 403)


def is_forbidden(records: list[FetchRecord], purpose: str, body_of: Callable[[FetchRecord], bytes | None]) -> bool:
    """The `forbidden` rule for a security.txt location or cms-hpt.txt (`purpose`), from the records of one listed
    domain in one pass and `body_of(record)`, the stored body of a record: the file's identified attempt and that
    attempt's browser retry both ended with status 401 or 403, both answers are plain (neither body is a block page,
    `blocking.block_page`: a challenge title or a fixed marker; and neither body contains `<script`, in any case), and
    the domain's homepage was an answering 200 in the same pass. Nobody outside can read such a file: it counts as no
    file for every outcome, is not unmeasured, and is not retried in pass 2. When the retry could not be sent, or
    either answer is not plain, the file stays unmeasured. The classifiers take the answer as their `forbidden`
    argument; crawl.needs_retry reads it too."""
    done = [r for r in records if r.error not in NOT_OUTCOMES]
    home = homepage_record(records)
    if home is None or home.status != 200 or not is_answer(home.status, home.blocked, home.error):
        return False
    tries = [r for r in done if r.purpose == purpose]
    if len(tries) < 2:
        return False
    first, retry = tries[-2:]
    return (first.ua_kind == "identified" and retry.ua_kind == "browser" and first.url == retry.url
            and first.status in FORBIDDEN_STATUSES and retry.status in FORBIDDEN_STATUSES
            and all(_plain(r.status, body_of(r) or b"") for r in (first, retry)))


def _plain(status: int | None, body: bytes) -> bool:
    """A denial with nothing a challenge needs: no block page, and no script (a page that runs a script may let a
    browser through; fix round 3, K5)."""
    return not block_page(status, body) and b"<script" not in body.lower()


def precheck(
    status: int | None,
    content_type: str | None,
    body: bytes | None,
    blocked: bool,
    error: str | None,
    forbidden: bool = False,
) -> str | None:
    """Rules 1 to 3, shared by every file classifier. None means a readable text 200. A record that is not an answer
    (`store.is_answer`) is unmeasured, or `forbidden` when `is_forbidden` holds for its file; an answer other than 200
    is a 4xx that is not a refusal: the file is absent."""
    if not is_answer(status, blocked, error):
        return FORBIDDEN if forbidden else "unmeasured"
    if status != 200:
        return "absent"
    if looks_like_html(content_type, body or b""):
        return "html_error"
    return None


def classify_security_txt(
    status: int | None,
    final_url: str | None,
    content_type: str | None,
    body: bytes | None,
    blocked: bool,
    error: str | None,
    now: datetime,
    forbidden: bool = False,
) -> str:
    early = precheck(status, content_type, body, blocked, error, forbidden)
    if early:
        return early
    result = parse_security_txt(body or b"", now)
    is_https = bool(final_url) and final_url.lower().startswith("https://")
    if not result.has_valid_contact or not is_https:
        return "invalid"
    if result.expires_status != "valid":
        return "valid_with_problems"
    return "valid"


def library_agrees(body: bytes, result: SecTxtResult) -> bool | None:
    """Cross-check has_valid_contact against the sectxt library.

    Uses sectxt.Parser(content), the offline content parser. SecurityTXT is the
    class that fetches over the network; Parser never does.
    """
    try:
        from sectxt import Parser

        lib = Parser(body)
        lib_valid = bool(
            lib.valid_contact_emails or lib.valid_contact_urls or lib.contact_tels
        )
    except Exception:
        return None
    return lib_valid == result.has_valid_contact
