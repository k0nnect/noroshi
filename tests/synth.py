"""A small synthetic crawl store built with real Store.record calls and real bodies."""
import hashlib
import json
from datetime import datetime, timezone

import pandas as pd

from noroshi.registries import Registries
from noroshi.store import FetchRecord, Store  # noqa: F401 (tests build records through this module)

NOW = datetime(2026, 12, 1, tzinfo=timezone.utc)
GOOD = b"Contact: mailto:sec@{d}\nExpires: 2030-01-01T00:00:00Z\n"
SECRET = b"Contact: mailto:secret-contact@hidden.example\nContact: tel:+1-555-0100\nExpires: 2030-01-01T00:00:00Z\n"
EXPIRED = b"Contact: mailto:sec@expired.org\nExpires: 2020-01-01T00:00:00Z\n"
LENIENT = b"Contact: http://lenient.org/report\nContact: mailto:nobody\n"
CMS_OK = b"location-name: A\nmrf-url: https://x.org/f.json\n"
CMS_BAD = b"location-name: A\n"
HOME = (b"<html><title>Site</title><body><p>welcome</p><p>"  # 200 visible characters or more (post-pilot Y4)
        + b"Our hospital serves the people of the valley, day and night, with care close to home. " * 3
        + b"</p></body></html>")


def rec(store, domain, purpose, body=None, *, status=200, ctype="text/plain", pass_no=1, url=None, final=None,
        ua="identified", error=None, blocked=False, sector="hospital", truncated=False, chain=(), error_url=None,
        error_detail=None):
    url = url or f"https://{domain}/{purpose}"
    from noroshi.fetch import url_site

    store.record(FetchRecord(domain=domain, sector=sector, host=domain, url=url, purpose=purpose, pass_no=pass_no,
                             ua_kind=ua, started_at="2026-11-20T00:00:00Z", status=status,
                             final_url=final or url, redirect_chain=list(chain), content_type=ctype, body_sha256=None,
                             body_len=0, truncated=truncated, error=error, blocked=blocked, site=url_site(url),
                             error_url=error_url, error_detail=error_detail), body)


def core(store, d, wk=None, root=None, cms=None, home=HOME, pass_no=1, wk_status=None, **kw):
    """Homepage, robots and the files; wk/root/cms are bodies (None means a 404)."""
    rec(store, d, "homepage", home, ctype="text/html", url=f"https://{d}/", pass_no=pass_no, **kw)
    rec(store, d, "robots", b"User-agent: *\n", url=f"https://{d}/robots.txt", pass_no=pass_no, **kw)
    for purpose, path, body in (("sectxt_wellknown", "/.well-known/security.txt", wk),
                                ("sectxt_root", "/security.txt", root), ("cmshpt", "/cms-hpt.txt", cms)):
        if purpose == "sectxt_root" and wk is not None and wk != LENIENT and root is None:
            continue  # the crawler skips the root request when the well-known file parsed
        if purpose == "cmshpt" and cms is False:
            continue
        rec(store, d, purpose, body, status=200 if body else 404, url=f"https://{d}{path}",
            ctype="text/plain" if body else "text/html", pass_no=pass_no, **kw)


