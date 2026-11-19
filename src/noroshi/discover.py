"""Find vulnerability-disclosure policy pages from homepage links."""
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

from noroshi.domains import normalize_url, url_key

PROBE_PATHS = ("/vulnerability-disclosure-policy", "/responsible-disclosure", "/security",
               "/vulnerability-disclosure", "/product-security", "/psirt")
PLATFORM_HOSTS = ("hackerone.com", "bugcrowd.com", "intigriti.com", "yeswehack.com")
MAX_CANDIDATES = 3

# Specific disclosure wording in the text of a stored page: the judge's prefilter and excerpt windows use it (and
# derive, for a platform link's own wording). Candidate links have their own patterns below; the two must not be merged.
# Two phrases are wordings real policy pages in the pilot use (post-pilot Y2), and one a second-pilot page uses (fix
# round 3, K4); nothing of the general link tier ("security", "trust center") belongs here.
PAGE_TEXT_PATTERN = re.compile(
    r"vulnerabilit(?:y|ies)\s+disclos"
    r"|responsible\s+disclos"
    r"|coordinated\s+(?:vulnerability\s+)?disclos"
    r"|report\s+(?:a\s+)?(?:security\s+)?(?:vulnerabilit|security\s+(?:issue|bug|flaw|concern))"
    r"|bug\s+bounty"
    r"|security\s+researchers?"
    r"|product\s+security"
    r"|psirt"
    r"|vulnerabilit(?:y|ies)\s+report"
    r"|coordinated\s+vulnerability"
    r"|vulnerabilit(?:y|ies)\s+rewards?",  # fix round 3, K4: roche.com's "Vulnerability Rewards Program"
    re.IGNORECASE,
)
# Candidate links, specific tier: link text or address (the text pattern is matched against both).
LINK_TEXT_PATTERN = re.compile(
    r"vulnerabilit(?:y|ies)\s+disclos"
    r"|responsible\s+disclos"
    r"|coordinated\s+(?:vulnerability\s+)?disclos"
    r"|report\s+(?:a\s+)?(?:security\s+)?(?:vulnerabilit|security\s+(?:issue|bug|flaw|concern))"
    r"|bug\s+bounty"
    r"|security\s+researchers?"
    r"|product\s+security"
    r"|psirt"
    r"|vulnerabilit(?:y|ies)\s+report"
    r"|coordinated\s+vulnerability"
    r"|vulnerabilit(?:y|ies)\s+rewards?",  # fix round 3, K4: roche.com's "Vulnerability Rewards Program"
    re.IGNORECASE,
)
LINK_HREF_PATTERN = re.compile(
    r"/vulnerability-disclosure|/responsible-disclosure|/product-security|/psirt|/bug-bounty"
    r"|/vulnerabilit|/coordinated-disclosure|/security-disclosure",
    re.IGNORECASE,
)
# Candidate links, general tier: short link text (trimmed: at most 5 words and 40 characters) that contains one of
# these as whole words, any case, and not "social security"; or an address whose whole path is one of these paths
# (with or without a trailing slash or .html).
GENERAL_LINK_WORDS = re.compile(r"\b(?:security|cybersecurity|cyber\s+security|trust\s+(?:center|centre|hub|portal))\b",
                                re.IGNORECASE)
GENERAL_LINK_EXCLUDED = re.compile(r"\bsocial\s+security\b", re.IGNORECASE)
GENERAL_LINK_MAX_WORDS = 5
GENERAL_LINK_MAX_CHARS = 40
GENERAL_LINK_PATH = re.compile(r"/(?:security|cybersecurity|cyber-security|trust|trust-center)(?:/|\.html)?",
                               re.IGNORECASE)


@dataclass(frozen=True)
class Link:
    url: str
    text: str


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.base_href: str | None = None  # the href of the first <base> that has one, as written
        self.found: list[tuple[str, str]] = []  # (href as written, anchor text), in page order
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "base" and self.base_href is None and "href" in dict(attrs):
            self.base_href = dict(attrs)["href"] or ""
        if tag == "a":
            self._flush()
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_endtag(self, tag):
        if tag == "a":
            self._flush()

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def _flush(self):
        href, self._href = self._href, None
        if href:
            self.found.append((href, " ".join("".join(self._text).split())))


def _resolve(base: str, href: str) -> str | None:
    """The http or https address `href` names against `base`, without its fragment; None for anything else."""
    try:
        url, _ = urldefrag(urljoin(base, href.strip()))
        parts = urlsplit(url)
    except ValueError:
        return None
    return url if parts.scheme in ("http", "https") and parts.hostname else None


def extract_links(html: bytes, base_url: str) -> list[Link]:
    """The links of a page whose own (final) URL is `base_url`, resolved as a browser does: against the first
    `<base href>` when it gives an http or https address, else against the page's URL (fix round 3, K3)."""
    parser = _Collector()
    parser.feed(html.decode("utf-8", errors="replace"))
    parser.close()
    parser._flush()
    base = (_resolve(base_url, parser.base_href) if parser.base_href and parser.base_href.strip() else None) or base_url
    links = []
    for href, text in parser.found:
        url = _resolve(base, href)
        if url is not None:
            links.append(Link(url, text))
    return links


def is_platform_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return any(host == p or host.endswith("." + p) for p in PLATFORM_HOSTS)


def is_platform_link(url: str) -> bool:
    """A platform link counts only when its path is not empty."""
    return is_platform_host(url) and urlsplit(url).path.strip("/") != ""


def _specific(link: Link) -> bool:
    href = unquote(link.url)
    return bool(LINK_TEXT_PATTERN.search(link.text) or LINK_TEXT_PATTERN.search(href) or LINK_HREF_PATTERN.search(href))


def _general(link: Link) -> bool:
    text = link.text.strip()
    short = len(text.split()) <= GENERAL_LINK_MAX_WORDS and len(text) <= GENERAL_LINK_MAX_CHARS
    return ((short and GENERAL_LINK_WORDS.search(text) is not None and GENERAL_LINK_EXCLUDED.search(text) is None)
            or GENERAL_LINK_PATH.fullmatch(urlsplit(unquote(link.url)).path) is not None)


def select_candidates(links: list[Link]) -> tuple[list[str], list[str]]:
    """At most 3 fetchable URLs, each in its one spelling and once (by the crawl's already-requested key, `url_key`):
    the specific tier in page order, then the general tier in page order; and every platform link."""
    specific: list[str] = []
    general: list[str] = []
    platform: list[str] = []
    for link in links:
        if is_platform_host(link.url):  # never fetched; counted only when the path is not empty
            if is_platform_link(link.url) and link.url not in platform:
                platform.append(link.url)
        elif _specific(link):
            specific.append(normalize_url(link.url))
        elif _general(link):
            general.append(normalize_url(link.url))
    chosen: dict[str, str] = {}  # key -> the first spelling met, specific tier first
    for url in specific + general:
        chosen.setdefault(url_key(url), url)
    return list(chosen.values())[:MAX_CANDIDATES], platform
