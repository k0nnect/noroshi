"""B3, B10, B16: "could not look" is never "no policy page".

Each store here is written by the real crawler against local servers; derive then reads it."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

from frozen import freeze
from noroshi import config
from noroshi.crawl import crawl_domain, crawl_sector, needs_retry
from noroshi.fetch import PoliteFetcher
from noroshi.judge import run_judging
from noroshi.outcomes import derive_domains
from noroshi.registries import Registries
from noroshi.store import Store

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
HOME = ("<title>Andalusia Health</title><p>welcome</p><p>"  # 200 visible characters or more (post-pilot Y4)
        + "Andalusia Health serves the people of the valley, day and night, with care close to home. " * 3 + "</p>")
POLICY = (b"<h1>Vulnerability Disclosure Policy</h1><p>To report a security vulnerability email "
          b"security@a.org.</p>")
POLICY += (b"<p>" + b"We thank every researcher who writes to us in good faith about our systems. " * 3
           + b"</p>")  # post-pilot Y2: a page needs 200 visible characters to be judged
NO_ROBOTS = "User-agent: *\nDisallow: /admin\n"


class FakeTime:
    def __init__(self):
        self.now = 1000.0

    def clock(self):
        return self.now

    async def sleep(self, secs):
        self.now += secs


@pytest.fixture
def servers():
    made = []

    def make() -> HTTPServer:
        s = HTTPServer(host="127.0.0.1", port=0)
        s.no_handler_status_code = 404
        s.start()
        s.host = f"s{len(made) + 1}.test"
        made.append(s)
        return s

    yield make
    for s in made:
        if s.is_running():
            s.stop()


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "db" / "c.sqlite") as s:
        yield s


@pytest.fixture
def live(tmp_path):
    p = tmp_path / "LIVE"
    p.write_text("go")
    return p


def fetcher_for(store, live, optout=frozenset()):
    ft = FakeTime()
    return PoliteFetcher(store, optout, live, clock=ft.clock, sleep=ft.sleep)


def site(servers, home=HOME, robots=NO_ROBOTS, pages=None):
    """A local site: its homepage, robots.txt (a string, or a (status, body) pair) and other pages
    (path -> body, or (status, body))."""
    s = servers()
    s.expect_request("/").respond_with_data(home, content_type="text/html")
    if robots is not None:
        status, body = robots if isinstance(robots, tuple) else (200, robots)
        s.expect_request("/robots.txt").respond_with_data(body, status=status, content_type="text/plain")
    for path, page in (pages or {}).items():
        status, body = page if isinstance(page, tuple) else (200, page)
        s.expect_request(path).respond_with_data(body, status=status, content_type="text/html")
    return s


def row(s, domain="a.org", sector="device"):
    return {"sector": sector, "domain": domain, "start_url": s.url_for("/"), "n_units": "1", "beds_sum": "",
            "name_tokens": ""}


def judge(store, tmp_path, verdict=(True, True)):
    out = tmp_path / "judgements.jsonl"
    text = json.dumps({"invites_reports": verdict[0], "contact_method": verdict[1], "safe_harbor": False})
    run_judging(store, out, ask=lambda *a, **k: SimpleNamespace(text=text))
    return out


def derive(store, rows, judgements):
    reg = Registries(platforms={}, diodb=set(), cna=set())
    out = derive_domains(store, pd.DataFrame(rows), judgements, reg, judgements.parent / "none.jsonl", NOW)
    return out.set_index("domain")


async def crawl_and_derive(store, live, tmp_path, s, **kw):
    r = row(s)
    assert await crawl_domain(r, 1, fetcher_for(store, live, kw.pop("optout", frozenset())), store, NOW) == "measured"
    return derive(store, [r], judge(store, tmp_path)).loc["a.org"]


# ---- B3: the reviewer's three dry-run rows

async def test_robots_disallowing_everything_with_a_real_policy_linked_is_unmeasured(servers, store, live, tmp_path):
    s = site(servers, home=HOME + "<a href='/responsible-disclosure'>Responsible Disclosure</a>",
             robots="User-agent: *\nDisallow: /\n", pages={"/responsible-disclosure": POLICY})
    r = await crawl_and_derive(store, live, tmp_path, s)
    assert r.policy_page == "unmeasured" and r.policy_look == "robots_disallowed;candidate_disallowed"
    assert r.main is False and r.broad is None


async def test_robots_answering_503_is_unmeasured(servers, store, live, tmp_path):
    s = site(servers, home=HOME + "<a href='/responsible-disclosure'>Responsible Disclosure</a>",
             robots=(503, "busy"), pages={"/responsible-disclosure": POLICY})
    r = await crawl_and_derive(store, live, tmp_path, s)
    assert r.policy_page == "unmeasured" and r.policy_look == "robots_unreadable"
    assert r.main is False and r.broad is None


async def test_a_vulnerability_disclosure_policy_link_answering_503_is_unmeasured(servers, store, live, tmp_path):
    s = site(servers, home=HOME + "<a href='/legal/vdp'>Vulnerability Disclosure Policy</a>",
             pages={"/legal/vdp": (503, "Service Unavailable")})
    r = await crawl_and_derive(store, live, tmp_path, s)
    assert r.policy_page == "unmeasured" and r.policy_look == "request_failed"
    assert r.main is False and r.broad is None


# ---- B3: the other reasons, and a complete look

async def test_a_complete_look_that_finds_nothing_is_no(servers, store, live, tmp_path):
    s = site(servers, home=HOME + "<a href='/about'>About us</a>")
    r = await crawl_and_derive(store, live, tmp_path, s)
    assert r.policy_page == "no" and r.policy_look == "" and r.probes_cut == 0 and r.broad is False
    assert r.policy_page_offdomain is None


async def test_a_policy_page_found_is_yes_whatever_else_happened(servers, store, live, tmp_path):
    s = site(servers, home=HOME + "<a href='/vdp'>Vulnerability Disclosure Policy</a><a href='/psirt-x'>PSIRT</a>",
             pages={"/vdp": POLICY, "/psirt-x": (503, "busy")})
    r = await crawl_and_derive(store, live, tmp_path, s)
    assert r.policy_page == "yes" and r.policy_look == "" and r.broad is True


async def test_a_policy_url_the_site_points_to_that_robots_disallows(servers, store, live, tmp_path):
    s = site(servers, robots="User-agent: *\nDisallow: /pol\n")
    s.expect_request("/.well-known/security.txt").respond_with_data(
        f"Contact: mailto:a@a.org\nPolicy: {s.url_for('/pol')}\n", content_type="text/plain")
    r = await crawl_and_derive(store, live, tmp_path, s)
    assert r.policy_page == "unmeasured" and r.policy_look == "candidate_disallowed"
    assert r.sectxt_class == "invalid" and r.broad is None  # served over plain HTTP here, so not the main outcome


async def test_a_conventional_path_that_answers_503_is_a_failed_request(servers, store, live, tmp_path):
    s = site(servers, pages={"/security": (503, "busy")})
    r = await crawl_and_derive(store, live, tmp_path, s)
    assert r.policy_page == "unmeasured" and r.policy_look == "request_failed"


async def test_an_unreadable_page_and_a_page_with_no_judgement(servers, store, live, tmp_path):
    s = site(servers, home=HOME + "<a href='/vdp.pdf'>Vulnerability Disclosure Policy</a>")
    s.expect_request("/vdp.pdf").respond_with_data(b"%PDF-1.4 not really a pdf", content_type="application/pdf")
    r0 = row(s)
    assert await crawl_domain(r0, 1, fetcher_for(store, live), store, NOW) == "measured"
    out = tmp_path / "j.jsonl"
    run_judging(store, out, ask=lambda *a, **k: None, pdf_runner=lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert derive(store, [r0], out).loc["a.org", "policy_look"] == "unreadable_page"
    r = derive(store, [r0], tmp_path / "no-judgements.jsonl").loc["a.org"]
    assert r.policy_page == "unmeasured" and r.policy_look == "not_judged"


async def test_a_truncated_homepage_makes_the_look_incomplete(servers, store, live, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_HOMEPAGE_BYTES", 400)  # the homepage's own cap (post-pilot X6)
    s = site(servers, home=HOME + "<p>" + "news " * 200 + "</p><a href='/trust/vdp'>Vulnerability Disclosure</a>")
    r = await crawl_and_derive(store, live, tmp_path, s)
    assert r.homepage_truncated is True
    assert r.policy_page == "unmeasured" and r.policy_look == "homepage_truncated" and r.broad is None


async def test_conventional_paths_cut_by_the_budget_do_not_make_the_look_incomplete(servers, store, live, tmp_path,
                                                                                    monkeypatch):
    monkeypatch.setattr(config, "MAX_REQUESTS_PER_DOMAIN", 7)  # homepage, robots, 2 files, 3 of 6 paths
    s = site(servers)
    r = await crawl_and_derive(store, live, tmp_path, s)
    assert r.policy_page == "no" and r.policy_look == "" and r.probes_cut == 3 and r.homepage_truncated is False


async def test_a_candidate_never_requested_makes_the_look_incomplete(servers, store, live, tmp_path):
    third = site(servers, pages={"/vdp": POLICY})
    s = site(servers, home=HOME + f"<a href='{third.url_for('/vdp')}'>Report a vulnerability</a>")
    r = await crawl_and_derive(store, live, tmp_path, s, optout=frozenset({third.host}))  # a third party opted out
    assert third.log == []
    assert r.policy_page == "unmeasured" and r.policy_look == "candidate_skipped"


# ---- B16: pass 2 retries an incomplete look, and the look is judged on both passes

async def test_robots_503_in_pass_1_is_retried_and_pass_2_completes_the_look(servers, store, live, tmp_path):
    s = site(servers, robots=None)
    flaky = {"pass": 1}
    s.expect_request("/robots.txt").respond_with_handler(
        lambda req: Response("busy" if flaky["pass"] == 1 else NO_ROBOTS, status=503 if flaky["pass"] == 1 else 200))
    df = pd.DataFrame([row(s)])
    frozen = freeze(tmp_path / "repo")
    f = fetcher_for(store, live)
    assert await crawl_sector(df, 1, f, store, frozen_file=frozen) == {"measured": 1}
    assert needs_retry(store, "a.org") is True
    assert derive(store, [row(s)], judge(store, tmp_path)).loc["a.org", "policy_look"] == "robots_unreadable"
    flaky["pass"] = 2
    later = datetime.now(timezone.utc) + timedelta(hours=49)
    assert await crawl_sector(df, 2, f, store, frozen_file=frozen, now=later) == {"measured": 1}
    r = derive(store, [row(s)], judge(store, tmp_path)).loc["a.org"]
    assert r.policy_page == "no" and r.policy_look == "" and r.broad is False


async def test_a_failed_request_is_retried_and_cleared_when_pass_2_measures_it(servers, store, live, tmp_path):
    s = site(servers, home=HOME + "<a href='/legal/vdp'>Vulnerability Disclosure Policy</a>")
    state = {"pass": 1}
    s.expect_request("/legal/vdp").respond_with_handler(
        lambda req: Response("busy" if state["pass"] == 1 else "<p>Careers</p>" + "<p>Join our team.</p>" * 20,
                             status=503 if state["pass"] == 1 else 200, content_type="text/html"))
    df = pd.DataFrame([row(s)])
    frozen = freeze(tmp_path / "repo")
    f = fetcher_for(store, live)
    await crawl_sector(df, 1, f, store, frozen_file=frozen)
    assert needs_retry(store, "a.org") is True
    state["pass"] = 2
    await crawl_sector(df, 2, f, store, frozen_file=frozen, now=datetime.now(timezone.utc) + timedelta(hours=49))
    r = derive(store, [row(s)], judge(store, tmp_path, verdict=(False, False))).loc["a.org"]
    assert r.policy_page == "no" and r.policy_look == ""


async def test_reasons_a_retry_cannot_fix_do_not_select_the_domain(servers, store, live, tmp_path):
    s = site(servers, home=HOME + "<a href='/responsible-disclosure'>Responsible Disclosure</a>",
             robots="User-agent: *\nDisallow: /\n")
    assert await crawl_domain(row(s), 1, fetcher_for(store, live), store, NOW) == "measured"
    assert needs_retry(store, "a.org") is False


# ---- B10: a policy page on another site is marked

async def test_a_policy_page_on_another_site_is_marked_offdomain(servers, store, live, tmp_path):
    third = site(servers, pages={"/vdp": POLICY})
    s = site(servers, home=HOME + f"<a href='{third.url_for('/vdp')}'>Report a vulnerability</a>")
    r = await crawl_and_derive(store, live, tmp_path, s)
    assert r.policy_page == "yes" and r.policy_page_offdomain is True


async def test_a_policy_page_on_the_final_site_is_not_offdomain(servers, store, live, tmp_path):
    third = site(servers, pages={"/vdp": POLICY})
    s = site(servers, home=HOME + f"<a href='{third.url_for('/vdp')}'>Report a vulnerability</a>"
             "<a href='/psirt'>PSIRT</a>", pages={"/psirt": POLICY + b"<p>ours</p>"})
    r = await crawl_and_derive(store, live, tmp_path, s)
    assert r.policy_page == "yes" and r.policy_page_offdomain is False


# ---- B13: a domain in two sector tables is crawled once and derived the same in both

async def test_a_domain_listed_as_hospital_and_device_has_one_measurement(servers, store, live, tmp_path):
    s = site(servers, home=HOME + "<a href='/vdp'>Vulnerability Disclosure Policy</a>", pages={"/vdp": POLICY})
    s.expect_request("/cms-hpt.txt").respond_with_data(
        "location-name: Andalusia\nmrf-url: https://a.org/mrf.json\n", content_type="text/plain")
    frozen = freeze(tmp_path / "repo")
    f = fetcher_for(store, live)
    hospital, device = row(s, sector="hospital"), row(s, sector="device")
    assert await crawl_sector(pd.DataFrame([hospital]), 1, f, store, frozen_file=frozen) == {"measured": 1}
    assert await crawl_sector(pd.DataFrame([device]), 1, f, store, frozen_file=frozen) == {"measured": 1}  # resumed
    assert {r.sector for r in store.requests_for("a.org", 1)} == {"hospital"}
    d = derive(store, [hospital, device], judge(store, tmp_path)).reset_index()
    h, v = d[d.sector == "hospital"].iloc[0], d[d.sector == "device"].iloc[0]
    same = ["status", "ua_kind", "final_domain", "sectxt_class", "main", "strict", "policy_page", "policy_look",
            "listing", "broad", "probes_cut", "homepage_truncated", "requests_sent"]
    assert {c: h[c] for c in same} == {c: v[c] for c in same}
    assert h.status == "measured" and h.policy_page == "yes" and h.final_domain == s.host
    assert h.cmshpt_class == "present_valid" and v.cmshpt_class == ""  # cms-hpt values only in the hospital row


# ---- B15: requests sent, and why a domain ended blocked

async def test_a_domain_ended_by_a_429_is_told_apart_from_one_refused_outright(servers, store, live, tmp_path):
    rate = site(servers, robots=(429, "slow down"))
    refused = servers()
    refused.expect_request("/").respond_with_data("denied", status=403, content_type="text/html")
    f = fetcher_for(store, live)
    rows = [row(rate, "rate.org"), row(refused, "refused.org")]
    assert await crawl_domain(rows[0], 1, f, store, NOW) == "blocked"
    assert await crawl_domain(rows[1], 1, f, store, NOW) == "blocked"
    d = derive(store, rows, tmp_path / "none.jsonl")
    assert d.loc["rate.org", "blocked_reason"] == "rate_limited" and d.loc["rate.org", "requests_sent"] == 2
    assert d.loc["refused.org", "blocked_reason"] == "refused"
    assert d.loc["refused.org", "requests_sent"] == 2  # the identified request and its one browser retry


async def test_reused_copies_are_not_requests_sent(servers, store, live, tmp_path):
    hub = site(servers)
    aliases = []
    for name in ("one.org", "two.org"):
        a = servers()
        a.expect_request("/").respond_with_data("", status=301, headers={"Location": hub.url_for("/")})
        aliases.append(row(a, name))
    f = fetcher_for(store, live)
    for r in aliases:
        assert await crawl_domain(r, 1, f, store, NOW) == "measured"
    d = derive(store, aliases, judge(store, tmp_path))
    assert d.loc["one.org", "requests_sent"] == 10  # homepage, robots, two files, six paths
    assert d.loc["two.org", "requests_sent"] == 1  # its own homepage; the rest were copies of one.org's
    assert store.db.execute("SELECT COUNT(*) FROM requests WHERE domain = 'two.org' AND reused").fetchone()[0] == 9
    assert d.loc["one.org", "status"] == d.loc["two.org", "status"] == "measured"
    assert d.loc["two.org", "policy_look"] == "" and d.loc["two.org", "probes_cut"] == 0  # copies are measurements


# ---- closing fixes, C2: only a real answer is a measurement

async def test_security_txt_files_answering_202_are_unmeasured_and_retried(servers, store, live, tmp_path):
    wait = (202, "<html>please wait</html>")
    s = site(servers, pages={"/.well-known/security.txt": wait, "/security.txt": wait})
    r0 = row(s, sector="hospital")
    assert await crawl_domain(r0, 1, fetcher_for(store, live), store, NOW) == "measured"
    r = derive(store, [r0], judge(store, tmp_path)).loc["a.org"]
    assert r.sectxt_class == "unmeasured" and r.main is None and r.broad is None
    assert r.cmshpt_class == "absent"  # its cms-hpt.txt answered 404: still absent
    assert needs_retry(store, "a.org") is True


async def test_a_candidate_answering_202_is_a_failed_request(servers, store, live, tmp_path):
    s = site(servers, home=HOME + "<a href='/legal/vdp'>Vulnerability Disclosure Policy</a>",
             pages={"/legal/vdp": (202, "<html>please wait</html>")})
    r = await crawl_and_derive(store, live, tmp_path, s)
    assert r.policy_page == "unmeasured" and r.policy_look == "request_failed" and r.broad is None


# ---- closing fixes, C3: a late opt-out covers a site's pages wherever they were collected

async def test_a_page_on_a_site_that_opts_out_after_the_crawl_is_withheld_and_purged(servers, store, live, tmp_path):
    from noroshi.optout import purge

    third = site(servers, pages={"/vdp": POLICY})  # site S
    s = site(servers, home=HOME + f"<a href='{third.url_for('/vdp')}'>Report a vulnerability</a>")
    b = row(s)  # listed domain B, whose candidate page is on S
    assert await crawl_domain(b, 1, fetcher_for(store, live), store, NOW) == "measured"
    judged = judge(store, tmp_path)  # judged before the opt-out arrived
    assert derive(store, [b], judged).loc["a.org", "policy_page"] == "yes"
    optout = frozenset({third.host})  # S opts out after the crawl

    reg = Registries(platforms={}, diodb=set(), cna=set())
    r = derive_domains(store, pd.DataFrame([b]), judged, reg, tmp_path / "none.jsonl", NOW, optout=optout).loc[0]
    assert (r.status, r.policy_page, r.policy_look, r.broad) == ("measured", "unmeasured", "candidate_skipped", None)

    asked = []
    counts = run_judging(store, tmp_path / "late.jsonl", optout=optout,
                         ask=lambda prompt, **k: asked.append(prompt) or SimpleNamespace(text="{}"))
    assert asked == [] and counts["opted_out"] == 1  # the page is not sent to the judge

    page = next(r for r in store.requests_for("a.org", 1) if r.purpose == "candidate")
    own = [r for r in store.requests_for("a.org", 1) if r.purpose != "candidate"]
    removed = purge(store, third.host, judged, [], tmp_path / "llm_log.jsonl")
    assert store.requests_for("a.org", 1) == own  # B's own records stay
    assert not (store.blob_dir / page.body_sha256).exists()
    assert page.url not in judged.read_text()
    assert (removed["records"], removed["requests sent"], removed["blobs"], removed["judgement rows"]) == (1, 1, 1, 1)


# ---- post-pilot X5: a meta refresh target stands in for its page

def refresh_to(path):
    return f'<html><head><meta http-equiv="refresh" content="0;URL=\'{path}\'"/><title>x</title></head><body></body></html>'


async def test_a_meta_refresh_target_stands_in_for_the_page(servers, store, live, tmp_path):
    s = site(servers, home=HOME + "<a href='/vdp'>Vulnerability Disclosure</a>",
             pages={"/vdp": refresh_to("/policies/vdp"), "/policies/vdp": POLICY})
    r = await crawl_and_derive(store, live, tmp_path, s)
    assert (r.policy_page, r.policy_look) == ("yes", "")


async def test_a_meta_refresh_target_that_fails_leaves_the_look_incomplete(servers, store, live, tmp_path):
    s = site(servers, home=HOME + "<a href='/vdp'>Vulnerability Disclosure</a>",
             pages={"/vdp": refresh_to("/policies/vdp"), "/policies/vdp": (503, "busy")})
    r = await crawl_and_derive(store, live, tmp_path, s)
    assert (r.policy_page, r.policy_look) == ("unmeasured", "request_failed")


async def test_a_meta_refresh_target_robots_txt_disallows_leaves_the_look_incomplete(servers, store, live, tmp_path):
    s = site(servers, home=HOME + "<a href='/vdp'>Vulnerability Disclosure</a>",
             robots="User-agent: *\nDisallow: /policies/\n",
             pages={"/vdp": refresh_to("/policies/vdp"), "/policies/vdp": POLICY})
    r = await crawl_and_derive(store, live, tmp_path, s)
    # post-pilot Y2: the refresh was not followed, so nothing stands in for the empty page, which is unreadable too
    assert (r.policy_page, r.policy_look) == ("unmeasured", "candidate_disallowed;unreadable_page")


# ---- fix round 3, K2: an address this listed domain already got an answer from is not requested again

async def test_a_candidate_the_policy_url_ended_at_is_not_requested_again(servers, store, live, tmp_path):
    """qmenta.com in the second pilot: the Policy: URL redirected to the page the homepage links."""
    s = site(servers, home=HOME + "<a href='/security-policy'>Vulnerability Disclosure Policy</a>",
             pages={"/security-policy": POLICY})
    s.expect_request("/.well-known/security.txt").respond_with_data(
        f"Contact: mailto:a@a.org\nPolicy: {s.url_for('/policy')}\n", content_type="text/plain")
    s.expect_request("/policy").respond_with_data("", status=301, headers={"Location": s.url_for("/security-policy")})
    r = await crawl_and_derive(store, live, tmp_path, s)
    assert [q.path for q, _ in s.log].count("/security-policy") == 1  # reached once, through the Policy: URL
    assert "candidate" not in [x.purpose for x in store.requests_for("a.org", 1)]
    assert (r.policy_page, r.policy_look) == ("yes", "")  # the look counts it read through the record that ended there


async def test_a_same_page_link_to_the_homepage_it_ended_at_is_not_requested_again(servers, store, live, tmp_path):
    """infinx.com in the second pilot: a menu link `href="#"` on a homepage reached by redirect."""
    s = site(servers, home=HOME + "<a href='#'>Security</a>")
    s.expect_request("/start").respond_with_data("", status=301, headers={"Location": s.url_for("/")})
    r = row(s) | {"start_url": s.url_for("/start")}
    assert await crawl_domain(r, 1, fetcher_for(store, live), store, NOW) == "measured"
    assert [q.path for q, _ in s.log].count("/") == 1  # the homepage's own address, once
    d = derive(store, [r], judge(store, tmp_path)).loc["a.org"]
    assert (d.policy_page, d.policy_look) == ("no", "")


async def test_a_conventional_path_a_candidate_ended_at_is_not_requested_again(servers, store, live, tmp_path):
    s = site(servers, home=HOME + "<a href='/trust'>Trust Center</a>", pages={"/security": POLICY})
    s.expect_request("/trust").respond_with_data("", status=301, headers={"Location": s.url_for("/security")})
    r = await crawl_and_derive(store, live, tmp_path, s)
    assert [q.path for q, _ in s.log].count("/security") == 1
    assert [x.url for x in store.requests_for("a.org", 1) if x.purpose == "probe"] == [
        s.url_for(p) for p in ("/vulnerability-disclosure-policy", "/responsible-disclosure",
                               "/vulnerability-disclosure", "/product-security", "/psirt")]
    assert (r.policy_page, r.policy_look, r.probes_cut) == ("yes", "", 0)


# ---- fix round 3, K7: an image answer and a page that is the homepage count as read

def judgement_rows(tmp_path):
    return [json.loads(x) for x in (tmp_path / "judgements.jsonl").read_text().splitlines()]


async def test_a_page_answered_with_an_image_is_read_and_is_not_a_policy_page(servers, store, live, tmp_path):
    """modulemd.com in the second pilot (#1230): /security redirects to a JPEG."""
    s = site(servers)
    s.expect_request("/security").respond_with_data("", status=301, headers={"Location": s.url_for("/up/security.jpg")})
    s.expect_request("/up/security.jpg").respond_with_data(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64,
                                                           content_type="image/jpeg")
    r = await crawl_and_derive(store, live, tmp_path, s)
    assert [x["skipped"] for x in judgement_rows(tmp_path)] == ["image"]
    assert (r.policy_page, r.policy_look, r.policy_pages_unjudged) == ("no", "", 0)


async def test_pages_that_end_at_the_homepage_are_the_homepage_even_when_cut(servers, store, live, tmp_path,
                                                                            monkeypatch):
    """ossur.com in the second pilot: every conventional path redirects to the homepage's own final URL, and those
    copies were cut at the size limit for pages other than the homepage."""
    from noroshi.discover import PROBE_PATHS

    monkeypatch.setattr(config, "MAX_BODY_BYTES", 120)  # the homepage is read whole; every other copy of it is cut
    s = site(servers, home=HOME + "<a href='/sec'>Security</a>")
    for path in ("/sec", *PROBE_PATHS):
        s.expect_request(path).respond_with_data("", status=301, headers={"Location": s.url_for("/")})
    r = await crawl_and_derive(store, live, tmp_path, s)
    pages = [x for x in store.requests_for("a.org", 1) if x.purpose in ("candidate", "probe")]
    assert len(pages) == 7 and all(x.truncated and x.final_url == s.url_for("/") for x in pages)
    assert [x["skipped"] for x in judgement_rows(tmp_path)] == ["same_as_homepage"] * 7
    assert (r.policy_page, r.policy_look, r.policy_pages_unjudged) == ("no", "", 0)


# ---- fix round 4, N1 and N2: only answered pages count as already requested, and the judge reads refreshes the same way

async def test_a_page_a_security_txt_redirect_reached_is_still_requested_and_judged(servers, store, live, tmp_path):
    """The re-reviewer's probe rrY3/test_probe_k2_corefile.py: a core file's final address is not a page read."""
    s = site(servers, home=HOME + "<a href='/responsible-disclosure'>Responsible Disclosure</a>",
             pages={"/responsible-disclosure": POLICY})
    s.expect_request("/.well-known/security.txt").respond_with_data(
        "", status=301, headers={"Location": s.url_for("/responsible-disclosure")})
    r = await crawl_and_derive(store, live, tmp_path, s)
    assert [x.purpose for x in store.requests_for("a.org", 1)].count("candidate") == 1
    assert (r.policy_page, r.policy_look) == ("yes", "")


async def test_a_refresh_stub_to_the_homepage_reached_by_redirect_is_set_aside(servers, store, live, tmp_path):
    """The re-reviewer's probe rrY3/test_probe_k2_refresh.py: the stub's target is the homepage's final address,
    answered but never itself requested."""
    s = site(servers, pages={"/security": "<html><head><meta http-equiv='refresh' content='0; url=/'></head>"
                                          "<body>Moved.</body></html>"})
    s.expect_request("/start").respond_with_data("", status=301, headers={"Location": s.url_for("/")})
    r = row(s) | {"start_url": s.url_for("/start")}
    assert await crawl_domain(r, 1, fetcher_for(store, live), store, NOW) == "measured"
    out = judge(store, tmp_path)
    assert [(x["url"].rsplit("/", 1)[-1], x["skipped"]) for x in judgement_rows(tmp_path)] == [("security",
                                                                                                "meta_refresh")]
    d = derive(store, [r], out).loc["a.org"]
    assert (d.policy_page, d.policy_look) == ("no", "")
