"""Host spellings: one spelling per URL (post-pilot X1) and the other host form after a network error (X2). Local
HTTPS sites are reached on the default port: every
connection to port 443 goes to one local TLS server, which answers by Host name. Names still resolve only through
the strict test resolver."""
import re
import ssl
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import urlsplit

import anyio
import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

from crawled import make_certificate
from noroshi import config
from noroshi.crawl import OutageWatch, crawl_domain
from noroshi.fetch import PoliteFetcher
from noroshi.look import look
from noroshi.store import Store

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
CERT_NAMES = ["988.test", "stopalcoholabuse.test", "www.samhsa.test", "womenshealth.test", "menopause.test",
              "www.acme-dns.org", "tlsco.test", "www.wall.test", "wall.test", "www.busy.test", "busy.test",
              "www.slow.test", "slow.test", "redir.test", "away.test", "www.away.test", "www.gate.test", "gate.test",
              "hop.test", "www.hop.test"]
# not in the certificate: www.tlsco.test, www.redir.test (TLS failures). Not resolvable: acme-dns.org, gone.org,
# www.gone.org, elsewhere.org.
HOME = ("<title>{t}</title><p>{t} serves the public.</p>"  # 200 visible characters or more (post-pilot Y4)
        + "<p>Our offices are open every weekday, and our staff answer every letter, call and visit.</p>" * 2)


class FakeTime:
    def __init__(self):
        self.now = 1000.0

    def clock(self):
        return self.now

    async def sleep(self, secs):
        self.now += secs


@pytest.fixture
def https(tmp_path, monkeypatch, local_dns):
    """One local HTTPS server for every name in CERT_NAMES, reached at https://<name>/ (port 443). `https.handler`
    answers each request; `https.log` lists (host, path) of every request the server received."""
    cert, key = make_certificate(tmp_path / "tls", CERT_NAMES)
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(cert, key)
    server = HTTPServer(host="127.0.0.1", port=0, ssl_context=context)
    server.start()
    monkeypatch.setenv("SSL_CERT_FILE", str(cert))
    local_dns.update(CERT_NAMES)  # names still resolve through the strict test resolver
    connect = anyio.connect_tcp  # what httpcore opens every connection with

    async def to_server(remote_host, remote_port, **kw):
        return await connect(remote_host, server.port if remote_port == 443 else remote_port, **kw)

    monkeypatch.setattr(anyio, "connect_tcp", to_server)
    server.handler = lambda request: Response("not here", status=404)
    server.expect_request(re.compile(".*")).respond_with_handler(lambda request: server.handler(request))
    server.hits = lambda host: [r.path for r, _ in server.log if r.host.split(":")[0] == host]
    yield server
    server.stop()


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "db" / "c.sqlite") as s:
        yield s


@pytest.fixture
def fetcher(store, tmp_path):
    live = tmp_path / "LIVE"
    live.write_text("go")
    ft = FakeTime()
    return PoliteFetcher(store, frozenset(), live, clock=ft.clock, sleep=ft.sleep)


def row(domain, start_url, sector="federal"):
    return {"sector": sector, "domain": domain, "start_url": start_url, "name_tokens": ""}


def agency(redirects):
    """`redirects`: listed host -> where its homepage sends the visitor (as the site wrote it)."""
    def handle(request):
        host = request.host.split(":")[0]
        if host in redirects and request.path == "/":
            return Response("", status=301, headers={"Location": redirects[host]})
        if request.path in ("/", "/mental-health/988", "/substance-use/prevention/iccpud", "/menopause"):
            return Response(HOME.format(t=host), content_type="text/html")
        if request.path == "/robots.txt":
            return Response("User-agent: *\nDisallow: /admin\n", content_type="text/plain")
        return Response("<html>The specified URL cannot be found.</html>", status=404, content_type="text/html")
    return handle


# ---- X1: one spelling per URL

