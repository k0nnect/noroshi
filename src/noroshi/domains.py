"""Website-string normalisation and registrable-domain helpers."""
import re
from urllib.parse import urlsplit

import tldextract

# Bundled suffix list only: no network fetch, no cache directory. Private suffixes (github.io, blogspot.com,
# ...) count as suffixes, so pydicom.github.io is its own registrable domain.
_extract = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True)

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})+$")

SHARED_PLATFORM_DOMAINS: frozenset[str] = frozenset({
    "facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com",
    "youtube.com", "wikipedia.org", "google.com", "yelp.com", "healthgrades.com",
    "medicare.gov",
    # Added in Task 4 after reviewing the HIFLD hospital data (none is a hospital or health-system site).
    # Association directory: one state hospital association page per member hospital.
    "wsha.org",
    # Federal health portals; those hospitals belong to the federal frame, not the hospital frame.
    "va.gov", "tricare.mil", "ihs.gov", "navy.mil",
    # State and city government portals.
    "ny.gov", "state.tx.us", "virginia.gov", "pa.gov", "ohio.gov", "sc.gov", "state.nj.us",
    "ca.gov", "mo.gov", "wa.gov", "oregon.gov", "mass.gov", "state.fl.us", "state.md.us",
    "ks.gov", "state.sc.us", "louisiana.gov", "ct.gov", "in.gov", "kentucky.gov", "texas.gov",
    "state.wi.us", "utah.gov", "arkansas.gov", "wi.gov", "state.ms.us", "state.ne.us",
    "maine.gov", "ms.gov", "hawaii.gov", "alabama.gov", "ri.gov", "georgia.gov", "sd.gov",
    "state.co.us", "state.ia.us", "state.mo.us", "nyc.gov",
    # County and city general-government portals (added after the coordinator's ruling).
    "waukeshacounty.gov", "brown.wi.us", "wood.wi.us", "winkler.tx.us", "sutter.ca.us",
    "indian-river.fl.us", "lynch.ne.us", "buttecounty.net", "charlestoncounty.org",
    "essexcountynj.org", "hudsoncountynj.org", "littlerivercounty.org", "milwaukeecounty.org",
    # Third-party sites found by the frames audit (see data/frames/shared_platform_review.csv).
    "ucnj.org", "ripley.ms", "sjgov.org", "srnm.org",
    "namisacramento.org", "spechospnjfdn.org",
    "myflfamilies.com", "wvdhhr.org", "scvrd.net",  # state agency sites (controller ruling)
    "smma.com", "shopaitribes.org", "alaweb.com", "brokenbow-ne.com",
    # Free site builder: many unrelated businesses.
    "business.site",
})

# Domain sale and parking services: a listed domain whose homepage lands here is not the organization's site.
PARKING_DOMAINS: frozenset[str] = frozenset({
    "hugedomains.com", "sedo.com", "dan.com", "afternic.com", "bodis.com", "parkingcrew.net", "above.com",
    "sedoparking.com", "godaddy.com",
})

# Federal health portals. They stay in SHARED_PLATFORM_DOMAINS: a hospital listed on one of them is a federal
# facility, so for the hospital sector they are third parties. In every other sector they are the
# organizations' own sites (the federal frame lists them; IHS is also an EHR developer).
FEDERAL_PORTAL_DOMAINS: frozenset[str] = frozenset({"va.gov", "tricare.mil", "ihs.gov", "navy.mil", "medicare.gov"})


def third_party_domains(sector: str) -> frozenset[str]:
    """Domains that are never an organization's own site in this sector: a listed domain whose homepage lands
    on one is a mismatch, and no domain table of the sector may hold one."""
    if sector == "hospital":
        return SHARED_PLATFORM_DOMAINS | PARKING_DOMAINS
    return (SHARED_PLATFORM_DOMAINS - FEDERAL_PORTAL_DOMAINS) | PARKING_DOMAINS


def normalize_website(raw: str | None) -> str | None:
    """Lowercase host without scheme, port or path; None if empty or malformed."""
    if raw is None:
        return None
    s = raw.strip()
    if not s or s.upper() == "NOT AVAILABLE":
        return None
    if "://" not in s:
        s = "//" + s
    try:
        host = urlsplit(s).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower().rstrip(".")
    return host if _HOST_RE.match(host) else None


_DEFAULT_PORTS = {"https": 443, "http": 80}


def normalize_url(url: str) -> str:
    """One spelling per URL: the scheme and host in lower case, no default port (:443 on https, :80 on http) and no
    trailing dot on the host. Path, query and fragment are kept as written. A URL whose host or port cannot be read
    comes back unchanged (no request can be built from it)."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return url
    head = f"{parts.scheme}://{parts.netloc}"  # urlsplit gives the scheme in lower case, the netloc as written
    if not parts.hostname or url[:len(head)].lower() != head.lower():
        return url
    scheme = parts.scheme.lower()
    host = parts.hostname.rstrip(".")  # urlsplit gives the host in lower case
    if ":" in host:
        host = f"[{host}]"  # an IPv6 address
    user = parts.netloc.rpartition("@")[0]
    netloc = (f"{user}@" if user else "") + host
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        netloc += f":{port}"
    return f"{scheme}://{netloc}{url[len(head):]}"


def url_key(url: str) -> str:
    """The key of a URL among URLs already requested or chosen: its one spelling (`normalize_url`), without fragment or
    trailing slash."""
    return normalize_url(url).split("#", 1)[0].rstrip("/")


def registrable_domain(host: str) -> str:
    """eTLD+1 of a host, e.g. www.example.co.uk -> example.co.uk."""
    ext = _extract(host)
    return ext.top_domain_under_public_suffix or host


def known_registrable_domain(host: str) -> str | None:
    """The registrable domain of a host that ends in a recognized public or private suffix and has a label
    before it; None for anything else (a bare suffix, or a name under no known suffix such as build.local)."""
    ext = _extract(host)
    return ext.top_domain_under_public_suffix if ext.suffix and ext.domain else None


def domain_label(host: str) -> str:
    """The label just before the public or private suffix, e.g. www.medtronic.com -> medtronic,
    pydicom.github.io -> pydicom."""
    return _extract(host).domain


def is_suffix(domain: str) -> bool:
    """True when the whole name is a public or private suffix (gov, co.uk, github.io), not a domain under one."""
    ext = _extract(domain)
    return bool(ext.suffix) and not ext.domain and not ext.subdomain


def is_shared_platform(domain: str) -> bool:
    return domain.lower() in SHARED_PLATFORM_DOMAINS
