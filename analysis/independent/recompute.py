"""Independent recomputation of the noroshi flow table and main/strict share table from the raw store.

Written from protocol/PROTOCOL.md (sections 4 to 8) only. It does not import any `noroshi` module and does not read
the study's result code or derived data. It reads, read-only:
  data/frames/{hospital,federal,device,icsma,ehr}_domains.csv   the five lists
  data/raw/crawl.sqlite (tables requests, done, browser_sites)  one row per attempt
  data/raw/blobs/<body_sha256>                                  stored bodies
  data/raw/identity.jsonl                                       identity decisions (stored model answers)
  info/optout.txt                                               the opt-out list

Run (one command, from the repository root):
  uv run --frozen --offline python "../noroshi-paper/analysis/independent/recompute.py"

Writes ../noroshi-paper/analysis/independent/recompute_output.json (relative to the repository root).
Nothing is sent to any website.
"""
import collections
import csv
import datetime as dt
import html
import json
import re
import sqlite3
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import tldextract
from scipy.stats import beta

REPO = Path.cwd()
if not (REPO / "protocol" / "PROTOCOL.md").exists():
    sys.exit("run from the noroshi repository root")
OUT = REPO.parent / "noroshi-paper" / "analysis" / "independent" / "recompute_output.json"
SECTORS = ["hospital", "federal", "device", "icsma", "ehr"]
DB = REPO / "data" / "raw" / "crawl.sqlite"
BLOBS = REPO / "data" / "raw" / "blobs"
NOT_OUTCOMES = {"cancelled", "in_flight", "outage"}  # sent but never an outcome (store docs / protocol 5)

# public suffix list bundled with tldextract 5.4.0, private suffixes included, never fetched (protocol 4)
_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=True, cache_dir=None)


def reg_domain(host_or_url: str | None) -> str | None:
    if not host_or_url:
        return None
    s = host_or_url if "//" in host_or_url else "//" + host_or_url
    host = (urlsplit(s).hostname or "").rstrip(".").lower()
    if not host:
        return None
    ext = _EXTRACT(host)
    return ext.top_domain_under_public_suffix or host


def host_of(url: str | None) -> str | None:
    if not url:
        return None
    return (urlsplit(url).hostname or "").rstrip(".").lower() or None


# ---------------------------------------------------------------- HTML helpers (stdlib only)