async def test_samhsa_a_final_url_with_443_leads_to_requests_without_the_port_and_a_second_domain_reuses_them(
        https, store, fetcher):
    """Pilot #248/#294 and #274/#335: 988.gov landed on https://www.samhsa.gov:443/mental-health/988 and its files
    went out with :443; stopalcoholabuse.gov then sent the same requests again without the port."""
    https.handler = agency({"988.test": "https://www.samhsa.test:443/mental-health/988",
                            "stopalcoholabuse.test": "https://www.samhsa.test/substance-use/prevention/iccpud"})
    assert await crawl_domain(row("988.test", "https://988.test/"), 1, fetcher, store, NOW) == "measured"
    first = store.requests_for("988.test", 1)
    assert first[0].final_url == "https://www.samhsa.test:443/mental-health/988"  # as the site wrote it
    assert [r.url for r in first[1:3]] == ["https://www.samhsa.test/robots.txt",
                                           "https://www.samhsa.test/.well-known/security.txt"]
    assert not any(":443" in r.url for r in first)
    sent = https.hits("www.samhsa.test")
    assert await crawl_domain(row("stopalcoholabuse.test", "https://stopalcoholabuse.test/"), 1, fetcher, store,
                              NOW) == "measured"
    assert https.hits("www.samhsa.test") == sent + ["/substance-use/prevention/iccpud"]  # only its homepage hop
    second = store.requests_for("stopalcoholabuse.test", 1)
    assert [r.reused for r in second] == [False] + [True] * (len(second) - 1)
    assert [r.url for r in second[1:]] == [r.url for r in first[1:]]


async def test_womenshealth_a_second_domain_landing_on_443_reuses_the_first_ones_requests(https, store, fetcher):
    """Pilot #245/#313 and #253/#348: womenshealth.gov went first without the port; menopause.gov landed on
    https://womenshealth.gov:443/menopause and sent the same requests again with it."""
    https.handler = agency({"menopause.test": "https://womenshealth.test:443/menopause"})
    assert await crawl_domain(row("womenshealth.test", "https://womenshealth.test/"), 1, fetcher, store,
                              NOW) == "measured"
    sent = https.hits("womenshealth.test")
    assert await crawl_domain(row("menopause.test", "https://menopause.test/"), 1, fetcher, store, NOW) == "measured"
    assert https.hits("womenshealth.test") == sent + ["/menopause"]  # only its homepage hop
    assert len(sent) == len(set(sent))  # every URL of the site was sent once in the pass
    second = store.requests_for("menopause.test", 1)
    assert all(r.reused for r in second[1:]) and not any(":443" in r.url for r in second)
    assert store.count_requests("menopause.test", 1) == 1


async def test_a_listed_url_spelled_with_the_default_port_or_upper_case_is_stored_and_reused_in_one_spelling(
        https, store, fetcher):
    https.handler = agency({})
    assert await crawl_domain(row("womenshealth.test", "HTTPS://WomensHealth.TEST:443/"), 1, fetcher, store,
                              NOW) == "measured"
    assert store.requests_for("womenshealth.test", 1)[0].url == "https://womenshealth.test/"
    n = len(https.log)
    assert await crawl_domain(row("menopause.test", "https://womenshealth.test./"), 1, fetcher, store,
                              NOW) == "measured"
    assert len(https.log) == n  # the same homepage, spelled another way: reused, nothing sent


# ---- X2: after a network error, the other host form once

def answers(request):
    host = request.host.split(":")[0]
    if host == "redir.test" and request.path == "/":
        return Response("", status=301, headers={"Location": "https://www.redir.test/"})
    if host == "away.test" and request.path == "/":
        return Response("", status=301, headers={"Location": "https://elsewhere.org/"})
    if host == "www.wall.test":
        return Response("<h1>Access Denied</h1>", status=403, content_type="text/html")
    if host in ("www.busy.test", "www.slow.test"):
        return Response("busy", status=503 if host == "www.busy.test" else 429)
    if request.path == "/":
        return Response(HOME.format(t=host), content_type="text/html")
    return Response("<html>not found</html>", status=404, content_type="text/html")


def homepages(store, domain):
    return [(r.url, r.error or r.status) for r in store.requests_for(domain, 1) if r.purpose == "homepage"]


