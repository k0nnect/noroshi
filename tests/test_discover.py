from pathlib import Path

import pytest

from noroshi.discover import (LINK_HREF_PATTERN, LINK_TEXT_PATTERN, PAGE_TEXT_PATTERN, PLATFORM_HOSTS, PROBE_PATHS,
                              Link, extract_links, select_candidates)

PILOT = Path(__file__).parent / "fixtures" / "pilot"


def test_extract_links_resolves_and_drops_non_http():
    html = (b'<a href="/a#frag">Vulnerability  <b>Disclosure</b></a><a href="mailto:x@y.z">m</a>'
            b'<a href="javascript:void(0)">j</a><a href="https://other.org/p">o</a><a>none</a>')
    got = extract_links(html, "https://x.org/home/")
    assert got == [Link("https://x.org/a", "Vulnerability Disclosure"), Link("https://other.org/p", "o")]


def test_select_candidates_caps_at_three_and_separates_platforms():
    links = [Link(f"https://x.org/p{i}", "Responsible Disclosure") for i in range(5)]
    links += [Link("https://hackerone.com/acme", "Report"), Link("https://hackerone.com/", "Bug bounty"),
              Link("https://www.bugcrowd.com/engagements/acme", "x"), Link("https://x.org/p0", "Responsible Disclosure")]
    fetch, platform = select_candidates(links)
    assert fetch == [f"https://x.org/p{i}" for i in range(3)]
    assert platform == ["https://hackerone.com/acme", "https://www.bugcrowd.com/engagements/acme"]


def test_campus_security_links_are_general_tier_candidates():
    """Post-pilot XR4: short link text with the whole word "security" is in the general tier."""
    links = [Link("https://x.org/parking", "Security & Parking"), Link("https://x.org/campus-security", "Campus Security")]
    assert select_candidates(links) == (["https://x.org/parking", "https://x.org/campus-security"], [])


def test_anchor_text_and_href_patterns():
    for text in ("Vulnerabilities disclosure", "Coordinated Vulnerability Disclosure", "Report a security issue",
                 "Report vulnerability", "Bug Bounty", "Security Researchers", "Product Security", "PSIRT"):
        assert LINK_TEXT_PATTERN.search(text), text
    assert not LINK_TEXT_PATTERN.search("Security & Parking")
    for h in ("/vulnerability-disclosure", "/responsible-disclosure/x", "/Product-Security", "/psirt", "/bug-bounty"):
        assert LINK_HREF_PATTERN.search(h), h
    links = [Link("https://x.org/legal/vulnerability-disclosure", "Legal")]
    assert select_candidates(links)[0] == ["https://x.org/legal/vulnerability-disclosure"]


def test_constants():
    assert PROBE_PATHS[0] == "/vulnerability-disclosure-policy" and len(PROBE_PATHS) == 6
    assert "hackerone.com" in PLATFORM_HOSTS


def test_bare_platform_links_are_dropped_not_fetched():
    links = extract_links(b'<a href="https://hackerone.com/">Bug Bounty</a>'
                          b'<a href="https://bugcrowd.com">Report a vulnerability</a>'
                          b'<a href="https://x.org/vulnerability-disclosure">VDP</a>', "https://x.org/")
    assert select_candidates(links) == (["https://x.org/vulnerability-disclosure"], [])


# ---- post-pilot X4: two tiers of candidate links

@pytest.mark.parametrize("name,base,wanted", [
    ("optum_home.html", "https://business.optum.com/en/?src=chc", ["https://www.optum.com/en/vulnerability.html"]),
    ("canon_home.html", "https://www.mi.medical.canon/",
     ["https://www.mi.medical.canon/about-us/contact-us/coordinated-vulnerability-reporting/",  # specific tier first
      "https://www.mi.medical.canon/support-and-services/customer-support/security-bulletins/"]),  # XR4: general
    ("eppendorf_home.html", "https://www.eppendorf.com/us-en", ["https://www.eppendorf.com/us-en/cybersecurity/"]),
])
def test_the_three_definite_misses_of_the_pilot_are_selected(name, base, wanted):
    links = extract_links((PILOT / name).read_bytes(), base)
    assert select_candidates(links) == (wanted, [])


@pytest.mark.parametrize("text", ["Vulnerability report", "Vulnerabilities reporting", "Coordinated Vulnerability Reporting",
                                  "Report a security issue", "Report security issue", "Report a vulnerability"])
