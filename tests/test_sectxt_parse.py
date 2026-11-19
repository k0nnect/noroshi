import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from noroshi.sectxt_parse import classify_security_txt, library_agrees, parse_security_txt

FIX = Path(__file__).parent / "fixtures" / "sectxt"
NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
GOOD = b"Contact: mailto:security@example.org\nExpires: 2027-06-01T00:00:00Z\n"
URL = "https://a.org/.well-known/security.txt"


def cls(body, *, status=200, url=URL, ctype="text/plain", blocked=False, error=None):
    return classify_security_txt(status, url, ctype, body, blocked, error, NOW)


def test_valid():
    assert cls(GOOD) == "valid"


def test_expired_counts_as_problem():
    body = GOOD.replace(b"2027", b"2024")
    assert cls(body) == "valid_with_problems"
    assert parse_security_txt(body, NOW).expires_status == "expired"


def test_missing_expires():
    body = b"Contact: mailto:security@example.org\n"
    assert cls(body) == "valid_with_problems"
    assert parse_security_txt(body, NOW).expires_status == "missing"


def test_two_expires_lines():
    body = GOOD + b"Expires: 2027-07-01T00:00:00Z\n"
    assert cls(body) == "valid_with_problems"
    assert parse_security_txt(body, NOW).expires_status == "multiple"


def test_invalid_expires():
    body = b"Contact: mailto:a@example.org\nExpires: next year\n"
    assert parse_security_txt(body, NOW).expires_status == "invalid"
    assert cls(body) == "valid_with_problems"


def test_expires_without_a_utc_offset_is_invalid():
    """Post-pilot X10: a date-time is valid only with Z or +hh:mm / -hh:mm."""
    for value in (b"2027-06-01T00:00:00", b"2026-11-19T23:59:59", b"2027-06-01 00:00:00", b"2027-06-01T00:00:00.5"):
        body = b"Contact: mailto:a@example.org\nExpires: " + value + b"\n"
        assert parse_security_txt(body, NOW).expires_status == "invalid", value
        assert cls(body) == "valid_with_problems"
    for value in (b"2027-06-01T00:00:00Z", b"2027-06-01T00:00:00.000Z", b"2027-06-01T00:00:00+02:00",
                  b"2027-06-01T00:00:00-05:00"):
        assert parse_security_txt(b"Contact: mailto:a@example.org\nExpires: " + value + b"\n", NOW).expires_status \
            == "valid", value


def test_expires_offset_respected():
    # 2026-11-20T01:00+05:00 is 2026-11-19T20:00Z, before NOW
    r = parse_security_txt(b"Contact: mailto:a@example.org\nExpires: 2026-11-20T01:00:00+05:00\n", NOW)
    assert r.expires_status == "expired"


def test_classify_html_200_is_html_error():
    assert cls(b"<html>Not found</html>", ctype="text/html") == "html_error"
    assert cls(b"<!doctype html><html>") == "html_error"


def test_404_absent_and_blocked_unmeasured():
    assert cls(b"", status=404, url=None, ctype=None) == "absent"
    assert cls(b"", status=403, url=None, ctype=None, blocked=True) == "unmeasured"
    assert cls(None, status=None, url=None, ctype=None, error="timeout") == "unmeasured"
    assert cls(b"", status=503, url=None, ctype=None) == "unmeasured"
    assert cls(GOOD, error="boom") == "unmeasured"
    assert cls(GOOD, blocked=True) == "unmeasured"
    assert cls(b"", status=301, url=None, ctype=None) == "unmeasured"  # a final 3xx is not an answer
    assert cls(b"", status=410, url=None, ctype=None) == "absent"


def test_http_only_is_invalid():
    assert cls(GOOD, url="http://a.org/security.txt") == "invalid"
    assert cls(GOOD, url=None) == "invalid"


def test_no_valid_contact_is_invalid():
    assert cls(b"Expires: 2027-06-01T00:00:00Z\n") == "invalid"


def test_bare_email_is_lenient_not_valid():
    r = parse_security_txt(b"Contact: security@example.org\n", NOW)
    assert not r.has_valid_contact and r.lenient_contact
    assert r.contact_types == frozenset()


def test_http_contact_is_lenient_not_valid():
    r = parse_security_txt(b"Contact: http://example.org/report\n", NOW)
    assert not r.has_valid_contact and r.lenient_contact