async def test_a_bare_name_that_fails_with_dns_is_measured_at_its_www_form(https, store, fetcher):
    https.handler = answers
    assert await crawl_domain(row("acme-dns.org", "https://acme-dns.org/", "device"), 1, fetcher, store,
                              NOW) == "measured"
    assert homepages(store, "acme-dns.org") == [("https://acme-dns.org/", "dns"), ("https://www.acme-dns.org/", 200)]
    recs = store.requests_for("acme-dns.org", 1)
    assert recs[2].url == "https://www.acme-dns.org/robots.txt"  # the crawl continues from the answering form
    assert store.count_requests("acme-dns.org", 1) == len(recs) == 11  # both homepage requests count in the 12
    assert look(store, {1: recs}, (1,)).reasons == []  # look.py replays from the homepage that answered
    n = len(https.log)
    store.db.execute("DELETE FROM done")  # a restart of the pass: neither form is requested again
    store.db.commit()
    assert await crawl_domain(row("acme-dns.org", "https://acme-dns.org/", "device"), 1, fetcher, store,
                              NOW) == "measured"
    assert len(https.log) == n


async def test_a_www_host_that_fails_with_tls_is_measured_at_its_bare_name(https, store, fetcher):
    https.handler = answers
    assert await crawl_domain(row("tlsco.test", "https://www.tlsco.test/", "hospital"), 1, fetcher, store,
                              NOW) == "measured"
    assert homepages(store, "tlsco.test") == [("https://www.tlsco.test/", "tls"), ("https://tlsco.test/", 200)]
    assert https.hits("tlsco.test")[:2] == ["/", "/robots.txt"]


async def test_both_forms_failing_is_unreachable_after_exactly_two_requests(https, store, fetcher):
    https.handler = answers
    assert await crawl_domain(row("gone.org", "https://gone.org/"), 1, fetcher, store, NOW) == "unreachable"
    assert homepages(store, "gone.org") == [("https://gone.org/", "dns"), ("https://www.gone.org/", "dns")]
    assert store.count_requests("gone.org", 1) == 2


@pytest.mark.parametrize("name,status,sent", [("wall.test", "blocked", 2), ("busy.test", "unreachable", 1),
                                              ("slow.test", "blocked", 1)])
async def test_no_other_form_after_a_refusal_an_http_status_or_a_429(https, store, fetcher, name, status, sent):
    https.handler = answers
    assert await crawl_domain(row(name, f"https://www.{name}/"), 1, fetcher, store, NOW) == status
    assert [url for url, _ in homepages(store, name)] == [f"https://www.{name}/"] * sent
    assert https.hits(name) == []


async def test_the_outage_watch_counts_a_domain_only_when_both_forms_failed(https, store, fetcher):
    async def control():
        raise AssertionError("the control request is asked only at 20 network failures in a row")

    https.handler = answers
    watch = OutageWatch(store, 1, control)
    watch.streak = [f"earlier{i}.org" for i in range(19)]
    assert await crawl_domain(row("acme-dns.org", "https://acme-dns.org/"), 1, fetcher, store, NOW,
                              watch=watch) == "measured"
    assert watch.streak == []  # the first form's failure was not counted; the answer reset the count
    watch.streak = [f"earlier{i}.org" for i in range(18)]
    assert await crawl_domain(row("gone.org", "https://gone.org/"), 1, fetcher, store, NOW,
                              watch=watch) == "unreachable"
    assert watch.streak[-2:] == ["earlier17.org", "gone.org"]  # one failure for the domain, not two


# ---- XR3: the other host form is not requested when the first attempt failed at that very host

async def test_a_bare_name_redirected_to_its_www_form_that_fails_is_not_sent_there_again(https, store, fetcher):
    async def control():
        raise AssertionError("the control request is asked only at 20 network failures in a row")

    https.handler = answers
    watch = OutageWatch(store, 1, control)
    watch.streak = [f"earlier{i}.org" for i in range(18)]
    assert await crawl_domain(row("redir.test", "https://redir.test/"), 1, fetcher, store, NOW,
                              watch=watch) == "unreachable"
    assert homepages(store, "redir.test") == [("https://redir.test/", "tls")]
    assert store.requests_for("redir.test", 1)[0].error_url == "https://www.redir.test/"
    assert watch.streak[-2:] == ["earlier17.org", "redir.test"]  # counted as a network failure, once


async def test_a_failure_at_a_redirect_target_on_a_third_host_still_tries_the_other_form(https, store, fetcher):
    https.handler = answers
    assert await crawl_domain(row("away.test", "https://away.test/"), 1, fetcher, store, NOW) == "measured"
    assert homepages(store, "away.test") == [("https://away.test/", "dns"), ("https://www.away.test/", 200)]


# ---- post-pilot fix round 1, F1: a domain leaves the pending set however its other form settles