def test_the_specific_tier_text(text):
    assert select_candidates([Link("https://x.org/p", text)])[0] == ["https://x.org/p"]


@pytest.mark.parametrize("path", ["/en/vulnerability.html", "/about/coordinated-disclosure", "/legal/security-disclosure/",
                                  "/Vulnerabilities"])
def test_the_specific_tier_address(path):
    assert select_candidates([Link("https://x.org" + path, "More")])[0] == ["https://x.org" + path]


@pytest.mark.parametrize("text", ["Security", " cybersecurity ", "Cyber Security", "PRODUCT SECURITY", "Security & Privacy",
                                  "Privacy & Security", "Trust Center", "Trust Centre", "Security Center", "security centre"])
def test_the_general_tier_text(text):
    assert select_candidates([Link("https://x.org/p", text)])[0] == ["https://x.org/p"]


@pytest.mark.parametrize("text", ["Social Security Administration", "Learn more about how we approach security at our company",
                                  "Securities", "Report an issue", "Report issue", "Report Issue with Certified Health IT",
                                  "Trust us", "Trustee portal", "Insecurity", "Our trusted partners", "Trust Bank Center"])
def test_other_wording_is_not_a_candidate(text):
    assert select_candidates([Link("https://x.org/p", text)])[0] == []


@pytest.mark.parametrize("path,chosen", [
    ("/security", True), ("/security/", True), ("/security.html", True), ("/Cybersecurity", True),
    ("/cyber-security/", True), ("/trust", True), ("/trust-center.html", True), ("/trust-center/", True),
    ("/en-us/about/cybersecurity", False), ("/security-bulletins", False), ("/trust/center", False),
    ("/securityx", False), ("/security/policy", False)])
def test_the_general_tier_address_is_the_whole_path(path, chosen):
    assert select_candidates([Link("https://x.org" + path, "Read more")])[0] == (["https://x.org" + path] if chosen else [])


def test_the_specific_tier_comes_first_and_three_in_all():
    links = [Link("https://x.org/a", "Security"), Link("https://x.org/b", "Trust Center"),
             Link("https://x.org/c", "Vulnerability report"), Link("https://x.org/d", "Cybersecurity"),
             Link("https://x.org/e", "Responsible Disclosure")]
    assert select_candidates(links)[0] == ["https://x.org/c", "https://x.org/e", "https://x.org/a"]
    general_first = [Link("https://x.org/a", "Security"), Link("https://x.org/a", "Vulnerability report")]
    assert select_candidates(general_first)[0] == ["https://x.org/a"]  # one URL once, in its specific place


def test_the_page_prefilter_keeps_its_own_specific_wording():
    from noroshi.judge import prefilter

    import inspect

    from noroshi import discover

    source = inspect.getsource(discover)  # two definitions (re.compile returns one cached object for equal text)
    assert "\nPAGE_TEXT_PATTERN = re.compile(" in source and "\nLINK_TEXT_PATTERN = re.compile(" in source
    for text in ("Security", "Cybersecurity", "Trust Center", "Trust Centre", "Security & Privacy"):
        assert select_candidates([Link("https://x.org/p", text)])[0] == ["https://x.org/p"]  # a candidate link ...
        assert not prefilter(text), text  # ... but not wording that sends a stored page to the judge
    assert prefilter("Vulnerability Disclosure Policy") and prefilter("Report a security vulnerability")


# ---- post-pilot XR4: a wider general tier, by whole words in short link text

@pytest.mark.parametrize("url,text", [  # as the pilot homepages carry them
    ("https://www.mindray.com/na/solutions/hospitalwide-solution/cybersecurity/", "Cybersecurity and Privacy"),
    ("https://www.exo.inc/security-trust-center", "Security Trust Center"),
    ("https://quibim.ai/trust-hub/", "Trust Hub"),
    ("https://quibim.ai/security-measures/", "Security Measures"),
    ("https://x.org/p", "Trust Portal"), ("https://x.org/p", "Cyber Security Notices"), ("https://x.org/p", "IT security"),
    ("https://x.org/p", "Cyber-Security"), ("https://x.org/p", "Trust  Centre"),
])
def test_the_general_tier_takes_short_text_with_a_security_or_trust_word(url, text):
    assert select_candidates([Link(url, text)])[0] == [url]


