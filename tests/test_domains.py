import pytest

from noroshi.domains import is_shared_platform, normalize_website, registrable_domain


@pytest.mark.parametrize("raw,host", [
    ("http://www.AndalusiaRegionalHospital.com", "www.andalusiaregionalhospital.com"),
    ("https://example.org:8443/path?q=1", "example.org"),
    ("www.ebaptisthealthcare.org/atmorecommunityhospital/", "www.ebaptisthealthcare.org"),
    ("NOT AVAILABLE", None), ("", None), (None, None), ("http://", None), ("not a url", None),
])
def test_normalize_website(raw, host):
    assert normalize_website(raw) == host


def test_registrable_domain():
    assert registrable_domain("www.ebaptisthealthcare.org") == "ebaptisthealthcare.org"
    assert registrable_domain("trust.nhs.uk") == "trust.nhs.uk"  # nhs.uk is a public suffix
    assert registrable_domain("a.b.example.co.uk") == "example.co.uk"


def test_shared_platform():
    assert is_shared_platform("facebook.com") and not is_shared_platform("hcahealthcare.com")


def test_private_suffixes_are_suffixes():
    from noroshi.domains import is_suffix

    assert registrable_domain("pydicom.github.io") == "pydicom.github.io"
    assert registrable_domain("www.pydicom.github.io") == "pydicom.github.io"
    assert registrable_domain("a.b.blogspot.com") == "b.blogspot.com"
    assert is_suffix("github.io") and is_suffix("co.uk") and is_suffix("gov") and is_suffix("blogspot.com")
    assert not is_suffix("pydicom.github.io") and not is_suffix("va.gov") and not is_suffix("example.co.uk")


def test_domain_label_treats_private_suffixes_as_suffixes():
    from noroshi.domains import domain_label

    assert domain_label("pydicom.github.io") == "pydicom"
    assert domain_label("www.medtronic.com") == "medtronic"
    assert domain_label("trust.nhs.uk") == "trust"
    assert domain_label("a.b.example.co.uk") == "example"


# ---- post-pilot X1: one spelling per URL

@pytest.mark.parametrize("raw,url", [
    ("https://www.samhsa.gov:443/robots.txt", "https://www.samhsa.gov/robots.txt"),
    ("HTTPS://WWW.Samhsa.GOV:443/Robots.txt", "https://www.samhsa.gov/Robots.txt"),
    ("https://womenshealth.gov:443/menopause", "https://womenshealth.gov/menopause"),
    ("http://a.org:80/x?q=1#f", "http://a.org/x?q=1#f"),
    ("https://a.org./x", "https://a.org/x"),
    ("https://a.org:8443/x", "https://a.org:8443/x"),
    ("http://a.org:443/", "http://a.org:443/"),
    ("https://a.org:80/", "https://a.org:80/"),
    ("https://a.org", "https://a.org"),
    ("https://www.samhsa.gov/robots.txt", "https://www.samhsa.gov/robots.txt"),
    ("http://a.org:99999/x", "http://a.org:99999/x"),  # no request can be built: left as it is
    ("mailto:a@b.org", "mailto:a@b.org"),
])
def test_normalize_url(raw, url):
    from noroshi.domains import normalize_url

    assert normalize_url(raw) == url
    assert normalize_url(url) == url