@pytest.mark.parametrize("value", ["mailto:foo", "https://", "tel:", "mailto:", "mailto:a@b", "https://localhost/x", "tel:abc", "mailto:a b@example.org"])
def test_malformed_contact_not_valid_and_collected(value):
    r = parse_security_txt(f"Contact: {value}\n".encode(), NOW)
    assert not r.has_valid_contact and not r.lenient_contact
    assert r.malformed_contacts == [value]
    assert r.contact_types == frozenset()


def test_mailto_parameters_ignored():
    r = parse_security_txt(b"Contact: mailto:security@example.org?subject=Report\n", NOW)
    assert r.has_valid_contact and r.malformed_contacts == []


def test_one_malformed_one_good_is_valid():
    r = parse_security_txt(b"Contact: mailto:foo\nContact: https://example.org/report\n", NOW)
    assert r.has_valid_contact and r.malformed_contacts == ["mailto:foo"]
    assert r.contact_types == frozenset({"https"})


def test_https_host_and_tel_digit_valid():
    r = parse_security_txt(b"Contact: https://example.org:8443/x\nContact: tel:+1-555-0100\n", NOW)
    assert r.contact_types == frozenset({"https", "tel"}) and not r.malformed_contacts


@pytest.mark.parametrize("name", ["bom.txt", "crlf.txt", "pgp_signed.txt", "lowercase_fields.txt", "comments_and_blank.txt", "multi_contact.txt"])
def test_format_variants_parse(name):
    r = parse_security_txt((FIX / name).read_bytes(), NOW)
    assert r.has_valid_contact and not r.errors
    assert r.expires_status == "valid"


def test_pgp_signed_flag_and_content():
    r = parse_security_txt((FIX / "pgp_signed.txt").read_bytes(), NOW)
    assert r.signed
    assert "hash" not in r.fields
    assert not parse_security_txt(GOOD, NOW).signed


def test_pgp_dash_escape_removed():
    body = (b"-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\n"
            b"- Contact: mailto:a@example.org\n"
            b"-----BEGIN PGP SIGNATURE-----\n\nabc\n-----END PGP SIGNATURE-----\n")
    r = parse_security_txt(body, NOW)
    assert r.signed and r.has_valid_contact


def test_field_names_case_insensitive():
    r = parse_security_txt(b"CONTACT: mailto:a@example.org\nExPiReS: 2027-06-01T00:00:00Z\n", NOW)
    assert r.has_valid_contact and r.expires_status == "valid"
    assert set(r.fields) == {"contact", "expires"}


def test_unknown_fields_kept_not_errors():
    r = parse_security_txt(GOOD + b"X-Custom: hello\n", NOW)
    assert r.fields["x-custom"] == ["hello"] and not r.errors


def test_line_without_colon_is_error():
    r = parse_security_txt(GOOD + b"this is not a field\n", NOW)
    assert len(r.errors) == 1


def test_contact_types():
    r = parse_security_txt((FIX / "multi_contact.txt").read_bytes(), NOW)
    assert r.contact_types == frozenset({"mailto", "https", "tel"})
    assert len(r.contacts) == 3


def test_contact_types_only_from_valid_contacts():
    r = parse_security_txt(b"Contact: mailto:a@example.org\nContact: http://x.org\n", NOW)
    assert r.contact_types == frozenset({"mailto"})
    assert r.lenient_contact


def test_library_agrees_on_good_file():
    assert library_agrees(GOOD, parse_security_txt(GOOD, NOW)) is True


def test_library_agrees_on_bare_email_and_http():
    for body in (b"Contact: security@example.org\n", b"Contact: http://example.org/r\n"):
        assert library_agrees(body, parse_security_txt(body, NOW)) is True


def test_library_agrees_on_fixtures():
    for p in FIX.glob("*.txt"):
        b = p.read_bytes()
        assert library_agrees(b, parse_security_txt(b, NOW)) is True, p.name


def test_library_makes_no_network_call(monkeypatch):
    import requests

    def boom(*a, **k):
        raise AssertionError("network used")

    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr(requests.Session, "request", boom)
    assert library_agrees(GOOD, parse_security_txt(GOOD, NOW)) is True


def test_empty_and_binary_bodies_do_not_raise():
    for body in (b"", os.urandom(4096)):
        r = parse_security_txt(body, NOW)
        assert not r.has_valid_contact
        assert cls(body) == "invalid"
        library_agrees(body, r)


