from noroshi.cmshpt_parse import classify_cms_hpt

URL = "https://hospital.example.org/cms-hpt.txt"
GOOD = (b"location-name: Example General Hospital\n"
        b"source-page-url: https://hospital.example.org/price-transparency\n"
        b"mrf-url: https://hospital.example.org/files/123456789_standardcharges.json\n"
        b"contact-name: MRF Team\ncontact-email: mrf@hospital.example.org\n")


def cls(body, status=200, ctype="text/plain", blocked=False, error=None):
    return classify_cms_hpt(status, URL, ctype, body, blocked, error)


def test_cms_hpt_valid():
    assert cls(GOOD) == "present_valid"


def test_cms_hpt_field_names_case_insensitive():
    assert cls(b"Location-Name: A\nMRF-URL: http://h.example.org/f.csv\n") == "present_valid"


def test_cms_hpt_multiple_locations():
    assert cls(GOOD + b"\n" + GOOD) == "present_valid"


def test_cms_hpt_html():
    assert cls(b"<html>nope</html>", ctype="text/html") == "html_error"
    assert cls(b"<!DOCTYPE html><html>") == "html_error"


def test_cms_hpt_text_without_fields():
    assert cls(b"hello world\n") == "present_invalid"
    assert cls(b"") == "present_invalid"


def test_cms_hpt_missing_one_field():
    assert cls(b"location-name: A\n") == "present_invalid"
    assert cls(b"mrf-url: https://h.example.org/f.json\n") == "present_invalid"


def test_cms_hpt_mrf_url_must_be_http():
    assert cls(b"location-name: A\nmrf-url: ftp://h.example.org/f.json\n") == "present_invalid"
    assert cls(b"location-name: A\nmrf-url: \n") == "present_invalid"


def test_cms_hpt_bom():
    assert cls(b"\xef\xbb\xbf" + GOOD) == "present_valid"


def test_cms_hpt_status_rules():
    assert cls(b"", status=404) == "absent"
    assert cls(b"", status=503) == "unmeasured"
    assert cls(b"", status=403, blocked=True) == "unmeasured"
    assert cls(None, status=None, error="timeout") == "unmeasured"
    assert cls(GOOD, error="x") == "unmeasured"


def test_precheck_rules_identical_to_security_txt():
    from datetime import datetime, timezone

    from noroshi.sectxt_parse import classify_security_txt

    now = datetime(2026, 11, 20, tzinfo=timezone.utc)
    shared = {"unmeasured": "unmeasured", "absent": "absent", "html_error": "html_error"}
    cases = [(None, None, b"", False, "x"), (200, "text/plain", b"", True, None),
             (502, None, b"", False, None), (404, None, b"", False, None),
             (302, None, b"", False, None), (200, "text/html", b"x", False, None),
             (200, "text/plain", b"<html>", False, None)]
    for status, ctype, body, blocked, error in cases:
        a = classify_security_txt(status, URL, ctype, body, blocked, error, now)
        b = classify_cms_hpt(status, URL, ctype, body, blocked, error)
        assert a == b and a in shared