def record(domain, url, error):
    from noroshi.store import FetchRecord

    return FetchRecord(domain, "federal", urlsplit(url).hostname, url, "homepage", 1, "identified",
                       "2026-11-20T00:00:00Z", None, None, [], None, None, 0, False, error, False)


async def trip(store, watch):
    """An outage stop unrelated to the domains under test: 19 counted failures and a 20th."""
    from noroshi.crawl import LocalOutage

    watch.streak = [f"d{i}.org" for i in range(19)]
    with pytest.raises(LocalOutage):
        await watch.homepage("d19.org", record("d19.org", "https://d19.org/", "dns"))


async def test_a_domain_whose_other_form_is_a_reused_copy_is_never_unmarked_by_a_later_stop(https, store, fetcher):
    """The reviewer's probe: tlsco.test starts at www. (TLS fails); its bare form was measured for another listed
    domain, so the other form is a reused copy and no fetch reaches the watch."""
    async def control():
        return False

    https.handler = answers
    watch = OutageWatch(store, 1, control)
    assert await crawl_domain(row("other.org", "https://tlsco.test/"), 1, fetcher, store, NOW, watch=watch) == "measured"
    assert await crawl_domain(row("tlsco.test", "https://www.tlsco.test/"), 1, fetcher, store, NOW,
                              watch=watch) == "measured"
    assert [(url, err) for url, err in homepages(store, "tlsco.test")] == [("https://www.tlsco.test/", "tls"),
                                                                          ("https://tlsco.test/", 200)]
    assert watch.pending == set()
    before = [(r.url, r.error) for r in store.requests_for("tlsco.test", 1)]
    await trip(store, watch)
    assert store.done_status("tlsco.test", 1) == "measured"
    assert [(r.url, r.error) for r in store.requests_for("tlsco.test", 1)] == before  # nothing relabelled
    n, sent = len(https.log), store.count_requests("tlsco.test", 1)
    assert await crawl_domain(row("tlsco.test", "https://www.tlsco.test/"), 1, fetcher, store, NOW,
                              watch=OutageWatch(store, 1, control)) == "measured"  # a rerun sends it nothing
    assert len(https.log) == n and store.count_requests("tlsco.test", 1) == sent


async def test_a_domain_whose_other_form_ends_the_crawl_before_a_fetch_leaves_the_pending_set(https, store, fetcher):
    """The other form's request raises before any fetch (here the site is rate-limited): pending is cleared too."""
    async def control():
        return False

    https.handler = answers
    watch = OutageWatch(store, 1, control)
    real = fetcher.fetch

    async def fetch(domain, sector, url, purpose, pass_no, reuse=None):
        rec = await real(domain, sector, url, purpose, pass_no, reuse=reuse)
        if url == "https://www.tlsco.test/":  # meanwhile another listed domain got a 429 from tlsco.test
            fetcher._rate_limited.add(("tlsco.test", 1))
        return rec

    fetcher.fetch = fetch
    assert await crawl_domain(row("tlsco.test", "https://www.tlsco.test/"), 1, fetcher, store, NOW,
                              watch=watch) == "blocked"
    assert watch.pending == set()
    await trip(store, watch)
    assert store.done_status("tlsco.test", 1) == "blocked"


# ---- post-pilot fix round 1, F2: on a rerun the stored answer is the homepage, and every reader takes the same one

