from noroshi.blocking import detect_block, looks_like_html


def test_status_blocks():
    for s in (401, 403, 406, 429):
        assert detect_block(s, {}, b"")
    assert not detect_block(404, {}, b"") and not detect_block(200, {}, b"Contact: mailto:a@b.org")


def test_block_page_200_challenge_detected():
    assert detect_block(200, {}, b"<html><title>Just a moment...</title>")
    assert detect_block(503, {"cf-mitigated": "challenge"}, b"")
    assert detect_block(200, {}, b"<H1>Access Denied</H1> errors.edgesuite.net")
    assert detect_block(200, {}, b"Request unsuccessful. Incapsula incident ID")


def test_other_markers_and_header_case():
    assert detect_block(200, {}, b"<title>Attention Required! | Cloudflare</title>")
    assert detect_block(200, {}, b'<div id="px-captcha"></div>')
    assert detect_block(200, {}, b"<h1>Pardon Our Interruption</h1>")
    assert detect_block(503, {"CF-Mitigated": "challenge"}, b"")


def test_ordinary_pages_not_blocked():
    assert not detect_block(200, {}, b"<html><body>Access denied is a phrase we discuss.</body>")
    assert not detect_block(None, {}, b"")
    assert not detect_block(503, {}, b"maintenance")


def test_looks_like_html():
    assert looks_like_html("text/html; charset=utf-8", b"x")
    assert looks_like_html("text/plain", b"\n <!DOCTYPE html><html>")
    assert not looks_like_html("text/plain", b"Contact: mailto:security@example.org\n")
    assert not looks_like_html(None, b"")


def test_just_a_moment_only_counts_in_title():
    page = b"<html><title>Clinic</title><body><p>Just a moment... loading your results</p></body>"
    assert not detect_block(200, {}, page)
    assert detect_block(200, {}, b"<HTML><TITLE>Just a moment...</TITLE>")


def test_an_aws_waf_action_header_is_a_refusal_whatever_its_value_or_status():
    for status in (202, 200, 405, 503):
        assert detect_block(status, {"x-amzn-waf-action": "challenge"}, b"")
    assert detect_block(202, {"X-Amzn-Waf-Action": "captcha"}, b"")
    assert not detect_block(202, {}, b"")


# ---- post-pilot X8: the Incapsula script challenge served with status 200

def test_the_stored_intuitive_homepage_is_a_refusal():
    from pathlib import Path

    body = (Path(__file__).parent / "fixtures" / "pilot" / "intuitive_home.html").read_bytes()  # pilot #468, 212 bytes
    assert detect_block(200, {"content-type": "text/html"}, body)
    assert detect_block(200, {}, b'<script src="/_Incapsula_Resource?SWJIYLWA=1,2"></script>')


# ---- post-pilot XR1: the Incapsula script marker counts only on an empty 200 page

def pilot(name):
    from pathlib import Path

    return (Path(__file__).parent / "fixtures" / "pilot" / name).read_bytes()


def test_the_stored_intuitive_challenge_at_a_conventional_path_is_a_refusal():
    import hashlib

    body = pilot("intuitive_home.html")  # #587 (status 200 at /vulnerability-disclosure-policy) stored these bytes
    assert hashlib.sha256(body).hexdigest() == "d02032286070b4dd9d8fbd985a7bdca8af8edf52b89ff177db3bfcb2c8a9c43d"
    assert detect_block(200, {"content-type": "text/html"}, body)


def test_the_stored_intuitive_404_security_txt_page_is_not_a_refusal_and_is_absent():
    from datetime import datetime, timezone

    from noroshi.sectxt_parse import classify_security_txt

    body = pilot("intuitive_wellknown_404.html")  # #517: an IIS 404 page with Incapsula's script appended
    assert b"_Incapsula_Resource" in body
    blocked = detect_block(404, {"content-type": "text/html"}, body)
    assert not blocked
    assert classify_security_txt(404, "https://www.intuitive.com/.well-known/security.txt", "text/html", body, blocked,
                                 None, datetime(2026, 11, 20, tzinfo=timezone.utc)) == "absent"


def test_a_200_page_with_the_script_and_real_text_is_not_a_refusal():
    script = b'<script src="/_Incapsula_Resource?SWJIYLWA=719d34d31c8e3a6e6fffd425f7e032f3&ns=1"></script>'
    text = b"<p>" + b"Our clinicians care for patients across the valley. " * 4 + b"</p>"  # 212 visible characters
    assert not detect_block(200, {}, b"<html><body>" + text + script + b"</body></html>")
    assert detect_block(200, {}, b"<html><body><p>Loading</p>" + script + b"</body></html>")
    assert not detect_block(503, {}, b"<html><body><p>Loading</p>" + script + b"</body></html>")


def test_the_visible_text_measure_is_the_meta_refresh_rules():
    from noroshi import blocking, crawl

    page = b"<title>T</title><script>x()</script><style>p{}</style><p>a  b</p>"
    assert blocking.visible_text(page) == " ".join(" ".join(crawl._parse(page).parts).split()) == "a b"
    assert crawl.MIN_VISIBLE_CHARS == blocking.MIN_VISIBLE_CHARS == 200


# ---- post-pilot XR6: Akamai's block page as Akamai sends it

def test_the_stored_akamai_page_is_a_block_page_by_its_body_alone():
    from noroshi.blocking import block_page

    body = pilot("fortwashingtonmc_home_403.html")  # #10 as stored: errors&#46;edgesuite&#46;net
    assert b"errors&#46;edgesuite&#46;net" in body and b"errors.edgesuite.net" not in body
    assert block_page(200, body) and block_page(403, body) and detect_block(200, {}, body)
    assert block_page(200, b"<p>https://errors.edgesuite.net/18.1</p>")  # the plain form still counts


# ---- fix round 3, K5: CleanTalk's SpamFireWall challenge is a block page

def test_the_stored_cleantalk_challenge_is_a_block_page():
    """rimrock.org in the second pilot (#226, cut down; the crawler's address replaced by a documentation one): the
    same page answered both security.txt locations and cms-hpt.txt, to both signatures."""
    from noroshi.blocking import block_page

    body = pilot("rimrock_sectxt_403_spamfirewall.html")
    assert block_page(403, body) and block_page(200, body) and detect_block(200, {}, body)
    assert block_page(403, b"<h1>SPAMFIREWALL is checking your browser</h1>")  # in any case, as the other markers