def test_pgp_missing_blank_line_keeps_content():
    body = (b"-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n"
            b"Contact: mailto:a@example.org\nExpires: 2027-06-01T00:00:00Z\n"
            b"-----BEGIN PGP SIGNATURE-----\n\nabc\n-----END PGP SIGNATURE-----\n")
    r = parse_security_txt(body, NOW)
    assert r.signed and r.has_valid_contact and r.expires_status == "valid"
    assert cls(body) == "valid"


def test_pgp_multiple_armor_headers_skipped():
    body = (b"-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\nCharset: utf-8\nComment: x\n\n"
            b"Contact: mailto:a@example.org\n-----BEGIN PGP SIGNATURE-----\n\nabc\n")
    r = parse_security_txt(body, NOW)
    assert r.has_valid_contact and set(r.fields) == {"contact"}


def test_pgp_no_headers_and_no_signature_block():
    body = b"-----BEGIN PGP SIGNED MESSAGE-----\n\nContact: mailto:a@example.org\n"
    assert parse_security_txt(body, NOW).has_valid_contact


@pytest.mark.parametrize("value", ["https://exa mple.org/x", "https://./x", "https://a..org/x", "https://.org/x", "https://example./x", "https://example.org/a b"])
def test_https_bad_host_or_whitespace_is_malformed(value):
    r = parse_security_txt(f"Contact: {value}\n".encode(), NOW)
    assert not r.has_valid_contact and r.malformed_contacts == [value]


# ---- closing fixes, C2: only a 200 or a 4xx that is not a refusal is an answer

@pytest.mark.parametrize("status", [202, 204, 206, 203, 301, 302, 304])
def test_a_final_status_that_is_not_200_or_a_4xx_is_unmeasured_for_both_file_classifiers(status):
    from noroshi.cmshpt_parse import classify_cms_hpt
    from noroshi.sectxt_parse import precheck

    assert precheck(status, "text/plain", GOOD, False, None) == "unmeasured"
    assert cls(GOOD, status=status) == "unmeasured"
    assert classify_cms_hpt(status, URL, "text/plain", b"location-name: A\nmrf-url: https://a.org/m.json\n", False,
                            None) == "unmeasured"


@pytest.mark.parametrize("status", [404, 410, 400])
def test_a_4xx_that_is_not_a_refusal_is_still_absent(status):
    from noroshi.cmshpt_parse import classify_cms_hpt

    assert cls(b"<html>not here</html>", status=status, ctype="text/html") == "absent"
    assert classify_cms_hpt(status, URL, "text/html", b"<html>not here</html>", False, None) == "absent"


# ---- post-pilot X3: forbidden

def rec(purpose, status, ua="identified", url=None, error=None, blocked=None):
    from noroshi.store import FetchRecord

    url = url or {"homepage": "https://www.a.org/", "sectxt_wellknown": "https://www.a.org/.well-known/security.txt",
                  "sectxt_root": "https://www.a.org/security.txt", "cmshpt": "https://www.a.org/cms-hpt.txt"}[purpose]
    return FetchRecord("a.org", "hospital", "www.a.org", url, purpose, 1, ua, "2026-11-20T00:00:00Z", status, url, [],
                       "text/plain", None, 0, False, error, status in (401, 403, 406, 429) if blocked is None else blocked)


HOME_OK = rec("homepage", 200)


@pytest.mark.parametrize("purpose", ["sectxt_wellknown", "sectxt_root", "cmshpt"])
@pytest.mark.parametrize("first,retry", [(403, 403), (401, 403), (403, 401), (401, 401)])
def test_both_signatures_refused_with_401_or_403_on_a_loaded_site_is_forbidden(purpose, first, retry):
    from noroshi.sectxt_parse import is_forbidden

    assert is_forbidden([HOME_OK, rec(purpose, first), rec(purpose, retry, "browser")], purpose, lambda r: None)