class _Text(HTMLParser):
    """Visible text: text outside title, script and style (protocol 5, 6.3); the first title outside svg/math; the
    og:site_name and description meta values; the first meta refresh tag."""

    SKIP = {"title", "script", "style"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.in_title = False
        self.title_done = False
        self.title_parts: list[str] = []
        self.foreign = 0  # inside svg or math
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ("svg", "math"):
            self.foreign += 1
        if tag in self.SKIP:
            self.skip += 1
            if tag == "title" and not self.title_done and not self.foreign:
                self.in_title = True
        if tag == "meta":
            a = {k.lower(): (v or "") for k, v in attrs}
            prop = (a.get("property") or a.get("name") or "").lower()
            if prop in ("og:site_name", "description") and prop not in self.meta and "content" in a:
                self.meta[prop] = a["content"]

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if tag == "meta":
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("svg", "math") and self.foreign:
            self.foreign -= 1
        if tag in self.SKIP and self.skip:
            self.skip -= 1
            if tag == "title" and self.in_title:
                self.in_title = False
                self.title_done = True

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        if not self.skip:
            self.parts.append(data)


def parse_html(body: bytes) -> dict:
    text = body.decode("utf-8", errors="replace")  # pages are read as UTF-8 whatever they declare
    p = _Text()
    try:
        p.feed(text)
        p.close()
    except Exception:  # malformed markup: keep what was read
        pass
    visible = " ".join("".join(p.parts).split())
    title = " ".join("".join(p.title_parts).split())
    return {"visible": visible, "title": title, "meta": p.meta}


# ---------------------------------------------------------------- refusal / challenge page (protocol 5)

_CHALLENGE = ["attention required! | cloudflare", "errors.edgesuite.net", "errors&#46;edgesuite&#46;net",
              "incapsula incident id", "px-captcha", "pardon our interruption", "spamfirewall"]


def first_title_raw(body: bytes) -> str:
    m = re.search(rb"<title[^>]*>(.*?)</title", body[:1_048_576], re.I | re.S)
    if not m:
        return ""
    return " ".join(html.unescape(m.group(1).decode("utf-8", "replace")).split())


def is_challenge(body: bytes | None, status: int | None) -> bool:
    if not body:
        return False
    if first_title_raw(body).startswith("Just a moment..."):
        return True
    head = body[:32 * 1024].decode("utf-8", "replace").lower()
    if any(s in head for s in _CHALLENGE):
        return True
    if status == 200 and "_incapsula_resource" in head and len(parse_html(body)["visible"]) < 200:
        return True
    return False


def is_plain(body: bytes | None, status: int | None) -> bool:
    """A 401/403 answer that is neither a challenge page nor holds `<script` anywhere (protocol 6.4)."""
    if body is None:
        body = b""
    return not is_challenge(body, status) and b"<script" not in body.lower()


# ---------------------------------------------------------------- security.txt (protocol 6.4)

_HTML_START = re.compile(r"^\s*<(?:!doctype|html|head|body)(?:[\s>/]|$)", re.I)
_EXPIRES = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$")


def is_html_response(content_type: str | None, body: bytes) -> bool:
    if content_type and "html" in content_type.lower():
        return True
    text = body[:4096].decode("utf-8", "replace").lstrip("﻿")
    return bool(_HTML_START.match(text))


def unwrap_pgp(text: str) -> tuple[str, bool]:
    if "-----BEGIN PGP SIGNED MESSAGE-----" not in text:
        return text, False
    lines = re.split(r"\r\n|\n|\r", text)
    out, state = [], 0  # 0 before BEGIN, 1 armor headers, 2 body
    for ln in lines:
        if state == 0:
            if ln.strip() == "-----BEGIN PGP SIGNED MESSAGE-----":
                state = 1
            continue
        if state == 1:
            if ln.strip() == "":
                state = 2
            continue
        if ln.strip() == "-----BEGIN PGP SIGNATURE-----":
            break
        out.append(ln[2:] if ln.startswith("- ") else ln)
    return "\n".join(out), True


def fields_of(text: str) -> list[tuple[str, str]]:
    res = []
    for ln in re.split(r"\r\n|\n|\r", text):
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^([^:\s]+):(.*)$", s)
        if m:
            res.append((m.group(1).lower(), m.group(2).strip()))
    return res


def contact_kind(v: str) -> str:
    """valid_mailto / valid_https / valid_tel, or lenient (bare email, http:), or malformed."""
    low = v.lower()
    ws = bool(re.search(r"\s", v))
    if low.startswith("mailto:"):
        addr = v[7:].split("?", 1)[0]
        if not ws and re.fullmatch(r"[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+", addr):
            return "valid_mailto"
        return "malformed"
    if low.startswith("https://"):
        rest = v[8:]
        hostport = re.split(r"[/?#]", rest, 1)[0].rsplit("@", 1)[-1]
        host = hostport.split(":", 1)[0] if not hostport.startswith("[") else hostport
        if not ws and "." in host.strip(".") and host:
            return "valid_https"
        return "malformed"
    if low.startswith("tel:"):
        return "valid_tel" if re.search(r"\d", v[4:]) else "malformed"
    if low.startswith("http://"):
        return "lenient"
    if re.fullmatch(r"[^@\s:]+@[^@\s]+\.[^@\s]+", v):
        return "lenient"
    return "malformed"


def parse_time(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def expires_ok(values: list[str], sent_at: dt.datetime) -> tuple[bool, str]:
    if not values:
        return False, "missing"
    if len(values) > 1:
        return False, "multiple"
    m = _EXPIRES.match(values[0].strip())
    if not m:
        return False, "malformed"
    y, mo, d, h, mi, se, frac, off = m.groups()
    try:
        if off in ("Z", "z"):
            tz = dt.timezone.utc
        else:
            sign = 1 if off[0] == "+" else -1
            tz = dt.timezone(sign * dt.timedelta(hours=int(off[1:3]), minutes=int(off[4:6])))
        when = dt.datetime(int(y), int(mo), int(d), int(h), int(mi), min(int(se), 59), tzinfo=tz)
    except ValueError:
        return False, "malformed"
    if frac:
        when += dt.timedelta(seconds=float("0" + frac))
    if when <= sent_at:
        return False, "expired"
    return True, "ok"


def classify_200(rec: dict) -> dict:
    """Class of a measured 200 security.txt answer: html / invalid / vwp / valid."""
    body = read_blob(rec["body_sha256"])
    if is_html_response(rec["content_type"], body):
        return {"cls": "html"}
    text = body.decode("utf-8", "replace").lstrip("﻿")
    text, signed = unwrap_pgp(text)
    flds = fields_of(text)
    kinds = [contact_kind(v) for k, v in flds if k == "contact"]
    https = (urlsplit(rec["final_url"] or rec["url"]).scheme.lower() == "https")
    valid_contact = any(k.startswith("valid") for k in kinds)
    exp_ok, exp_state = expires_ok([v for k, v in flds if k == "expires"], parse_time(rec["started_at"]))
    info = {"contacts": kinds, "expires": exp_state, "signed": signed, "https": https}
    if not valid_contact or not https:
        return {"cls": "invalid", **info}
    if not exp_ok:
        return {"cls": "vwp", **info}
    return {"cls": "valid", **info}


_blob_cache: dict[str, bytes] = {}


def read_blob(sha: str | None) -> bytes:
    if not sha:
        return b""
    if sha not in _blob_cache:
        p = BLOBS / sha
        _blob_cache[sha] = p.read_bytes() if p.exists() else b""
    return _blob_cache[sha]


def sectxt_has_valid_contact(body: bytes) -> bool:
    import sectxt  # the protocol's second parser, used only as a cross-check
    try:
        p = sectxt.Parser(body)
    except Exception:
        return False
    tels = [v for v in (p.contact_tels or []) if re.search(r"\d", v[4:])]
    return bool(p.valid_contact_emails or p.valid_contact_urls or tels)


def is_answer(r: dict) -> bool:
    """A measurement: completed, not refused, final status 200 or a 4xx (protocol 6.2)."""
    return (r["error"] is None and not r["blocked"] and r["status"] is not None
            and (r["status"] == 200 or 400 <= r["status"] < 500))


# ---------------------------------------------------------------- load

def load():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    reqs = [dict(r) for r in con.execute("SELECT id, * FROM requests ORDER BY id")]
    done = {(r["domain"], r["pass_no"]): r["status"] for r in con.execute("SELECT * FROM done")}
    browser_sites = {(r["site"], r["pass_no"]) for r in con.execute("SELECT * FROM browser_sites")}
    con.close()
    frames = {s: list(csv.DictReader(open(REPO / "data" / "frames" / f"{s}_domains.csv"))) for s in SECTORS}
    idents = collections.defaultdict(list)
    for ln in open(REPO / "data" / "raw" / "identity.jsonl"):
        if ln.strip():
            d = json.loads(ln)
            idents[d["domain"]].append(d)
    optout_file = REPO / "info" / "optout.txt"
    optout = {reg_domain(x.strip()) for x in optout_file.read_text().splitlines()
              if x.strip() and not x.strip().startswith("#")} if optout_file.exists() else set()
    return reqs, done, browser_sites, frames, idents, optout


# ---------------------------------------------------------------- crawl status of one pass (protocol 5, 6.1, 6.3)

PARKED = ["domain is for sale", "this domain is for sale", "buy this domain", "domain for sale",
          "this domain is parked", "domain parking"]


def pass_status(domain, start_url, recs, rl_by_pass, p):
    """(status, reason) of one listed domain in one pass, from its request records."""
    homes = [r for r in recs if r["purpose"] == "homepage" and r["error"] not in NOT_OUTCOMES]
    # crawl test 3: a homepage request that ended on ww<digits>.<listed domain>, checked before refusal and failure
    ww = re.compile(r"^ww\d+\." + re.escape(domain) + r"$")
    start_host = host_of(homes[0]["url"]) if homes else host_of(start_url)
    for r in homes:
        fh, uh = host_of(r["final_url"]), host_of(r["url"])
        # ended there (any status) after starting on another host; a request that failed there does not count
        if r["error"] is None and fh and ww.match(fh) and fh != uh:
            return "mismatch", "crawl_ww_host"
        # a followed homepage meta refresh that pointed to such a host (a start host of that form is not enough;
        # the other host form only adds or removes `www.`, which never matches ww<digits>)
        if uh and ww.match(uh) and uh != start_host:
            return "mismatch", "crawl_ww_host"
    own_sites = {domain, reg_domain(start_url)}
    for r in homes:
        own_sites.add(reg_domain(r["url"]))
    if homes:
        own_sites.add(reg_domain(homes[-1]["final_url"]))
    own_sites.discard(None)
    last_id = max((r["id"] for r in recs), default=None)
    first_id = min((r["id"] for r in recs), default=None)
    # a 429 from one of the domain's own sites in this pass, before its crawl ended (any listed domain's request)
    for (rid, site) in rl_by_pass.get(p, []):
        if site in own_sites and (last_id is None or rid <= last_id):
            # a 429 before this domain's first record, or one of its own records, or during its crawl
            return "blocked", "rate_limited"
    if any(r["error"] == "rate_limited" for r in homes):
        return "blocked", "rate_limited"
    if not homes:
        return "nohome", "no_homepage_record"
    h = homes[-1]
    if h["blocked"]:
        return "blocked", "refused"
    if h["error"]:
        return "unreachable", h["error"]
    if h["status"] != 200:
        return "unreachable", f"status_{h['status']}"
    page = parse_html(read_blob(h["body_sha256"]))
    hay = (page["title"] + " " + page["visible"]).lower()
    if any(m in hay for m in PARKED):
        return "mismatch", "crawl_parked_marker"
    return "measured", None


# ---------------------------------------------------------------- security.txt per location and pass (protocol 6.4)

def location_pass_result(recs, home_ok):
    """Result of one security.txt location in one pass: a class (valid, vwp, invalid, html, absent, forbidden) or
    'unmeasured' (requested, no result) or None (never requested). Also returns the record used."""
    if not recs:
        return None, None
    result, used = "unmeasured", None
    i = 0
    while i < len(recs):
        r = recs[i]
        if r["error"] in NOT_OUTCOMES:
            i += 1
            continue
        nxt = recs[i + 1] if i + 1 < len(recs) else None
        # identified attempt refused with 401/403 and its browser retry at the same address
        if (r["ua_kind"] == "identified" and r["status"] in (401, 403) and r["blocked"] and r["error"] is None
                and nxt is not None and nxt["ua_kind"] == "browser" and nxt["url"] == r["url"]):
            if (nxt["status"] in (401, 403) and nxt["error"] is None and home_ok
                    and is_plain(read_blob(r["body_sha256"]), r["status"])
                    and is_plain(read_blob(nxt["body_sha256"]), nxt["status"])):
                result, used = "forbidden", nxt
            elif is_answer(nxt):
                result, used = measured_class(nxt), nxt
            i += 2
            continue
        if is_answer(r):
            result, used = measured_class(r), r
        i += 1
    return result, used


def measured_class(r):
    if r["status"] == 200:
        c = classify_200(r)
        r["_cls_info"] = c
        return c["cls"]
    return "absent"  # a 4xx that is not a refusal


POSITIVE = {"valid", "vwp"}


def combine_passes(results):
    """results: [(pass_no, result)] in pass order. Most recent result (a class or forbidden) wins; a failed attempt
    never replaces a result; a valid or valid-with-problems result in either pass stands (protocol 6.2)."""
    got = [(p, r) for p, r in results if r not in (None, "unmeasured")]
    if not got:
        return "unmeasured", None
    latest_p, latest = got[-1]
    if latest in POSITIVE:
        return latest, latest_p
    pos = [(p, r) for p, r in got if r in POSITIVE]
    if pos:
        return pos[-1][1], pos[-1][0]
    return latest, latest_p


def domain_class(wk, root):
    """Combine the two locations (protocol 6.4)."""
    if wk == "valid":
        return "valid", "wellknown"
    if root == "valid":
        return "valid", "root"
    if wk == "vwp":
        return "vwp", "wellknown"
    if root == "vwp":
        return "vwp", "root"
    if "unmeasured" in (wk, root):
        return "unmeasured", None
    for c in ("invalid", "html", "forbidden", "absent"):
        if wk == c:
            return c, "wellknown"
        if root == c:
            return c, "root"
    return "unmeasured", None


# ---------------------------------------------------------------- identity view (protocol 6.3)

def identity_view(rec):
    page = parse_html(read_blob(rec["body_sha256"]))
    t = page["visible"]
    first, rest = t[:2000], t[2000:]
    return (host_of(rec["final_url"]), page["title"][:300], page["meta"].get("og:site_name", "")[:300],
            page["meta"].get("description", "")[:300], first, rest[-800:])


def decide(d):
    a, b, t = d["a"]["site"], d["b"]["site"], (d["tiebreak"] or {}).get("site")
    return a if a == b else t


# ---------------------------------------------------------------- statistics

def cp(k, n):
    if n == 0:
        return None, None
    lo = 0.0 if k == 0 else float(beta.ppf(0.025, k, n - k + 1))
    hi = 1.0 if k == n else float(beta.ppf(0.975, k + 1, n - k))
    return lo, hi


def share(k, n):
    lo, hi = cp(k, n)
    return {"k": k, "n": n, "share": (k / n if n else None), "ci95_low": lo, "ci95_high": hi,
            "share_pct": (round(100 * k / n, 1) if n else None),
            "ci_pct": ([round(100 * lo, 1), round(100 * hi, 1)] if n else None)}


# ---------------------------------------------------------------- main

def main():
    run_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    reqs, done, browser_sites, frames, idents, optout = load()
    by_dp = collections.defaultdict(list)
    for r in reqs:
        by_dp[(r["domain"], r["pass_no"])].append(r)
    # 429 answers per pass: (row id, site that answered). rate_limited rows were stopped before a 429 site.
    rl_by_pass = collections.defaultdict(list)
    for r in reqs:
        if r["status"] == 429:
            rl_by_pass[r["pass_no"]].append((r["id"], reg_domain(r["final_url"] or r["url"])))

    sectors_of = collections.defaultdict(list)
    start_url = {}
    for s in SECTORS:
        for row in frames[s]:
            sectors_of[row["domain"]].append(s)
            start_url.setdefault(row["domain"], row["start_url"])
    all_domains = sorted(sectors_of)

    per_domain = {}
    cross = collections.Counter()
    cross_examples = collections.defaultdict(list)
    for dom in all_domains:
        passes = sorted(p for (d, p) in done if d == dom)
        st_by_pass = {}
        for p in passes:
            st, why = pass_status(dom, start_url[dom], by_dp[(dom, p)], rl_by_pass, p)
            if st == "nohome":
                st, why = done[(dom, p)], "done_mark_no_homepage_record"
            if done[(dom, p)] == "mismatch" and st != "mismatch":
                # the third-party-platform list (src/noroshi/domains.py) is not reimplemented here; the crawler's
                # own end mark is taken for that crawl test
                st, why = "mismatch", "crawl_rule_from_done_mark"
            if st != done[(dom, p)]:
                cross[("pass_status_differs_from_done", p, done[(dom, p)], st)] += 1
                cross_examples[("pass_status", p, done[(dom, p)], st)].append(dom)
            st_by_pass[p] = (st, why)
        rec = {"domain": dom, "sectors": sectors_of[dom], "passes": {p: s for p, (s, _) in st_by_pass.items()}}
        # combine passes (protocol 6.2)
        if reg_domain(dom) in optout or any(s == "optout" for s, _ in st_by_pass.values()):
            final, reason = "optout", None
        elif not passes:
            final, reason = "not_crawled", None
        elif any(s == "measured" for s, _ in st_by_pass.values()):
            final, reason = "measured", None
        else:
            final, reason = st_by_pass[max(passes)]
        measured_passes = [p for p, (s, _) in st_by_pass.items() if s == "measured"]
        # identity check on the homepage of the latest measured pass (protocol 6.3)
        rec["identity"] = None
        if final == "measured":
            mp = max(measured_passes)
            homes = [r for r in by_dp[(dom, mp)] if r["purpose"] == "homepage" and r["error"] not in NOT_OUTCOMES]
            h = homes[-1]
            rec["measured_pass"] = mp
            rec["homepage_final_url"] = h["final_url"]
            fed_only = set(sectors_of[dom]) == {"federal"}
            if not fed_only:
                cands = idents.get(dom, [])
                same = [d for d in cands if d["pass_no"] == mp and d["final_url"] == h["final_url"]]
                how = "same_pass_and_final_url"
                if not same:
                    # a decision made for another pass's homepage holds only if the view shown is the same
                    v_now = identity_view(h)
                    same = []
                    for d in cands:
                        oh = [r for r in by_dp[(dom, d["pass_no"])] if r["purpose"] == "homepage"
                              and r["error"] not in NOT_OUTCOMES]
                        if oh and oh[-1]["final_url"] == d["final_url"]:
                            if (oh[-1]["body_sha256"] == h["body_sha256"] and oh[-1]["final_url"] == h["final_url"]) \
                                    or identity_view(oh[-1]) == v_now:
                                same.append(d)
                    how = "earlier_pass_same_view"
                if same:
                    cross[("identity_matched_by", how)] += 1
                    d = same[-1]
                    site = decide(d)
                    if site != d["site"]:
                        cross[("identity_rule_differs_from_stored_site",)] += 1
                    rec["identity"] = {"site": site, "decision_pass": d["pass_no"], "matched_by": how,
                                       "agreed": d["a"]["site"] == d["b"]["site"]}
                    if site == "other":
                        final, reason = "mismatch", "identity_other"
                    elif site == "unclear":
                        final, reason = "mismatch", "identity_unclear"
                else:
                    rec["identity"] = {"site": None, "matched_by": "no_decision_for_this_view"}
                    cross[("measured_without_identity_decision",)] += 1
                    cross_examples[("no_identity",)].append(dom)
        rec["status"] = final
        rec["status_reason"] = reason
        # security.txt: per location per pass, every pass's records count (protocol 6.2)
        loc_results = {"wellknown": [], "root": []}
        loc_detail = {}
        for p in passes:
            recs = by_dp[(dom, p)]
            homes = [r for r in recs if r["purpose"] == "homepage" and r["error"] not in NOT_OUTCOMES]
            home_ok = bool(homes) and homes[-1]["status"] == 200 and not homes[-1]["blocked"] \
                and homes[-1]["error"] is None
            for loc, purpose in (("wellknown", "sectxt_wellknown"), ("root", "sectxt_root")):
                lrecs = [r for r in recs if r["purpose"] == purpose]
                res, used = location_pass_result(lrecs, home_ok)
                loc_results[loc].append((p, res))
                if used is not None and res in ("valid", "vwp", "invalid", "html", "absent", "forbidden"):
                    loc_detail[(loc, p)] = {"url": used["url"], "final_url": used["final_url"], "status": used["status"],
                                            "content_type": used["content_type"], "info": used.get("_cls_info")}
        combined = {}
        for loc in ("wellknown", "root"):
            combined[loc], _p = combine_passes(loc_results[loc])
        cls, chosen = domain_class(combined["wellknown"], combined["root"])
        rec["sectxt_by_pass"] = {loc: {str(p): r for p, r in loc_results[loc]} for loc in loc_results}
        rec["sectxt_location"] = combined
        rec["sectxt_class"] = cls
        rec["sectxt_chosen"] = chosen
        # passes disagree (flag only)
        rec["sectxt_detail"] = {f"{loc}:{p}": v for (loc, p), v in loc_detail.items()}
        # second parser (sectxt 0.9.8), compared on one point only: does the chosen file have a valid contact
        if final == "measured" and cls in ("valid", "vwp", "invalid"):
            chosen_p = combine_passes(loc_results[chosen])[1]
            used = [r for r in by_dp[(dom, chosen_p)] if r["purpose"] == f"sectxt_{chosen}" and is_answer(r)]
            if used:
                mine = any(k.startswith("valid") for k in classify_200(used[-1]).get("contacts", []))
                theirs = sectxt_has_valid_contact(read_blob(used[-1]["body_sha256"]))
                rec["second_parser_agrees_on_contact"] = (mine == theirs)
                cross[("second_parser_contact", "agree" if mine == theirs else "disagree")] += 1
                if mine != theirs:
                    cross_examples[("second_parser_disagree",)].append(dom)
        per_domain[dom] = rec
    # refusal flag of the store against the refusal rule as far as it can be checked from status and body
    # (the `cf-mitigated` / `x-amzn-waf-action` headers are not stored, so a header-only refusal cannot be checked)
    for r in reqs:
        if r["error"] is not None or r["status"] is None or r["status"] == 429:
            continue
        mine = r["status"] in (401, 403, 406) or is_challenge(read_blob(r["body_sha256"]), r["status"])
        if mine and not r["blocked"]:
            cross[("refusal_rule_says_refused_but_store_not_blocked", r["purpose"])] += 1
            cross_examples[("refused_not_blocked",)].append(f"{r['domain']} {r['pass_no']} {r['purpose']} {r['url']}")
        elif r["blocked"] and not mine:
            cross[("store_blocked_without_status_or_body_marker(header?)", r["purpose"])] += 1

    # ------------------------------------------------------------ tables
    def flow_and_shares(domains):
        st = collections.Counter(per_domain[d]["status"] for d in domains)
        mm = collections.Counter(per_domain[d]["status_reason"] for d in domains
                                 if per_domain[d]["status"] == "mismatch")
        bl = collections.Counter(per_domain[d]["status_reason"] for d in domains
                                 if per_domain[d]["status"] == "blocked")
        meas = [d for d in domains if per_domain[d]["status"] == "measured"]
        known = [d for d in meas if per_domain[d]["sectxt_class"] != "unmeasured"]
        k_main = sum(per_domain[d]["sectxt_class"] in POSITIVE for d in known)
        k_strict = sum(per_domain[d]["sectxt_class"] == "valid" for d in known)
        cls = collections.Counter(per_domain[d]["sectxt_class"] for d in meas)
        return {
            "flow": {"listed": len(domains), "optout": st["optout"], "not_crawled": st["not_crawled"],
                     "measured": st["measured"], "unreachable": st["unreachable"], "blocked": st["blocked"],
                     "mismatch": st["mismatch"],
                     "mismatch_by_reason": {"crawl_rule": sum(v for k, v in mm.items() if k and k.startswith("crawl")),
                                            "identity_other": mm["identity_other"],
                                            "identity_unclear": mm["identity_unclear"]},
                     "mismatch_reason_detail": dict(mm),
                     "blocked_by_reason": dict(bl)},
            "sectxt_class_counts_among_measured": dict(cls),
            "main": share(k_main, len(known)),
            "strict": share(k_strict, len(known)),
        }

    tables = {}
    for s in SECTORS:
        tables[s] = flow_and_shares([row["domain"] for row in frames[s]])
    tables["all_distinct"] = flow_and_shares(all_domains)

    out = {
        "run_at_utc": run_at,
        "repository": str(REPO),
        "inputs": {"crawl_sqlite_rows": len(reqs), "done_rows": len(done),
                   "identity_rows": sum(len(v) for v in idents.values()), "optout_entries": sorted(x for x in optout if x)},
        "tables": tables,
        "cross_checks": {"|".join(map(str, k)): v for k, v in cross.items()},
        "cross_check_examples": {"|".join(map(str, k)): v[:50] for k, v in cross_examples.items()},
        "per_domain": per_domain,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1, default=str))
    # short console summary
    for s, t in tables.items():
        f = t["flow"]
        print(f"{s:13s} listed {f['listed']:5d} meas {f['measured']:5d} unr {f['unreachable']:4d} blk {f['blocked']:4d} "
              f"mis {f['mismatch']:3d} | main {t['main']['k']}/{t['main']['n']} {t['main']['share_pct']}% "
              f"{t['main']['ci_pct']} | strict {t['strict']['k']}/{t['strict']['n']} {t['strict']['share_pct']}%")
    print(json.dumps(out["cross_checks"], indent=1))
    print("wrote", OUT)


if __name__ == "__main__":
    main()