async def test_a_rerun_after_a_stop_continues_from_the_stored_answer_of_the_other_form(https, store, fetcher):
    """The reviewer's sequence: acme-dns.org was pending; a stop relabelled its first form `outage`; its other form
    answered in the moment before the run was cancelled, and no done row was written. The rerun of the pass."""
    from noroshi.crawl import needs_retry
    from noroshi.sectxt_parse import classify_security_txt, is_forbidden

    def handle(request):
        if request.host.split(":")[0] == "www.acme-dns.org" and request.path == "/.well-known/security.txt":
            return Response("Access denied", status=403, content_type="text/plain")
        return answers(request)

    https.handler = handle
    first = record("acme-dns.org", "https://acme-dns.org/", "outage")
    second = replace(record("acme-dns.org", "https://www.acme-dns.org/", None), status=200, content_type="text/html",
                     final_url="https://www.acme-dns.org/")
    store.record(replace(first, sector="device", site="acme-dns.org"), None)
    store.record(replace(second, sector="device", site="acme-dns.org"), HOME.format(t="www.acme-dns.org").encode())
    assert await crawl_domain(row("acme-dns.org", "https://acme-dns.org/", "device"), 1, fetcher, store,
                              NOW) == "measured"
    recs = store.requests_for("acme-dns.org", 1)
    assert homepages(store, "acme-dns.org") == [("https://acme-dns.org/", "outage"), ("https://www.acme-dns.org/", 200)]
    assert "/" not in https.hits("www.acme-dns.org")  # the stored answer is the homepage: neither form is sent again
    assert recs[2].url == "https://www.acme-dns.org/robots.txt"
    assert look(store, {1: recs}, (1,)).reasons == []
    assert not needs_retry(store, "acme-dns.org")
    wk = [r for r in recs if r.purpose == "sectxt_wellknown"]
    assert [(r.status, r.ua_kind) for r in wk] == [(403, "identified"), (403, "browser")]
    body_of = lambda r: store.get_blob(r.body_sha256) if r.body_sha256 else None  # noqa: E731
    assert classify_security_txt(wk[-1].status, wk[-1].final_url, wk[-1].content_type, body_of(wk[-1]), True, None, NOW,
                                 forbidden=is_forbidden(recs, "sectxt_wellknown", body_of)) == "forbidden"


def test_the_homepage_of_a_pass_is_its_newest_homepage_record_among_outcomes():
    from noroshi.store import homepage_record

    a = record("a.org", "https://a.org/", "dns")
    b = replace(record("a.org", "https://www.a.org/", None), status=200)
    c = record("a.org", "https://a.org/", "outage")
    d = replace(record("a.org", "https://a.org/x", "in_flight"), purpose="robots")
    assert homepage_record([a, b, c, d]) is b and homepage_record([c]) is None and homepage_record([]) is None


# ---- post-pilot fix round 1, F3 and F4: no other form after a refusal, nor when it was already a redirect hop

async def test_no_other_form_after_a_refusal_whose_browser_retry_failed_at_the_network_level(https, store, fetcher):
    """The reviewer's probe 2: www.gate.test refuses the identified signature; the browser retry is redirected to a
    host that does not resolve. The first form was refused, so the other form is not tried, now or on a restart."""
    def handle(request):
        if request.host.split(":")[0] == "www.gate.test" and request.path == "/":
            if request.headers["User-Agent"] == config.UA_BROWSER:
                return Response("", status=301, headers={"Location": "https://elsewhere.org/"})
            return Response("<h1>Access Denied</h1>", status=403, content_type="text/html")
        return answers(request)

    https.handler = handle
    assert await crawl_domain(row("gate.test", "https://www.gate.test/"), 1, fetcher, store, NOW) == "unreachable"
    assert [(r.url, r.error or r.status, r.ua_kind) for r in store.requests_for("gate.test", 1)] == [
        ("https://www.gate.test/", 403, "identified"), ("https://www.gate.test/", "dns", "browser")]
    assert https.hits("gate.test") == []
    n = len(https.log)
    store.db.execute("DELETE FROM done")
    store.db.commit()
    assert await crawl_domain(row("gate.test", "https://www.gate.test/"), 1, fetcher, store, NOW) == "unreachable"
    assert len(https.log) == n  # the restart decides the same from the store


async def test_no_other_form_when_the_first_attempt_was_already_redirected_through_it(https, store, fetcher):
    """The reviewer's probe 3: hop.test redirects to www.hop.test, which redirects to a host that fails."""
    def handle(request):
        host = request.host.split(":")[0]
        if host == "hop.test" and request.path == "/":
            return Response("", status=301, headers={"Location": "https://www.hop.test/"})
        if host == "www.hop.test" and request.path == "/":
            return Response("", status=301, headers={"Location": "https://elsewhere.org/"})
        return answers(request)

    https.handler = handle
    assert await crawl_domain(row("hop.test", "https://hop.test/"), 1, fetcher, store, NOW) == "unreachable"
    assert https.hits("www.hop.test") == ["/"]  # once, as a redirect hop of the first attempt
    rec, = store.requests_for("hop.test", 1)
    assert (rec.redirect_chain, rec.error, rec.error_url) == (["https://hop.test/", "https://www.hop.test/"], "dns",
                                                              "https://elsewhere.org/")
