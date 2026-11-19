"""End to end: the real crawl command writes the store (two passes, against local HTTPS sites), then judge (with a
faked model), derive, purge and analyze run on it through the command line. No test before this one crossed the
seam between the crawler's records and the analysis."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from werkzeug.wrappers import Response

from crawled import Sites
from frozen import freeze
from noroshi import cli, config, identity, judge
from noroshi.discover import PROBE_PATHS

HEAD = "sector,domain,start_url,n_units,beds_sum,name_tokens\n"
PAGE = ("<title>{t}</title><p>{t} cares for the people of the valley, day and night.</p>"
        + "<p>Our doctors, nurses and staff serve every family in the county, close to home.</p>" * 2 + "{extra}")
POLICY = ("<h1>Vulnerability Disclosure Policy</h1><p>To report a security vulnerability in our systems, email "
          "secret-contact@{d}.</p>" + "<p>We thank every researcher who writes to us in good faith.</p>" * 3)
ROBOTS_OK = "User-agent: *\nDisallow: /admin\n"
HOSPITAL = ["e2e-valid.org", "e2e-robotsdeny.org", "e2e-robots5xx.org", "e2e-robotsflaky.org", "e2e-candfail.org",
            "e2e-flaky.org", "e2e-blocked.org", "e2e-rate.org", "e2e-alias.org", "e2e-hub.org", "e2e-optlate.org"]
# post-pilot Y7: a file refused to both signatures, a site that answers every path with one page, and a homepage that
# is now someone else's business
EHR = ["e2e-forbidden.com", "e2e-catchall.com", "e2e-taken.com"]
PILOT = Path(__file__).parent / "fixtures" / "pilot"
TAKEN = ("<title>Smash Daily - Badminton News</title><p>Badminton tips, rackets and shuttlecock reviews.</p>"
         + "<p>Our editors test every racket and string so you can play better badminton at your club.</p>" * 3)


def sectxt(sites, d, policy=None):
    text = f"Contact: mailto:secret-contact@{d}\nExpires: 2030-01-01T00:00:00Z\n"
    return text + (f"Policy: {sites.url(d, policy)}\n" if policy else "")


def by_phase(sites, first, later):
    """A handler answering `first` (status, body) in pass 1 and `later` afterwards."""
    def handler(request):
        status, body = first if sites.phase == 1 else later
        return Response(body, status=status, content_type="text/plain" if request.path.endswith(".txt") else "text/html")
    return handler


def build_sites(sites):
    def site(d, extra="", robots=ROBOTS_OK, home=None):
        s = sites.add(d)
        s.expect_request("/").respond_with_data(home or PAGE.format(t=d, extra=extra), content_type="text/html")
        if robots is not None:
            s.expect_request("/robots.txt").respond_with_data(robots, content_type="text/plain")
        return s

    s = site("e2e-valid.org")
    s.expect_request("/.well-known/security.txt").respond_with_data(
        sectxt(sites, "e2e-valid.org", "/security-policy"), content_type="text/plain")
    s.expect_request("/security-policy").respond_with_data(POLICY.format(d="e2e-valid.org"), content_type="text/html")
    s.expect_request("/cms-hpt.txt").respond_with_data("location-name: Valid\nmrf-url: https://e2e-valid.org/m.json\n",
                                                       content_type="text/plain")
    s = site("e2e-robotsdeny.org", "<a href='/responsible-disclosure'>Responsible Disclosure</a>",
             robots="User-agent: *\nDisallow: /\n")
    s.expect_request("/responsible-disclosure").respond_with_data(POLICY.format(d="e2e-robotsdeny.org"))
    s = site("e2e-robots5xx.org", "<a href='/responsible-disclosure'>Responsible Disclosure</a>", robots=None)
    s.expect_request("/robots.txt").respond_with_data("busy", status=503)
    s.expect_request("/responsible-disclosure").respond_with_data(POLICY.format(d="e2e-robots5xx.org"))
    s = site("e2e-robotsflaky.org", robots=None)
    s.expect_request("/robots.txt").respond_with_handler(by_phase(sites, (503, "busy"), (200, ROBOTS_OK)))
    s = site("e2e-candfail.org", "<a href='/legal/vdp'>Vulnerability Disclosure Policy</a>")
    s.expect_request("/legal/vdp").respond_with_data("Service Unavailable", status=503)
    s = site("e2e-flaky.org")
    s.expect_request("/.well-known/security.txt").respond_with_handler(
        by_phase(sites, (200, sectxt(sites, "e2e-flaky.org")), (404, "<html>not found</html>")))
    s.expect_request("/cms-hpt.txt").respond_with_handler(
        by_phase(sites, (200, "location-name: F\nmrf-url: https://e2e-flaky.org/m.json\n"), (404, "<html>no</html>")))
    s.expect_request("/security").respond_with_handler(by_phase(sites, (503, "busy"), (404, "<html>no</html>")))
    sites.add("e2e-blocked.org").expect_request("/").respond_with_data("<h1>Access Denied</h1>", status=403)
    site("e2e-rate.org", robots=None).expect_request("/robots.txt").respond_with_data("slow down", status=429)
    site("e2e-hub.org")
    sites.add("e2e-alias.org").expect_request("/").respond_with_data(
        "", status=301, headers={"Location": sites.url("e2e-hub.org")})
    site("e2e-optlate.org").expect_request("/.well-known/security.txt").respond_with_data(
        sectxt(sites, "e2e-optlate.org"), content_type="text/plain")
    site("e2e-forbidden.com").expect_request("/.well-known/security.txt").respond_with_data(
        (PILOT / "insulet_wellknown_403.txt").read_bytes(), status=403, content_type="text/plain")  # to both signatures
    s = site("e2e-catchall.com")
    shell = (PILOT / "grants_probe_shell.html").read_text()
    for path in PROBE_PATHS:  # the same page at every conventional path, each copy naming its own path
        s.expect_request(path).respond_with_data(shell + f"<!-- {path} -->", content_type="text/html")
    site("e2e-taken.com", home=TAKEN)


def fake_ask(prompt, model=None, system=None, purpose=None, **kw):
    text = prompt.split("<<<", 1)[1].rsplit(">>>", 1)[0]
    return SimpleNamespace(text=json.dumps({"invites_reports": "report a security vulnerability" in text.lower(),
                                            "contact_method": "secret-contact@" in text, "safe_harbor": False}))


def fake_identity(prompt, model=None, system=None, purpose=None, **kw):
    site = "other" if "badminton" in prompt.lower() else "own"
    return SimpleNamespace(text=json.dumps({"site": site, "reason": "what the page is about"}))


@pytest.fixture
def run(tmp_path, monkeypatch, local_dns):
    monkeypatch.setenv("NOROSHI_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "MIN_DELAY_SECONDS", 0.0)  # the gap is not what this test is about
    sites = Sites(tmp_path / "tls", HOSPITAL + EHR, local_dns)
    monkeypatch.setenv("SSL_CERT_FILE", str(sites.cert))
    build_sites(sites)
    p = config.paths()
    for d in (p.frames, p.info, p.registries):
        d.mkdir(parents=True, exist_ok=True)
    (p.frames / "hospital_domains.csv").write_text(
        HEAD + "".join(f"hospital,{d},{sites.url(d)},1,100,\n" for d in HOSPITAL))
    (p.frames / "device_domains.csv").write_text(HEAD + f"device,e2e-valid.org,{sites.url('e2e-valid.org')},1,,\n")
    for name in ("federal", "icsma"):
        (p.frames / f"{name}_domains.csv").write_text(HEAD)
    (p.frames / "ehr_domains.csv").write_text(HEAD + "".join(f"ehr,{d},{sites.url(d)},1,,\n" for d in EHR))
    p.passes.mkdir(parents=True, exist_ok=True)
    for which in ("memory_a", "memory_b"):  # the EHR developers' names, as the resolution passes give them
        (p.passes / f"ehr_{which}.jsonl").write_text("".join(json.dumps(
            {"key": d, "name": f"{d.split('.')[0]} Software Inc", "model": "m", "domain": d}) + "\n" for d in EHR))
    (p.frames / "hospitals.csv").write_text(
        "hifld_id,name,state,type,owner,beds,rural,domain,exclusion\n"
        + "".join(f"{i},H{i},TX,GENERAL ACUTE CARE,NON-PROFIT,100,True,{d},\n" for i, d in enumerate(HOSPITAL)))
    (p.registries / "CNAsList.json").write_text(json.dumps([{"contact": [{"email": [{"emailAddr": "psirt@e2e-hub.org"}]}]}]))
    (p.registries / "program-list.json").write_text("[]")
    for name in ("hackerone", "bugcrowd", "intigriti", "yeswehack"):
        (p.registries / f"{name}_data.json").write_text("[]")
    (p.info / "LIVE").write_text("https://info.test/study 2026-11-16T00:00:00Z\n")  # never requested
    freeze(tmp_path)
    orig = judge.run_judging
    monkeypatch.setattr(judge, "run_judging", lambda store, out, **kw: orig(store, out, ask=fake_ask, **kw))
    decide = identity.run_identity
    monkeypatch.setattr(identity, "run_identity", lambda *a, **kw: decide(*a, ask=fake_identity, **kw))
    yield sites, p
    sites.stop()


def test_crawl_judge_derive_purge_and_analyze_on_a_store_the_crawler_wrote(run, capsys):
    sites, p = run
    cli.main(["crawl", "--sector", "all", "--pass", "1"])
    db = sqlite3.connect(p.raw / "crawl.sqlite")
    three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db:
        db.execute("UPDATE requests SET started_at = ? WHERE pass_no = 1", (three_days_ago,))  # pass 2 may run
    sites.phase = 2
    cli.main(["crawl", "--sector", "all", "--pass", "2"])
    retried = {r[0] for r in db.execute("SELECT DISTINCT domain FROM requests WHERE pass_no = 2")}
    assert retried == {"e2e-robots5xx.org", "e2e-robotsflaky.org", "e2e-candfail.org", "e2e-flaky.org",
                       "e2e-blocked.org", "e2e-rate.org"}
    # every request a server received is in the store: one row per attempt, plus its redirect hops
    hops = sum(1 + len(json.loads(c)) for (c,) in db.execute("SELECT redirect_chain FROM requests WHERE NOT reused"))
    assert hops == sum(len(s.log) for s in sites.servers.values())
    cli.main(["judge"])
    cli.main(["derive"])
    with pytest.raises(SystemExit) as stop:  # before the identity check has run
        cli.main(["analyze"])
    assert "12 measured domains have no identity decision" in str(stop.value)
    assert not (p.results / "numbers.json").exists()
    cli.main(["identity"])
    (p.info / "optout.txt").write_text("https://www.e2e-optlate.org/\n")  # an opt-out after the crawl
    cli.main(["derive"])
    with pytest.raises(SystemExit) as stop:  # its records are still stored: not before the purge
        cli.main(["analyze"])
    assert "noroshi purge e2e-optlate.org" in str(stop.value)
    capsys.readouterr()

    d = pd.read_csv(p.derived / "domains.csv", dtype=str, keep_default_na=False)
    h = d[d.sector == "hospital"].set_index("domain")
    v = h.loc["e2e-valid.org"]
    assert (v.status, v.main, v.strict, v.policy_page, v.broad, v.cmshpt_class, v.ua_kind) == \
        ("measured", "True", "True", "yes", "True", "present_valid", "identified")
    dev = d[d.sector == "device"].iloc[0]
    assert dev.domain == "e2e-valid.org" and (dev.status, dev.main, dev.policy_page, dev.cmshpt_class) == \
        ("measured", "True", "yes", "")
    # the reviewer's rows: could not look is never "no policy page"
    for dom, look in (("e2e-robotsdeny.org", "robots_disallowed;candidate_disallowed"),
                      ("e2e-robots5xx.org", "robots_unreadable"), ("e2e-candfail.org", "request_failed")):
        assert (h.loc[dom, "policy_page"], h.loc[dom, "policy_look"], h.loc[dom, "broad"]) == ("unmeasured", look, "")
    assert (h.loc["e2e-robotsflaky.org", "policy_page"], h.loc["e2e-robotsflaky.org", "policy_look"]) == ("no", "")
    f = h.loc["e2e-flaky.org"]  # valid in pass 1, 404 in pass 2
    assert (f.sectxt_class, f.main, f.cmshpt_class, f.passes_disagree, f.policy_page) == \
        ("valid", "True", "present_valid", "True", "no")
    assert (h.loc["e2e-blocked.org", "status"], h.loc["e2e-blocked.org", "blocked_reason"]) == ("blocked", "refused")
    assert (h.loc["e2e-rate.org", "status"], h.loc["e2e-rate.org", "blocked_reason"]) == ("blocked", "rate_limited")
    a = h.loc["e2e-alias.org"]  # redirects to the hub, whose CNA listing it now gets
    assert (a.final_domain, a.cna, a.listing_matched_on, a.listing, a.broad) == \
        ("e2e-hub.org", "True", "final", "True", "True")
    assert h.loc["e2e-hub.org", "listing_matched_on"] == "listed"
    # each sent its own homepage; the ten other requests were sent once, by whichever went first, and copied
    sent = sorted([int(a.requests_sent), int(h.loc["e2e-hub.org", "requests_sent"])])
    assert sent == [1, 11]
    assert (h.loc["e2e-optlate.org", "status"], h.loc["e2e-optlate.org", "main"]) == ("optout", "")
    e = d[d.sector == "ehr"].set_index("domain")
    f = e.loc["e2e-forbidden.com"]  # refused to both signatures with a plain denial: no file
    assert (f.status, f.sectxt_class, f.file_forbidden, f.main, f.strict, f.site_switched, f.identity) == \
        ("measured", "forbidden", "True", "False", "False", "False", "own")
    c = e.loc["e2e-catchall.com"]  # its paths answer with one page: no policy page from them, and the look is complete
    assert (c.status, c.policy_page, c.policy_look, c.policy_pages_unjudged, c.broad) == ("measured", "no", "", "0",
                                                                                          "False")
    judged = [json.loads(x) for x in (p.raw / "judgements.jsonl").read_text().splitlines()]
    assert {r["skipped"] for r in judged if r["domain"] == "e2e-catchall.com"} == {"catch_all"}
    t = e.loc["e2e-taken.com"]  # the identity check: someone else's site, so could not measure
    assert (t.status, t.mismatch_reason, t.identity, t.identity_agreed) == ("mismatch", "identity_other", "other",
                                                                            "True")
    assert (t.main, t.strict, t.policy_page, t.broad, t.listing, t.sectxt_class) == ("", "", "unmeasured", "", "",
                                                                                     "unmeasured")
    first = pd.read_csv(p.derived / "wayback_first_seen.csv")
    assert sorted(first.domain) == ["e2e-flaky.org", "e2e-valid.org"]  # e2e-valid.org once, though in two sectors
    assert set(first.path) == {"/.well-known/security.txt"}

    with sqlite3.connect(p.raw / "crawl.sqlite") as con:
        assert con.execute("SELECT COUNT(*) FROM requests WHERE domain = 'e2e-optlate.org'").fetchone()[0] > 0
    cli.main(["purge", "e2e-optlate.org"])
    assert "purged e2e-optlate.org" in capsys.readouterr().out
    with sqlite3.connect(p.raw / "crawl.sqlite") as con:
        assert con.execute("SELECT COUNT(*) FROM requests WHERE domain = 'e2e-optlate.org'").fetchone()[0] == 0
    assert "e2e-optlate.org" not in (p.raw / "judgements.jsonl").read_text()

    cli.main(["derive"])
    cli.main(["analyze"])
    n = json.loads((p.results / "numbers.json").read_text())
    assert n["flow.hospital.frame"] == 11 and n["flow.all.frame"] == 14 and n["flow.all.multi_sector"] == 1
    assert (n["flow.ehr.measured"], n["flow.ehr.mismatch_identity_other"], n["flow.ehr.file_forbidden"]) == (2, 1, 1)
    assert n["agreement.identity.site.n"] == 11 and n["agreement.judge.invites_reports.n"] >= 1
    assert (p.results / "tables" / "tableS2_agreement.csv").exists()
    assert n["flow.hospital.blocked"] == 2 and n["flow.hospital.blocked_rate_limited"] == 1
    assert n["flow.hospital.optout"] == 1 and n["flow.hospital.measured"] == 8
    assert n["flow.hospital.look_robots_unreadable"] == 1 and n["flow.hospital.look_robots_disallowed"] == 1
    assert n["flow.hospital.look_candidate_disallowed"] == 1 and n["flow.hospital.look_request_failed"] == 1
    assert n["flow.hospital.policy_unjudged"] == 3
    with sqlite3.connect(p.raw / "crawl.sqlite") as con:
        assert n["flow.all.requests_sent"] == con.execute("SELECT COUNT(*) FROM requests WHERE NOT reused").fetchone()[0]
    # measured hospital domains: valid, flaky, alias and hub have a main or broad outcome; the three that could
    # not look leave the policy denominator
    assert n["hospital.domain.main.k"] == 2 and n["hospital.domain.policy.n"] == 5 and n["hospital.domain.policy.k"] == 1
    assert n["hospital.domain.broad.k"] == 4
    assert (p.results / "tables" / "table1_flow.md").exists() and (p.results / "figures" / "figure1_shares.png").exists()


def test_requests_sent_to_purged_domains_stay_in_the_flow_table(run):
    """Closing fixes, C4: after a purge, the flow table still accounts for every request the servers saw."""
    sites, p = run
    cli.main(["crawl", "--sector", "all", "--pass", "1"])
    (p.info / "optout.txt").write_text("e2e-optlate.org\ne2e-rate.org\n")
    for name in ("e2e-optlate.org", "e2e-rate.org"):
        cli.main(["purge", name])
    cli.main(["identity"])
    assert len((p.raw / "purge.log").read_text().splitlines()) == 2
    cli.main(["derive"])
    cli.main(["analyze"])
    n = json.loads((p.results / "numbers.json").read_text())
    with sqlite3.connect(p.raw / "crawl.sqlite") as con:  # a redirect hop is followed, not counted as a request
        hops = sum(len(json.loads(c)) for (c,) in con.execute("SELECT redirect_chain FROM requests WHERE NOT reused"))
    seen = sum(len(s.log) for s in sites.servers.values())
    assert n["flow.all.requests_sent_purged"] == len(sites.log("e2e-optlate.org")) + len(sites.log("e2e-rate.org"))
    assert n["flow.all.requests_sent"] + n["flow.all.requests_sent_purged"] == seen - hops