def test_healthit_complaint_link_is_no_longer_selected():
    links = [Link("https://healthit.gov/certification-health-it/certified-health-it-complaint-process/",
                  "Report Issue with Certified Health IT"),
             Link("https://www.hhs.gov/vulnerability-disclosure-policy/index.html", "Vulnerability Disclosure Policy")]
    assert select_candidates(links)[0] == ["https://www.hhs.gov/vulnerability-disclosure-policy/index.html"]


def test_the_general_tier_limits():
    assert select_candidates([Link("https://x.org/p", "a b c d security")])[0] == ["https://x.org/p"]  # 5 words
    assert select_candidates([Link("https://x.org/p", "a b c d e security")])[0] == []  # 6 words
    forty = "Security " + "x" * 31
    assert len(forty) == 40 and select_candidates([Link("https://x.org/p", forty)])[0] == ["https://x.org/p"]
    assert select_candidates([Link("https://x.org/p", forty + "x")])[0] == []  # 41 characters
    assert select_candidates([Link("https://x.org/social-security", "Social Security")])[0] == []
    assert select_candidates([Link("https://x.org/security", "Social Security")])[0] == ["https://x.org/security"]  # path


# ---- post-pilot fix round 1, F9: candidates are de-duplicated with the crawl's "already fetched" key

def test_two_spellings_of_one_address_take_one_candidate_slot():
    links = [Link("https://x.org/security", "Security"), Link("https://X.org:443/security/", "Security"),
             Link("https://x.org/trust", "Trust Center"), Link("https://x.org/vdp", "Vulnerability Disclosure")]
    assert select_candidates(links)[0] == ["https://x.org/vdp", "https://x.org/security", "https://x.org/trust"]


def test_the_candidate_key_is_the_crawls_already_fetched_key():
    from noroshi import crawl
    from noroshi.domains import url_key

    for url in ("https://X.org:443/a/", "https://x.org/a#top", "https://x.org/a"):
        assert crawl._norm_url(url) == url_key(url) == "https://x.org/a"


# ---- fix round 3, K3: links resolve against the page's <base href>

def test_links_resolve_against_the_base_href_of_the_stored_ypsomed_homepage():
    """ypsomed.com in the second pilot (#837, cut down to its head and the link): the link was resolved against the
    final URL to /en/en/... and the candidate answered 404."""
    links = extract_links((PILOT / "ypsomed_home_base.html").read_bytes(), "https://www.ypsomed.com/en/")
    wanted = "https://www.ypsomed.com/en/legal-information/vulnerability-disclosure"
    assert [link.url for link in links] == [wanted]
    assert select_candidates(links)[0] == [wanted]


@pytest.mark.parametrize("base", ["", "<base target='_top'>", "<base href=''>", "<base href>", "<base href='  '>",
                                  "<base href='javascript:void(0)'>", "<base href='mailto:a@b.org'>",
                                  "<base href='http://[::1'>"])
def test_a_missing_empty_or_unusable_base_falls_back_to_the_pages_url(base):
    html = f"<head>{base}</head><a href='en/x'>X</a>".encode()
    assert [link.url for link in extract_links(html, "https://a.org/en/")] == ["https://a.org/en/en/x"]


def test_a_relative_base_resolves_against_the_page_and_the_first_base_with_an_href_counts():
    html = b"<a href='x'>X</a><head><base target='_top'><base href='/root/'><base href='https://o.org/'></head>"
    assert [link.url for link in extract_links(html, "https://a.org/en/p")] == ["https://a.org/root/x"]


# ---- fix round 3, K4: "vulnerability rewards" wording counts

def test_the_stored_roche_vulnerability_rewards_page_passes_the_prefilter():
    """roche.com in the second pilot (#663, cut down to its passage): never shown to a model."""
    from noroshi.blocking import visible_text
    from noroshi.judge import prefilter

    assert prefilter(visible_text((PILOT / "roche_security_rewards.html").read_bytes()))


@pytest.mark.parametrize("text", ["Vulnerability Rewards Programme", "Vulnerability reward", "vulnerabilities rewards"])
def test_vulnerability_rewards_is_page_wording_and_specific_link_wording(text):
    assert PAGE_TEXT_PATTERN.search(text)
    assert select_candidates([Link("https://x.org/p", text)])[0] == ["https://x.org/p"]