def build(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    rows = []

    def add(d, sector="hospital", units=1, beds=50, tokens=""):
        rows.append({"sector": sector, "domain": d, "start_url": f"https://{d}/", "n_units": str(units),
                     "beds_sum": str(beds) if beds else "", "name_tokens": tokens})

    def good(d):
        return GOOD.replace(b"{d}", d.encode())

    add("good.org", units=3, beds=300)
    core(store, "good.org", wk=good("good.org"), cms=CMS_OK)
    store.mark_done("good.org", 1, "measured")
    add("expired.org")
    core(store, "expired.org", wk=EXPIRED, cms=CMS_BAD)
    store.mark_done("expired.org", 1, "measured")
    add("root.org")
    core(store, "root.org", wk=None, root=good("root.org"), cms=CMS_OK)
    store.mark_done("root.org", 1, "measured")
    add("secret.org")
    core(store, "secret.org", wk=SECRET, cms=None)
    store.mark_done("secret.org", 1, "measured")
    add("blocked.org")
    rec(store, "blocked.org", "homepage", b"denied", status=403, ctype="text/html", blocked=True)
    store.mark_done("blocked.org", 1, "blocked")
    add("down.org")
    rec(store, "down.org", "homepage", None, status=None, error="timeout")
    store.mark_done("down.org", 1, "unreachable")
    add("optout.org")
    store.mark_done("optout.org", 1, "optout")
    add("notcrawled.org")
    add("policyonly.org")
    core(store, "policyonly.org", wk=None, cms=None)
    page = (b"<p>Report a security vulnerability to us. Email x@policyonly.org</p>"
            + b"<p>We thank every researcher who writes to us in good faith about our systems.</p>" * 3)
    rec(store, "policyonly.org", "candidate", page, ctype="text/html", url="https://policyonly.org/vdp")
    store.mark_done("policyonly.org", 1, "measured")
    add("cnaonly.com")
    core(store, "cnaonly.com", wk=None, cms=None)
    store.mark_done("cnaonly.com", 1, "measured")
    add("none.org")
    core(store, "none.org", wk=None, cms=None)
    store.mark_done("none.org", 1, "measured")
    add("unmeasured.org")  # measured, but the well-known request never completed
    rec(store, "unmeasured.org", "homepage", HOME, ctype="text/html", url="https://unmeasured.org/")
    rec(store, "unmeasured.org", "sectxt_wellknown", None, status=None, error="timeout")
    store.mark_done("unmeasured.org", 1, "measured")
    add("pass2.org")  # pass 1 timed out on the file, pass 2 found a valid one
    core(store, "pass2.org", wk=None, cms=None)
    rec(store, "pass2.org", "sectxt_wellknown", None, status=None, error="timeout", url="https://pass2.org/x")
    store.mark_done("pass2.org", 1, "measured")
    # pass 2: the site refused the identified signature, so its requests then used the browser signature
    rec(store, "pass2.org", "homepage", b"no", status=403, ctype="text/html", pass_no=2, blocked=True,
        url="https://pass2.org/")
    rec(store, "pass2.org", "sectxt_wellknown", good("pass2.org"), pass_no=2, ua="browser",
        url="https://pass2.org/.well-known/security.txt")
    rec(store, "pass2.org", "homepage", HOME, ctype="text/html", pass_no=2, ua="browser", url="https://pass2.org/")
    store.mark_done("pass2.org", 2, "measured")
    add("late.org")  # blocked in pass 1, measured in pass 2
    rec(store, "late.org", "homepage", b"no", status=403, ctype="text/html", blocked=True)
    store.mark_done("late.org", 1, "blocked")
    rec(store, "late.org", "homepage", b"no", status=403, ctype="text/html", blocked=True, pass_no=2,
        url="https://late.org/")  # refused again, then retried with the browser signature
    core(store, "late.org", wk=good("late.org"), cms=None, pass_no=2, ua="browser")
    store.mark_done("late.org", 2, "measured")
    add("lenient.org")
    core(store, "lenient.org", wk=LENIENT, cms=None)
    store.mark_done("lenient.org", 1, "measured")
    # platform links: one for this programme (path has the label), one for someone else's
    add("plat.org", tokens="plat")
    links = (b'<a href="https://hackerone.com/plat_program">Report</a>'
             b'<a href="https://bugcrowd.com/otherco">Sponsors</a>'
             b'<a href="https://intigriti.com/programs/x">Report a vulnerability</a>')
    core(store, "plat.org", wk=None, cms=None, home=HOME.replace(b"</body>", links + b"</body>"))
    store.mark_done("plat.org", 1, "measured")
    # sectors besides hospital
    add("fed.gov", sector="federal", beds=0)
    core(store, "fed.gov", wk=good("fed.gov"), sector="federal")
    store.mark_done("fed.gov", 1, "measured")
    add("vendor.com", sector="device", beds=0)
    core(store, "vendor.com", wk=None, sector="device")
    store.mark_done("vendor.com", 1, "measured")
    frame = pd.DataFrame(rows)
    judg = tmp_path / "judgements.jsonl"
    base = {"sector": "hospital", "prefilter": True, "a": None, "b": None, "tiebreak": None, "agreed": True,
            "safe_harbor_agreed": True, "skipped": None}
    lines = [
        {**base, "domain": "policyonly.org", "url": "https://policyonly.org/vdp", "body_sha256": hashlib.sha256(page).hexdigest(),
         "final": {"invites_reports": True, "contact_method": True, "safe_harbor": True}},
        {**base, "domain": "none.org", "url": "https://none.org/privacy", "body_sha256": "2",
         "final": {"invites_reports": True, "contact_method": False, "safe_harbor": False}},
        {**base, "domain": "none.org", "url": "https://none.org/a.pdf", "body_sha256": "3", "final": None,
         "prefilter": None, "skipped": "pdf_unreadable"},
        {**base, "domain": "blocked.org", "url": "https://blocked.org/x", "body_sha256": "4",
         "final": {"invites_reports": True, "contact_method": True, "safe_harbor": False}},
    ]
    judg.write_text("".join(json.dumps(x) + "\n" for x in lines))
    reg = Registries(platforms={"hackerone": {"hosted.org"}}, diodb=set(), cna={"cnaonly.com"})
    return store, frame, judg, reg