@pytest.mark.parametrize("records", [
    [HOME_OK, rec("sectxt_wellknown", 403)],  # the browser retry was never sent (a limit was reached)
    [HOME_OK, rec("sectxt_wellknown", 403), rec("sectxt_wellknown", 404, "browser")],
    [HOME_OK, rec("sectxt_wellknown", 403), rec("sectxt_wellknown", 406, "browser")],
    [HOME_OK, rec("sectxt_wellknown", 406), rec("sectxt_wellknown", 403, "browser")],
    [HOME_OK, rec("sectxt_wellknown", 403, "browser")],  # the site already used the browser signature: no pair
    [HOME_OK, rec("sectxt_wellknown", 403), rec("sectxt_wellknown", 403, "browser", error="in_flight", blocked=False)],
    [rec("homepage", 403), rec("homepage", 200, "browser"), rec("sectxt_wellknown", 403, "browser")],
    [rec("homepage", 404), rec("sectxt_wellknown", 403), rec("sectxt_wellknown", 403, "browser")],  # not loaded
    [rec("sectxt_wellknown", 403), rec("sectxt_wellknown", 403, "browser")],  # no homepage record
])
def test_anything_else_is_not_forbidden(records):
    from noroshi.sectxt_parse import is_forbidden

    assert not is_forbidden(records, "sectxt_wellknown", lambda r: None)


def test_the_classifiers_report_forbidden_only_for_a_record_that_is_not_an_answer():
    from noroshi.cmshpt_parse import classify_cms_hpt

    assert classify_security_txt(403, URL, "text/plain", b"Access denied", True, None, NOW, forbidden=True) == "forbidden"
    assert classify_cms_hpt(403, URL, "text/html", b"Forbidden", True, None, forbidden=True) == "forbidden"
    assert classify_security_txt(403, URL, "text/plain", b"Access denied", True, None, NOW) == "unmeasured"
    assert classify_security_txt(200, URL, "text/plain", GOOD, False, None, NOW, forbidden=True) == "valid"


# ---- post-pilot XR2: a block page on either answer is not forbidden

@pytest.mark.parametrize("first,retry,forbidden", [
    (b"Access denied", b"Access denied", True),
    (b"<h1>Forbidden</h1>", b"", True),
    (b"<H1>Access Denied</H1> errors.edgesuite.net", b"Access denied", False),
    (b"Access denied", b"<html><title>Just a moment...</title></html>", False),
    (b"<title>Attention Required! | Cloudflare</title>", b"<title>Attention Required! | Cloudflare</title>", False),
    (b"Request unsuccessful. Incapsula incident ID: 1", b"Access denied", False),
    # Incapsula's script is not a block page on a 403, but since fix round 3 (K5) any script keeps the file unmeasured
    (b'<p>Forbidden</p><script src="/_Incapsula_Resource?SWJIYLWA=1"></script>', b"Access denied", False),
])
def test_forbidden_needs_a_plain_denial_in_both_bodies(first, retry, forbidden):
    from noroshi.sectxt_parse import is_forbidden

    a, b = rec("sectxt_wellknown", 403), rec("sectxt_wellknown", 403, "browser")
    bodies = {id(a): first, id(b): retry}
    assert is_forbidden([HOME_OK, a, b], "sectxt_wellknown", lambda r: bodies.get(id(r))) is forbidden


# ---- fix round 3, K5: forbidden needs both answers plain: no challenge, no block marker, no script

PILOT = Path(__file__).parent / "fixtures" / "pilot"
RIMROCK = (PILOT / "rimrock_sectxt_403_spamfirewall.html").read_bytes()


@pytest.mark.parametrize("first,retry", [
    (RIMROCK, RIMROCK),  # rimrock.org in the second pilot: CleanTalk's challenge to both signatures
    (RIMROCK.replace(b"SpamFireWall", b"Firewall"), RIMROCK.replace(b"SpamFireWall", b"Firewall")),  # no marker left
    (b"<p>Forbidden</p><script>check()</script>", b"Access denied"),
    (b"Access denied", b"<html><SCRIPT src='/c.js'></SCRIPT>Forbidden</html>"),
])
def test_a_denial_whose_body_carries_a_script_keeps_the_file_unmeasured(first, retry):
    from noroshi.sectxt_parse import is_forbidden

    a, b = rec("cmshpt", 403), rec("cmshpt", 403, "browser")
    bodies = {id(a): first, id(b): retry}
    assert not is_forbidden([HOME_OK, a, b], "cmshpt", lambda r: bodies.get(id(r)))


@pytest.mark.parametrize("name", ["abbott_wellknown_403.html", "optum_wellknown_403.html", "insulet_wellknown_403.txt"])
def test_the_first_pilots_stored_plain_denials_stay_forbidden(name):
    from noroshi.sectxt_parse import is_forbidden

    body = (PILOT / name).read_bytes()
    a, b = rec("sectxt_wellknown", 403), rec("sectxt_wellknown", 403, "browser")
    assert is_forbidden([HOME_OK, a, b], "sectxt_wellknown", lambda r: body)
