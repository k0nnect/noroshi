from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

from frozen import freeze
from noroshi import config
from noroshi.crawl import (HEALTH_TERMS, NotFrozen, crawl_domain, crawl_sector, for_sale, homepage_matches,
                           needs_retry)
from noroshi import crawl as crawl_mod
from noroshi.fetch import NotLive, PoliteFetcher
from noroshi.sectxt_parse import classify_security_txt
from noroshi.store import FetchRecord, Store

CHECKOUT = Path(__file__).resolve().parents[1]  # committed inputs are read from here

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
GOOD = b"Contact: mailto:sec@a.org\nExpires: 2030-01-01T00:00:00Z\n"
HOME = b"<html><title>Andalusia Health</title><body>welcome</body></html>"
LONG = b"<p>" + b"Totally unrelated parked page words. " * 14 + b"</p>"  # about 500 characters
CORE5 = ["homepage", "robots", "sectxt_wellknown", "sectxt_root", "cmshpt"]


class FakeTime:
    def __init__(self):
        self.now = 1000.0

    def clock(self):
        return self.now

    async def sleep(self, secs):
        self.now += secs


@pytest.fixture
def servers():
    """Local servers, each under its own host name (s1.test, s2.test, ...): each one is its own site.
    The test resolver maps every *.test name to 127.0.0.1."""
    made = []

    def make() -> HTTPServer:
        s = HTTPServer(host="127.0.0.1", port=0)
        s.no_handler_status_code = 404
        s.start()
        s.host = f"s{len(made) + 1}.test"  # url_for() now names this host
        made.append(s)
        return s

    yield make
    for s in made:
        if s.is_running():
            s.stop()


@pytest.fixture
def server(servers):
    s = servers()
    s.home = HOME  # tests change the homepage by assigning server.home
    s.expect_request("/").respond_with_handler(lambda request: Response(s.home, content_type="text/html"))
    return s


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "db" / "c.sqlite") as s:
        yield s


@pytest.fixture
def live(tmp_path):
    p = tmp_path / "LIVE"
    p.write_text("go")
    return p


@pytest.fixture
def frozen(tmp_path):
    return freeze(tmp_path / "repo")


@pytest.fixture
def fetcher(store, live):
    ft = FakeTime()
    return PoliteFetcher(store, frozenset(), live, clock=ft.clock, sleep=ft.sleep)


def at(server, host, path="/"):
    """The same local server reached under another host name."""
    return f"http://{host}:{server.port}{path}"


def row(server, domain="a.org", sector="hospital", tokens="andalusia"):
    return {"sector": sector, "domain": domain, "start_url": server.url_for("/"), "name_tokens": tokens}


def purposes(store, domain="a.org", pass_no=1):
    return [r.purpose for r in store.requests_for(domain, pass_no)]


def any_scheme(status, final_url, content_type, body, blocked, error, now):
    return classify_security_txt(status, "https://x/", content_type, body, blocked, error, now)


def test_homepage_mismatch_flagged():
    assert homepage_matches({"andalusia"}, b"<title>Andalusia Health</title>" + LONG, HEALTH_TERMS)
    assert homepage_matches({"zzz"}, b"<title>Riverside Medical Group</title>" + LONG, HEALTH_TERMS)
    assert not homepage_matches({"andalusia"}, b"<title>This domain is for sale</title>" + LONG, HEALTH_TERMS)
    assert homepage_matches(set(), b"<title>anything</title>" + LONG)
    assert not homepage_matches({"andalusia"}, b"<script>andalusia</script>" + LONG)


def test_js_shell_page_is_never_a_mismatch():
    assert homepage_matches({"andalusia"}, b"<title>Loading</title><body>" + b"x" * 40 + b"</body>")
    assert homepage_matches({"andalusia"}, b"<body><div id=root></div><script>app()</script></body>")


def test_meta_tags_count_as_a_match():
    page = b'<meta property="og:site_name" content="Andalusia Regional"><body>' + LONG + b"</body>"
    assert homepage_matches({"andalusia"}, page)
    assert homepage_matches({"andalusia"}, b'<meta name="description" content="About Andalusia">' + LONG)
    assert homepage_matches({"andalusia"}, b'<meta property="og:title" content="Andalusia">' + LONG)
    assert not homepage_matches({"andalusia"}, b'<meta name="keywords" content="Andalusia">' + LONG)


async def test_request_order_normal_site(server, store, fetcher):
    assert await crawl_domain(row(server), 1, fetcher, store, NOW) == "measured"
    assert purposes(store) == CORE5 + ["probe"] * 6
    assert store.done_status("a.org", 1) == "measured"
    assert len(server.log) == 11


async def test_root_file_skipped_when_wellknown_valid(server, store, fetcher):
    server.expect_request("/.well-known/security.txt").respond_with_data(GOOD, content_type="text/plain")
    await crawl_domain(row(server), 1, fetcher, store, NOW, classifier=any_scheme)
    assert "sectxt_root" not in purposes(store)
    assert not any(r.path == "/security.txt" for r, _ in server.log)


async def test_root_file_still_fetched_over_http_with_real_classifier(server, store, fetcher):
    server.expect_request("/.well-known/security.txt").respond_with_data(GOOD, content_type="text/plain")
    await crawl_domain(row(server), 1, fetcher, store, NOW)
    assert "sectxt_root" in purposes(store)  # plain http can never classify as valid


async def test_cmshpt_only_for_hospitals(server, store, fetcher):
    await crawl_domain(row(server, sector="federal", tokens=""), 1, fetcher, store, NOW)
    assert "cmshpt" not in purposes(store)
    assert not any(r.path == "/cms-hpt.txt" for r, _ in server.log)


async def test_files_fetched_from_final_host_after_redirect(servers, server, store, fetcher):
    a = servers()
    other = at(server, "final.test")
    a.expect_request("/").respond_with_data("", status=301, headers={"Location": other})
    await crawl_domain(row(a), 1, fetcher, store, NOW)
    assert [r.path for r, _ in a.log] == ["/"]
    assert [r.path for r, _ in server.log][:3] == ["/", "/robots.txt", "/.well-known/security.txt"]
    recs = store.requests_for("a.org", 1)
    assert recs[0].redirect_chain == [a.url_for("/")] and recs[0].final_url == other
    assert all(r.host == "final.test" for r in recs[1:])
    assert len(recs) == 11


async def test_blocked_homepage_stops_domain(servers, store, fetcher):
    s = servers()
    s.expect_request("/").respond_with_data("no", status=403)
    assert await crawl_domain(row(s), 1, fetcher, store, NOW) == "blocked"
    assert store.count_requests("a.org", 1) == 2 and store.done_status("a.org", 1) == "blocked"


async def test_unreachable_homepage(servers, store, fetcher):
    s = servers()
    r = row(s)
    s.stop()
    assert await crawl_domain(r, 1, fetcher, store, NOW) == "unreachable"
    assert store.count_requests("a.org", 1) == 2  # the listed host and its other form (post-pilot X2)


async def test_server_error_homepage_is_unreachable(servers, store, fetcher):
    s = servers()
    s.expect_request("/").respond_with_data("oops", status=503)
    assert await crawl_domain(row(s), 1, fetcher, store, NOW) == "unreachable"
    assert store.count_requests("a.org", 1) == 1


@pytest.mark.parametrize("status", [404, 410, 400, 300])
async def test_homepage_that_does_not_load_is_unreachable(servers, store, fetcher, status):
    s = servers()
    s.expect_request("/").respond_with_data("<a href='/rd'>Responsible Disclosure</a>", status=status)
    assert await crawl_domain(row(s), 1, fetcher, store, NOW) == "unreachable"
    assert purposes(store) == ["homepage"] and len(s.log) == 1


LINKED = b"<title>Andalusia</title><a href='/vdp'>Vulnerability Disclosure</a>"


async def test_robots_disallow_all_skips_probes_not_files(server, store, fetcher):
    server.expect_request("/robots.txt").respond_with_data("User-agent: *\nDisallow: /\n", content_type="text/plain")
    server.home = LINKED
    await crawl_domain(row(server), 1, fetcher, store, NOW)
    assert purposes(store) == CORE5


async def test_robots_with_byte_order_mark_still_disallows(server, store, fetcher):
    body = b"\xef\xbb\xbfUser-agent: *\nDisallow: /\n"
    server.expect_request("/robots.txt").respond_with_data(body, content_type="text/plain")
    server.home = LINKED
    await crawl_domain(row(server), 1, fetcher, store, NOW)
    assert purposes(store) == CORE5


async def test_candidates_and_probes_dedupe_and_cap(server, store, fetcher):
    links = "".join(f"<a href='/r{i}'>Responsible Disclosure</a>" for i in range(5))
    links += "<a href='/security'>Product Security</a><a href='https://hackerone.com/acme'>Bug Bounty</a>"
    server.home = f"<title>Andalusia</title>{links}".encode()
    assert await crawl_domain(row(server), 1, fetcher, store, NOW) == "measured"
    n = store.count_requests("a.org", 1)
    assert n <= 12 == config.MAX_REQUESTS_PER_DOMAIN
    assert purposes(store)[:5] == CORE5
    assert purposes(store).count("candidate") == 3
    assert not any("hackerone" in r.url for r in store.requests_for("a.org", 1))
    assert len(server.log) == n


async def test_cap_never_exceeded(server, store, fetcher, monkeypatch):
    monkeypatch.setattr(config, "MAX_REQUESTS_PER_DOMAIN", 6)
    assert await crawl_domain(row(server), 1, fetcher, store, NOW) == "measured"
    assert store.count_requests("a.org", 1) == 6


async def test_policy_field_counts_toward_three(server, store, fetcher):
    server.expect_request("/.well-known/security.txt").respond_with_data(
        b"Contact: mailto:a@a.org\nPolicy: %s\n" % server.url_for("/pol").encode(), content_type="text/plain")
    links = "".join(f"<a href='/r{i}'>Responsible Disclosure</a>" for i in range(5))
    server.home = f"<title>Andalusia</title>{links}".encode()
    await crawl_domain(row(server), 1, fetcher, store, NOW)
    p = purposes(store)
    assert p.count("policy_field") == 1 and p.count("candidate") == 2
    assert p.index("policy_field") < p.index("candidate")


async def test_policy_field_obeys_robots_on_final_host_and_keeps_slot(server, store, fetcher):
    server.expect_request("/robots.txt").respond_with_data("User-agent: *\nDisallow: /pol\n", content_type="text/plain")
    server.expect_request("/.well-known/security.txt").respond_with_data(
        b"Contact: mailto:a@a.org\nPolicy: %s\n" % server.url_for("/pol").encode(), content_type="text/plain")
    server.home = "".join(f"<a href='/r{i}'>Responsible Disclosure</a>" for i in range(5)).encode() + b"<title>Andalusia</title>"
    await crawl_domain(row(server), 1, fetcher, store, NOW)
    p = purposes(store)
    assert "policy_field" not in p and p.count("candidate") == 3
    assert not any(r.path == "/pol" for r, _ in server.log)


async def test_policy_field_on_other_host_fetched_without_robots_check(servers, server, store, fetcher):
    other = servers()
    other.expect_request("/pol").respond_with_data("policy", content_type="text/html")
    server.expect_request("/robots.txt").respond_with_data("User-agent: *\nDisallow: /pol\n", content_type="text/plain")
    server.expect_request("/.well-known/security.txt").respond_with_data(
        b"Contact: mailto:a@a.org\nPolicy: %s\n" % other.url_for("/pol").encode(), content_type="text/plain")
    await crawl_domain(row(server), 1, fetcher, store, NOW)
    assert "policy_field" in purposes(store) and [r.path for r, _ in other.log] == ["/pol"]


async def test_platform_policy_url_not_fetched(server, store, fetcher):
    server.expect_request("/.well-known/security.txt").respond_with_data(
        b"Contact: mailto:a@a.org\nPolicy: https://hackerone.com/\n", content_type="text/plain")
    await crawl_domain(row(server), 1, fetcher, store, NOW)
    assert "policy_field" not in purposes(store)


async def test_mismatch_stops_domain(servers, store, fetcher):
    s = servers()
    s.expect_request("/").respond_with_data(b"<title>This domain is for sale</title>" + LONG, content_type="text/html")
    assert await crawl_domain(row(s), 1, fetcher, store, NOW) == "mismatch"
    assert store.count_requests("a.org", 1) == 1


async def test_js_shell_homepage_continues(servers, store, fetcher):
    s = servers()
    s.expect_request("/").respond_with_data("<title>Loading</title><div id=root></div>", content_type="text/html")
    assert await crawl_domain(row(s), 1, fetcher, store, NOW) == "measured"


async def test_nan_name_tokens_means_no_mismatch_check(servers, store, fetcher):
    s = servers()
    s.expect_request("/").respond_with_data("<title>parked</title>", content_type="text/html")
    assert await crawl_domain(row(s, tokens=float("nan")), 1, fetcher, store, NOW) == "measured"


async def test_optout_first_request_records_nothing(server, store, live):
    ft = FakeTime()
    f = PoliteFetcher(store, frozenset({"a.org"}), live, clock=ft.clock, sleep=ft.sleep)
    assert await crawl_domain(row(server), 1, f, store, NOW) == "optout"
    assert store.count_requests("a.org", 1) == 0 and store.done_status("a.org", 1) == "optout"
    assert server.log == []


async def test_domain_spelling_is_normalized(server, store, fetcher):
    assert await crawl_domain(row(server, domain="Example.ORG."), 1, fetcher, store, NOW) == "measured"
    assert store.count_requests("example.org", 1) == 11
    assert store.done_status("example.org", 1) == "measured"
    assert store.count_requests("Example.ORG.", 1) == 0 and store.done_status("Example.ORG.", 1) is None
    assert {r.domain for r in store.requests_for("example.org", 1)} == {"example.org"}
    assert store.db.execute("SELECT COUNT(*) FROM requests WHERE domain != 'example.org'").fetchone()[0] == 0


async def test_interrupted_domain_resumes_without_repeating(server, store, fetcher):
    await crawl_domain(row(server), 1, fetcher, store, NOW)
    n = len(server.log)
    store.db.execute("DELETE FROM done")
    store.db.commit()
    assert await crawl_domain(row(server), 1, fetcher, store, NOW) == "measured"
    assert len(server.log) == n and store.count_requests("a.org", 1) == 11


def table(*rows):
    return pd.DataFrame(rows)


async def test_resume_skips_done_domains(servers, store, fetcher, frozen):
    s1, s2 = servers(), servers()
    for s in (s1, s2):
        s.expect_request("/").respond_with_data(HOME, content_type="text/html")
    df = table(row(s1, "a.org"), row(s2, "b.org"))
    assert await crawl_sector(df, 1, fetcher, store, frozen_file=frozen) == {"measured": 2}
    n1, n2 = len(s1.log), len(s2.log)
    assert await crawl_sector(df, 1, fetcher, store, frozen_file=frozen) == {"measured": 2}
    assert (len(s1.log), len(s2.log)) == (n1, n2)


async def test_limit_and_seed_sample_is_reproducible(servers, store, fetcher, live):
    rows = []
    for i in range(6):
        s = servers()
        s.expect_request("/").respond_with_data(HOME, content_type="text/html")
        rows.append(row(s, f"d{i}.org"))
    df = table(*rows)
    await crawl_sector(df, 1, fetcher, store, limit=2, seed=7)
    first = {r[0] for r in store.db.execute("SELECT domain FROM done")}
    assert len(first) == 2
    with Store(store.path.parent / "second.sqlite") as s2:
        ft = FakeTime()
        f2 = PoliteFetcher(s2, frozenset(), live, clock=ft.clock, sleep=ft.sleep)
        await crawl_sector(df, 1, f2, s2, limit=2, seed=7)
        assert {r[0] for r in s2.db.execute("SELECT domain FROM done")} == first


async def test_full_pass_refused_without_frozen_protocol(server, store, fetcher, tmp_path):
    df = table(row(server))
    missing = tmp_path / "nope" / "FROZEN.txt"
    with pytest.raises(NotFrozen):
        await crawl_sector(df, 1, fetcher, store, frozen_file=missing)
    assert server.log == []
    pilot = table(row(server, "a.org"), row(server, "b.org"))  # a limit below the table size is a pilot
    assert await crawl_sector(pilot, 1, fetcher, store, limit=1, frozen_file=missing) == {"measured": 1}


async def test_pass2_only_retries_failures_and_respects_48h(servers, store, fetcher, frozen):
    good, bad, flaky = servers(), servers(), servers()
    good.expect_request("/").respond_with_data(HOME, content_type="text/html")
    bad.expect_request("/").respond_with_data("down", status=503)
    flaky.expect_request("/").respond_with_data(HOME, content_type="text/html")
    flaky.expect_request("/.well-known/security.txt").respond_with_data("x", status=500)
    df = table(row(good, "good.org"), row(bad, "bad.org"), row(flaky, "flaky.org"))
    await crawl_sector(df, 1, fetcher, store, frozen_file=frozen)
    assert [needs_retry(store, d) for d in ("good.org", "bad.org", "flaky.org")] == [False, True, True]
    good_n = len(good.log)
    soon = datetime.now(timezone.utc) + timedelta(hours=47)
    assert await crawl_sector(df, 2, fetcher, store, frozen_file=frozen, now=soon) == {"skipped_too_early": 2}
    assert store.count_requests("bad.org", 2) == 0
    for s in (bad, flaky):
        s.clear()
        s.expect_request("/").respond_with_data(HOME, content_type="text/html")
    later = datetime.now(timezone.utc) + timedelta(hours=49)
    assert await crawl_sector(df, 2, fetcher, store, frozen_file=frozen, now=later) == {"measured": 2}
    assert store.count_requests("good.org", 2) == 0 and len(good.log) == good_n
    assert store.done_status("bad.org", 1) == "unreachable"  # the pass-1 outcome is kept


def test_needs_retry_false_for_mismatch_optout_and_unknown(store):
    store.mark_done("m.org", 1, "mismatch")
    store.mark_done("o.org", 1, "optout")
    assert not needs_retry(store, "m.org") and not needs_retry(store, "o.org") and not needs_retry(store, "never.org")


async def test_concurrent_domains_share_one_fetcher(servers, store, fetcher, frozen):
    rows = []
    for i in range(3):
        s = servers()
        s.expect_request("/").respond_with_data(HOME, content_type="text/html")
        rows.append(row(s, f"c{i}.org"))
    assert await crawl_sector(table(*rows), 1, fetcher, store, frozen_file=frozen) == {"measured": 3}
    assert all(store.count_requests(f"c{i}.org", 1) == 11 for i in range(3))


def stored(domain, url, purpose, **kw):
    base = dict(domain=domain, sector="hospital", host="localhost", url=url, purpose=purpose, pass_no=1,
                ua_kind="identified", started_at="2026-11-20T00:00:00Z", status=None, final_url=None,
                redirect_chain=[], content_type=None, body_sha256=None, body_len=0, truncated=False,
                error="cancelled", blocked=False)
    base.update(kw)
    return FetchRecord(**base)


async def test_cancelled_robots_record_is_not_reused_on_resume(server, store, fetcher):
    server.expect_request("/robots.txt").respond_with_data("User-agent: *\nDisallow: /\n", content_type="text/plain")
    server.home = LINKED
    store.record(stored("a.org", server.url_for("/robots.txt"), "robots"), None)
    await crawl_domain(row(server), 1, fetcher, store, NOW)
    assert purposes(store).count("robots") == 2
    assert "probe" not in purposes(store) and "candidate" not in purposes(store)


async def test_cancelled_homepage_record_is_not_unreachable_on_resume(server, store, fetcher):
    store.record(stored("a.org", server.url_for("/"), "homepage"), None)
    assert await crawl_domain(row(server), 1, fetcher, store, NOW) == "measured"
    assert purposes(store).count("homepage") == 2


async def test_blocked_identified_then_cancelled_browser_retry_is_requested_again(servers, store, fetcher):
    s = servers()
    s.expect_request("/").respond_with_handler(
        lambda r: Response(HOME, 200, content_type="text/html") if r.headers["User-Agent"].startswith("Mozilla/")
        else Response("no", 403))
    store.record(stored("a.org", s.url_for("/"), "homepage", status=403, blocked=True, error=None,
                        final_url=s.url_for("/")), b"no")
    store.record(stored("a.org", s.url_for("/"), "homepage", ua_kind="browser"), None)
    assert await crawl_domain(row(s), 1, fetcher, store, NOW) == "measured"
    assert store.requests_for("a.org", 1)[-1].ua_kind == "browser"


async def test_malformed_policy_url_does_not_crash(server, store, fetcher):
    server.expect_request("/.well-known/security.txt").respond_with_data(
        b"Contact: mailto:a@a.org\nPolicy: https://[oops/\n", content_type="text/plain")
    assert await crawl_domain(row(server), 1, fetcher, store, NOW) == "measured"
    assert "policy_field" not in purposes(store)


async def test_one_failing_domain_does_not_stop_the_sector(servers, store, fetcher, frozen, monkeypatch, caplog):
    s1, s2 = servers(), servers()
    for s in (s1, s2):
        s.expect_request("/").respond_with_data(HOME, content_type="text/html")
    real = crawl_mod.crawl_domain

    async def flaky(r, *a, **k):
        if r["domain"] == "bad.org":
            raise ValueError("boom")
        return await real(r, *a, **k)

    monkeypatch.setattr(crawl_mod, "crawl_domain", flaky)
    got = await crawl_sector(table(row(s1, "bad.org"), row(s2, "good.org")), 1, fetcher, store, frozen_file=frozen)
    assert got == {"measured": 1, "error": 1}
    assert store.done_status("bad.org", 1) is None and store.done_status("good.org", 1) == "measured"
    assert "bad.org" in caplog.text


async def test_not_live_still_aborts_the_run(server, store, tmp_path, frozen):
    f = PoliteFetcher(store, frozenset(), tmp_path / "missing", clock=(ft := FakeTime()).clock, sleep=ft.sleep)
    with pytest.raises(NotLive):
        await crawl_sector(table(row(server)), 1, f, store, frozen_file=frozen)


async def test_limit_at_or_above_table_size_is_a_full_pass(server, store, fetcher, tmp_path):
    missing = tmp_path / "nope" / "FROZEN.txt"
    with pytest.raises(NotFrozen):
        await crawl_sector(table(row(server)), 1, fetcher, store, limit=99999, frozen_file=missing)
    with pytest.raises(NotFrozen):
        await crawl_sector(table(row(server)), 1, fetcher, store, limit=1, frozen_file=missing)
    assert server.log == []


async def test_robots_404_allows_probes(server, store, fetcher):
    await crawl_domain(row(server), 1, fetcher, store, NOW)
    assert purposes(store).count("probe") == 6


async def test_robots_503_disallows_pages_but_files_still_requested(server, store, fetcher):
    server.expect_request("/robots.txt").respond_with_data("down", status=503)
    server.home = LINKED
    await crawl_domain(row(server), 1, fetcher, store, NOW)
    assert purposes(store) == CORE5


async def test_robots_timeout_disallows_pages_but_files_still_requested(server, store, fetcher, monkeypatch):
    import time

    monkeypatch.setattr(config, "READ_TIMEOUT", 0.3)

    def slow(request):
        time.sleep(0.8)
        return Response("late")

    server.expect_request("/robots.txt").respond_with_handler(slow)
    server.home = LINKED
    await crawl_domain(row(server), 1, fetcher, store, NOW)
    assert purposes(store) == CORE5
    assert store.requests_for("a.org", 1)[1].error == "timeout"


async def test_robots_failure_does_not_block_policy_on_another_host(servers, server, store, fetcher):
    other = servers()
    other.expect_request("/pol").respond_with_data("policy", content_type="text/html")
    server.expect_request("/robots.txt").respond_with_data("down", status=500)
    server.expect_request("/.well-known/security.txt").respond_with_data(
        b"Contact: mailto:a@a.org\nPolicy: %s\n" % other.url_for("/pol").encode(), content_type="text/plain")
    await crawl_domain(row(server), 1, fetcher, store, NOW)
    assert "policy_field" in purposes(store)


async def test_homepage_redirect_to_optout_host_is_optout(servers, server, store, live):
    a = servers()
    a.expect_request("/").respond_with_data("", status=301, headers={"Location": server.url_for("/")})
    f = PoliteFetcher(store, frozenset({server.host}), live, clock=(ft := FakeTime()).clock, sleep=ft.sleep)
    assert await crawl_domain(row(a), 1, f, store, NOW) == "optout"
    assert store.done_status("a.org", 1) == "optout"


async def test_third_party_optout_skips_only_that_url(server, store, live):
    server.home = f"<title>Andalusia</title><a href='{at(server, 'third.test', '/x')}'>Responsible Disclosure</a>".encode()
    f = PoliteFetcher(store, frozenset({"third.test"}), live, clock=(ft := FakeTime()).clock, sleep=ft.sleep)
    assert await crawl_domain(row(server), 1, f, store, NOW) == "measured"
    assert store.done_status("a.org", 1) == "measured"
    assert "candidate" not in purposes(store) and purposes(store).count("probe") == 6
    assert not any(r.headers.get("Host", "").startswith("third.test") for r, _ in server.log)


async def test_policy_url_already_fetched_is_not_requested_again(server, store, fetcher):
    server.expect_request("/.well-known/security.txt").respond_with_data(
        b"Contact: mailto:a@a.org\nPolicy: %s\n" % server.url_for("/").encode(), content_type="text/plain")
    server.home = "".join(f"<a href='/r{i}'>Responsible Disclosure</a>" for i in range(5)).encode() + b"<title>Andalusia</title>"
    await crawl_domain(row(server), 1, fetcher, store, NOW)
    p = purposes(store)
    assert "policy_field" not in p and p.count("candidate") == 3
    assert [r.path for r, _ in server.log].count("/") == 1


# ---- A1: record before sending; store errors stop the run

CHILD = """
import asyncio, socket, sys
from datetime import datetime, timezone
_gai = socket.getaddrinfo
def _local(host, port, family=0, type=0, proto=0, flags=0):  # the tests' strict resolver: only *.test names
    if not (host.decode() if isinstance(host, bytes) else str(host)).endswith(".test"):
        raise socket.gaierror(socket.EAI_NONAME, "refused")
    return _gai("127.0.0.1", port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = _local
from noroshi import config
from noroshi.crawl import crawl_domain
from noroshi.fetch import PoliteFetcher
from noroshi.store import Store
config.MIN_DELAY_SECONDS = 0.0
db, live, url = sys.argv[1:4]
row = {"sector": "hospital", "domain": "a.org", "start_url": url, "name_tokens": "andalusia"}
with Store(db) as store:
    asyncio.run(crawl_domain(row, 1, PoliteFetcher(store, frozenset(), live), store, datetime.now(timezone.utc)))
"""


async def test_hard_kill_between_send_and_record_never_allows_a_13th_request(server, store, fetcher, live):
    import subprocess
    import sys
    import threading
    import time

    links = "".join(f"<a href='/r{i}'>Responsible Disclosure</a>" for i in range(3))
    server.home = f"<title>Andalusia</title>{links}".encode()  # 5 core files, 3 candidates, then probes
    started = threading.Event()

    def slow(request):  # the 12th request: the crawl is killed while it waits for the answer
        started.set()
        time.sleep(3)
        return Response("late", status=404)

    server.expect_request("/vulnerability-disclosure").respond_with_handler(slow)
    child = subprocess.Popen([sys.executable, "-c", CHILD, str(store.path), str(live), server.url_for("/")])
    try:
        while not started.wait(0.1):
            assert child.poll() is None, "the child crawl ended before its 12th request"
    finally:
        child.kill()
        child.wait()
    time.sleep(3.5)  # the slow handler finishes and logs the 12th request
    assert len(server.log) == 12
    assert store.count_requests("a.org", 1) == 12  # the killed attempt left its in-flight row
    assert await crawl_domain(row(server), 1, fetcher, store, NOW) == "measured"  # the restart
    assert len(server.log) == 12  # never a 13th request in the pass


async def test_locked_store_aborts_the_run_before_any_request(servers, tmp_path, live, frozen, monkeypatch):
    import sqlite3

    from noroshi import store as store_mod

    monkeypatch.setattr(store_mod, "BUSY_TIMEOUT_SECONDS", 0.2, raising=False)
    s = servers()
    s.expect_request("/").respond_with_data(HOME, content_type="text/html")
    with Store(tmp_path / "locked.sqlite") as st:
        other = sqlite3.connect(tmp_path / "locked.sqlite")
        other.execute("BEGIN IMMEDIATE")  # another writer holds the write lock
        try:
            f = PoliteFetcher(st, frozenset(), live, clock=(ft := FakeTime()).clock, sleep=ft.sleep)
            with pytest.raises(sqlite3.OperationalError):
                await crawl_sector(table(row(s)), 1, f, st, frozen_file=frozen)
        finally:
            other.rollback()
            other.close()
    assert s.log == []  # nothing was sent, so nothing went unrecorded


@pytest.mark.parametrize("method,exc", [("mark_done", "sqlite"), ("get_blob", "os")])
async def test_store_failures_propagate_out_of_the_sector(server, store, fetcher, frozen, monkeypatch, method, exc):
    import sqlite3

    def boom(self, *a, **k):
        raise sqlite3.OperationalError("disk I/O error") if exc == "sqlite" else OSError("disk full")

    monkeypatch.setattr(Store, method, boom)
    with pytest.raises(sqlite3.OperationalError if exc == "sqlite" else OSError):
        await crawl_sector(table(row(server)), 1, fetcher, store, frozen_file=frozen)


async def test_in_flight_homepage_record_is_not_an_outcome_on_resume(server, store, fetcher):
    store.record(stored("a.org", server.url_for("/"), "homepage", error="in_flight"), None)
    assert await crawl_domain(row(server), 1, fetcher, store, NOW) == "measured"
    assert purposes(store).count("homepage") == 2



# ---- A2: one site, many listed domains: reuse within a pass, and the site cap

def hub_server(servers, rest=None):
    """One local server playing a hub site (hub.test) and any number of alias*.test sites that 301 to it."""
    import re

    s = servers()

    def handle(request):
        host = request.host.split(":")[0]
        if host.startswith("alias"):
            return Response("", status=301, headers={"Location": at(s, "hub.test")})
        if request.path == "/":
            return Response(HOME, content_type="text/html")
        return rest(request) if rest else Response("not here", status=404)

    s.expect_request(re.compile(".*")).respond_with_handler(handle)
    return s


def host_hits(server, host):
    return [r.path for r, _ in server.log if r.host.split(":")[0] == host]


async def test_many_listed_domains_redirecting_to_one_host_respect_the_site_cap(servers, store, fetcher, frozen):
    s = hub_server(servers, rest=lambda request: Response("busy", status=503))  # nothing there can be reused
    df = table(*[row(s, f"alias{i:02d}.test") | {"start_url": at(s, f"alias{i:02d}.test")} for i in range(22)])
    assert await crawl_sector(df, 1, fetcher, store, frozen_file=frozen) == {"measured": 22}
    hub = host_hits(s, "hub.test")
    assert hub.count("/") == 22  # one uncounted redirect hop per listed domain
    assert len(hub) - hub.count("/") == config.MAX_REQUESTS_PER_SITE  # every other request: at most 12 in the pass
    assert store.count_site_requests("hub.test", 1) == 12


async def test_a_url_measured_earlier_in_the_pass_is_reused_not_requested(servers, store, fetcher):
    s = hub_server(servers)
    a1 = row(s, "alias1.test") | {"start_url": at(s, "alias1.test")}
    a2 = row(s, "alias2.test") | {"start_url": at(s, "alias2.test")}
    assert await crawl_domain(a1, 1, fetcher, store, NOW) == "measured"
    n = len(host_hits(s, "hub.test"))
    assert await crawl_domain(a2, 1, fetcher, store, NOW) == "measured"
    assert len(host_hits(s, "hub.test")) == n + 1  # only the second homepage's redirect hop
    recs = store.requests_for("alias2.test", 1)
    assert [r.reused for r in recs] == [False] + [True] * (len(recs) - 1)
    assert [r.purpose for r in recs] == purposes(store, "alias1.test")
    assert store.count_requests("alias2.test", 1) == 1  # reused copies count toward no cap


async def test_third_party_site_at_its_cap_is_skipped_and_the_domain_continues(server, store, fetcher):
    for i in range(config.MAX_REQUESTS_PER_SITE):
        store.record(stored(f"other{i}.org", f"https://third.test/{i}", "probe", site="third.test", error="timeout"), None)
    server.home = f"<title>Andalusia</title><a href='{at(server, 'third.test', '/vdp')}'>Report a vulnerability</a>".encode()
    assert await crawl_domain(row(server), 1, fetcher, store, NOW) == "measured"
    assert "candidate" not in purposes(store) and purposes(store).count("probe") == 6


async def test_link_no_request_can_be_built_from_is_skipped(server, store, fetcher):
    server.home = b"<title>Andalusia</title><a href='http://s1.test:99999/vdp'>Report a vulnerability</a>"
    assert await crawl_domain(row(server), 1, fetcher, store, NOW) == "measured"
    assert "candidate" not in purposes(store) and purposes(store).count("probe") == 6


# ---- A4: a third party's refusal leaves the measured site on the identified signature

async def test_third_party_refusal_keeps_the_measured_site_identified(servers, server, store, fetcher):
    third = servers()
    third.expect_request("/psirt-info").respond_with_data("<html>Access Denied</html>", status=403)
    server.home = f"<title>Andalusia</title><a href='{third.url_for('/psirt-info')}'>Product Security</a>".encode()
    assert await crawl_domain(row(server, sector="device"), 1, fetcher, store, NOW) == "measured"
    recs = store.requests_for("a.org", 1)
    assert [r.ua_kind for r in recs if r.purpose == "candidate"] == ["identified", "browser"]
    assert {r.ua_kind for r in recs if r.purpose == "probe"} == {"identified"}


# ---- A7: parked and third-party final sites

PARK = b"Buy this premium domain name today. Make an offer or lease to own with flexible payments. " * 5


@pytest.mark.parametrize("final_host", ["www.sedo.com", "www.facebook.com", "hugedomains.com"])
async def test_final_site_on_a_parking_or_shared_platform_is_mismatch(servers, store, fetcher, local_dns, final_host):
    local_dns.add(final_host)
    s, target = servers(), servers()
    target.expect_request("/").respond_with_data(HOME + LONG, content_type="text/html")
    s.expect_request("/").respond_with_data("", status=301, headers={"Location": at(target, final_host)})
    assert await crawl_domain(row(s), 1, fetcher, store, NOW) == "mismatch"
    assert purposes(store) == ["homepage"]


@pytest.mark.parametrize("marker", ["Domain is for sale", "This domain is for sale!", "Buy this domain now",
                                    "Premium domain for sale", "This domain is parked free", "Domain Parking by X"])
@pytest.mark.parametrize("where", ["title", "body"])
async def test_for_sale_markers_are_mismatch(servers, store, fetcher, marker, where):
    s = servers()
    page = (f"<title>{marker}</title><p>Andalusia Hospital</p>" if where == "title"
            else f"<title>Andalusia Hospital</title><p>{marker}</p>").encode() + LONG
    s.expect_request("/").respond_with_data(page, content_type="text/html")
    assert await crawl_domain(row(s), 1, fetcher, store, NOW) == "mismatch"


async def test_for_sale_marker_ends_a_federal_domain_too(servers, store, fetcher):
    s = servers()
    s.expect_request("/").respond_with_data(b"<title>x</title><p>This domain is for sale.</p>", content_type="text/html")
    assert await crawl_domain(row(s, sector="federal", tokens=""), 1, fetcher, store, NOW) == "mismatch"


def test_page_that_only_prints_the_domain_name_does_not_match():
    page = (b"<title>riverbendhealth-parked.org is for sale</title><p>riverbendhealth-parked.org is for sale. "
            + PARK + b"</p>")
    assert homepage_matches({"riverbend"}, page, HEALTH_TERMS)  # without the domain the old check matched
    assert not homepage_matches({"riverbend"}, page, HEALTH_TERMS, domain="riverbendhealth-parked.org")
    real = b"<title>Welcome</title><p>RiverbendHealth.org: Riverbend Health cares for you. " + PARK + b"</p>"
    assert homepage_matches({"riverbend"}, real, HEALTH_TERMS, domain="riverbendhealth.org")


async def test_reviewers_for_sale_page_without_a_marker_is_left_to_the_homepage_review(servers, store, fetcher,
                                                                                       local_dns):
    """Post-pilot X8: this page carries none of the for-sale markers; it ended mismatch only by the name-word check,
    which no longer decides at crawl time. Whether it is the organization's site is decided after the crawl."""
    local_dns.add("riverbendhealth-parked.org")
    s = servers()
    page = (b"<title>riverbendhealth-parked.org is for sale</title><p>riverbendhealth-parked.org is for sale. "
            + PARK + b"</p>")
    s.expect_request("/").respond_with_data(page, content_type="text/html")
    r = row(s, "riverbendhealth-parked.org", tokens="riverbend") | {"start_url": at(s, "riverbendhealth-parked.org")}
    assert not for_sale(page) and not homepage_matches({"riverbend"}, page, HEALTH_TERMS, "riverbendhealth-parked.org")
    assert await crawl_domain(r, 1, fetcher, store, NOW) == "measured"


# ---- A10: a 429 from the measured site ends the domain for the pass

async def test_429_from_the_measured_site_ends_the_domain_blocked(server, store, fetcher):
    server.expect_request("/.well-known/security.txt").respond_with_data("slow down", status=429)
    assert await crawl_domain(row(server), 1, fetcher, store, NOW) == "blocked"
    assert purposes(store) == ["homepage", "robots", "sectxt_wellknown"]
    assert [r.path for r, _ in server.log][-1] == "/.well-known/security.txt"


async def test_429_from_a_third_party_does_not_end_the_domain(servers, server, store, fetcher):
    third = servers()
    third.expect_request("/vdp").respond_with_data("slow down", status=429)
    server.home = f"<title>Andalusia</title><a href='{third.url_for('/vdp')}'>Report a vulnerability</a>".encode()
    assert await crawl_domain(row(server), 1, fetcher, store, NOW) == "measured"
    assert purposes(store).count("probe") == 6


# ---- A9: a local outage does not burn the pass

def down_rows(n, prefix="down"):
    return table(*[{"sector": "federal", "domain": f"{prefix}{i}.org", "start_url": f"https://{prefix}{i}.invalid/",
                    "name_tokens": ""} for i in range(n)])


async def test_twenty_network_failures_and_a_failed_control_abort_and_unmark(store, fetcher, frozen):
    from noroshi.crawl import LocalOutage

    calls = []

    async def control():
        calls.append(1)
        return False

    with pytest.raises(LocalOutage):
        await crawl_sector(down_rows(21), 1, fetcher, store, frozen_file=frozen, control=control)
    assert calls == [1]
    assert store.db.execute("SELECT COUNT(*) FROM done").fetchone()[0] == 0
    errors = [r[0] for r in store.db.execute("SELECT error FROM requests WHERE purpose = 'homepage'")]
    # both host forms of the 20 counted domains (post-pilot X2), and any first form left waiting for its other form
    assert errors.count("outage") >= 40 and set(errors) <= {"outage", "cancelled"}

    async def back():
        return True

    # a rerun of the pass: an outage record is not an outcome, so every homepage is requested again
    await crawl_sector(down_rows(21), 1, fetcher, store, frozen_file=frozen, control=back)
    outage = {r[0] for r in store.db.execute("SELECT domain FROM requests WHERE error = 'outage'")}
    assert len(outage) >= 20 and all(sum(r.error == "dns" for r in store.requests_for(d, 1)) == 2  # both forms again
                                     and store.done_status(d, 1) == "unreachable" for d in outage)


async def test_control_success_resets_the_count_and_the_run_goes_on(store, fetcher, frozen):
    calls = []

    async def control():
        calls.append(1)
        return True

    assert await crawl_sector(down_rows(25), 1, fetcher, store, frozen_file=frozen, control=control) == {"unreachable": 25}
    assert calls == [1]


async def test_control_url_comes_from_the_live_file(httpserver, tmp_path):
    from noroshi.crawl import live_control

    live = tmp_path / "LIVE"
    httpserver.expect_request("/info").respond_with_data("the study page")
    live.write_text(f"{httpserver.url_for('/info')} 2026-11-16T00:00:00Z\n")
    assert await live_control(live)() is True
    live.write_text("go\n")  # no URL in the LIVE file: the control cannot pass
    assert await live_control(live)() is False
    live.write_text("https://info.invalid/page\n")  # a control that cannot be reached
    assert await live_control(live)() is False



# ---- A13: the freeze gate checks the hash

def test_freeze_gate_needs_an_existing_commit_and_an_unchanged_protocol(tmp_path):
    from noroshi.crawl import require_frozen

    frozen = freeze(tmp_path / "repo")
    require_frozen(None, 10, frozen)  # the commit exists and PROTOCOL.md is as committed
    require_frozen(3, 10, tmp_path / "missing" / "FROZEN.txt")  # a pilot needs no freeze
    md = frozen.parent / "PROTOCOL.md"
    md.write_text(md.read_text() + "a late edit\n")
    with pytest.raises(NotFrozen, match="PROTOCOL.md"):
        require_frozen(None, 10, frozen)
    md.write_text("# Protocol\n\nFrozen for the test.\n")
    require_frozen(None, 10, frozen)
    for text in ("x\n", "", "0123456789abcdef0123456789abcdef01234567\n"):
        frozen.write_text(text)
        with pytest.raises(NotFrozen):
            require_frozen(None, 10, frozen)
    with pytest.raises(NotFrozen):
        require_frozen(None, 10, tmp_path / "missing" / "FROZEN.txt")


async def test_listed_domains_crawled_at_once_reuse_what_the_first_measured(servers, store, fetcher, frozen):
    s = hub_server(servers)
    df = table(*[row(s, f"alias{i}.test") | {"start_url": at(s, f"alias{i}.test")} for i in range(3)])
    assert await crawl_sector(df, 1, fetcher, store, frozen_file=frozen) == {"measured": 3}
    hub = [p for p in host_hits(s, "hub.test") if p != "/"]
    assert len(hub) == len(set(hub))  # each hub URL was sent once in the pass, whoever asked first
    assert sum(r.reused for d in range(3) for r in store.requests_for(f"alias{d}.test", 1)) == 2 * len(hub)


# ---- rulings round, R1: federal portals are third parties for the hospital sector only

@pytest.mark.parametrize("name,sector", [("va.gov", "federal"), ("medicare.gov", "federal"), ("ihs.gov", "ehr")])
async def test_federal_portal_listed_in_its_own_sector_is_measured(servers, store, fetcher, local_dns, name, sector):
    local_dns.add(name)
    s = servers()
    s.expect_request("/").respond_with_data(HOME, content_type="text/html")
    r = row(s, name, sector=sector, tokens="") | {"start_url": at(s, name)}
    assert await crawl_domain(r, 1, fetcher, store, NOW) == "measured"


@pytest.mark.parametrize("sector,final_host,status", [
    ("federal", "www.va.gov", "measured"), ("ehr", "www.ihs.gov", "measured"),
    ("hospital", "www.va.gov", "mismatch"), ("device", "www.ny.gov", "mismatch")])
async def test_redirect_into_a_portal_depends_on_the_sector(servers, store, fetcher, local_dns, sector, final_host, status):
    local_dns.add(final_host)
    s, target = servers(), servers()
    target.expect_request("/").respond_with_data(HOME, content_type="text/html")
    s.expect_request("/").respond_with_data("", status=301, headers={"Location": at(target, final_host)})
    assert await crawl_domain(row(s, sector=sector, tokens=""), 1, fetcher, store, NOW) == status


# ---- rulings round, R4: a listed domain's own homepage request is never refused by the site cap

async def test_listed_hub_still_sends_its_homepage_after_others_used_its_site_cap(servers, store, fetcher, frozen):
    s = hub_server(servers, rest=lambda request: Response("busy", status=503))  # nothing there can be reused
    aliases = table(*[row(s, f"alias{i}.test") | {"start_url": at(s, f"alias{i}.test")} for i in range(3)])
    assert await crawl_sector(aliases, 1, fetcher, store, frozen_file=frozen) == {"measured": 3}
    assert store.count_site_requests("hub.test", 1) == config.MAX_REQUESTS_PER_SITE
    before = host_hits(s, "hub.test")
    hub = row(s, "hub.test") | {"start_url": at(s, "hub.test")}
    assert await crawl_domain(hub, 1, fetcher, store, NOW) == "measured"
    assert host_hits(s, "hub.test") == before + ["/"]  # its homepage, and no other new request
    assert purposes(store, "hub.test") == ["homepage"]


# ---- residuals round, S1: remove the full domain string before matching, never the bare label

COMPANY_TABLES = ("device", "icsma", "ehr")
FILLER = "0123456789 " * 28  # 308 characters of neutral filler: no letters, so no name token can hide in it


def committed_tokens(domain, sector):
    t = pd.read_csv(CHECKOUT / "data" / "frames" / f"{sector}_domains.csv", dtype=str, keep_default_na=False)
    return set(t.set_index("domain").loc[domain, "name_tokens"].split())


@pytest.mark.parametrize("domain,sector,name", [
    ("medtronic.com", "icsma", "Medtronic"), ("medtronic.com", "device", "Medtronic"),
    ("philips.com", "icsma", "Philips"), ("athenahealth.com", "ehr", "athenahealth"),
    ("fire.ly", "ehr", "Firely"), ("pydicom.github.io", "icsma", "pydicom")])
def test_an_ordinary_company_homepage_matches_its_own_domain(domain, sector, name):
    page = (f"<title>{name} | Home</title><p>Welcome to {name}. Visit www.{domain} or write to info@{domain}. "
            f"{FILLER}</p>").encode()
    assert homepage_matches(committed_tokens(domain, sector), page, (), domain)


def test_every_committed_company_row_matches_a_page_titled_with_its_name_tokens():
    failed = []
    for sector in COMPANY_TABLES:
        t = pd.read_csv(CHECKOUT / "data" / "frames" / f"{sector}_domains.csv", dtype=str, keep_default_na=False)
        for domain, tokens in zip(t.domain, t.name_tokens):
            page = f"<title>{tokens}</title><p>{FILLER}</p>".encode()
            if not homepage_matches(set(tokens.split()), page, (), domain):
                failed.append(f"{sector}:{domain}")
    assert failed == [], f"{len(failed)} committed company rows would end mismatch: {failed[:8]}"


# ---- residuals round, S3: waiting for a site's slot never counts toward the time limit, at any hop

async def test_many_aliases_queueing_for_a_slow_hub_all_end_measured(servers, store, live, frozen):
    import re

    ft = FakeTime()
    s = servers()

    def handle(request):
        ft.now += 1.5  # a slow site: every answer takes 1.5 s of the fake clock
        host = request.host.split(":")[0]
        if host.startswith("alias"):
            return Response("", status=301, headers={"Location": at(s, "hub.test")})
        if request.path == "/":
            return Response(HOME, content_type="text/html")
        return Response("not here", status=404)

    s.expect_request(re.compile(".*")).respond_with_handler(handle)
    f = PoliteFetcher(store, frozenset(), live, clock=ft.clock, sleep=ft.sleep)
    df = table(*[row(s, f"alias{i:02d}.test") | {"start_url": at(s, f"alias{i:02d}.test")} for i in range(22)])
    assert await crawl_sector(df, 1, f, store, frozen_file=frozen) == {"measured": 22}
    assert store.db.execute("SELECT COUNT(*) FROM requests WHERE error = 'timeout'").fetchone()[0] == 0
    assert ft.now - 1000.0 > config.TOTAL_TIMEOUT  # the hub queue really was longer than the limit


# ---- residuals round, S4: a 429 stops every request to that site for the pass

async def test_after_a_hub_answers_429_aliases_send_it_nothing_more_and_end_blocked(servers, store, fetcher, frozen):
    s = hub_server(servers)
    s.clear()
    import re

    def handle(request):
        if request.host.split(":")[0].startswith("alias"):
            return Response("", status=301, headers={"Location": at(s, "hub.test")})
        return Response("slow down", status=429)

    s.expect_request(re.compile(".*")).respond_with_handler(handle)
    df = table(*[row(s, f"alias{i}.test") | {"start_url": at(s, f"alias{i}.test")} for i in range(3)])
    assert await crawl_sector(df, 1, fetcher, store, frozen_file=frozen) == {"blocked": 3}
    assert host_hits(s, "hub.test") == ["/"]  # one request, the one that got the 429
    homes = [r for i in range(3) for r in store.requests_for(f"alias{i}.test", 1)]
    assert sorted(str(r.status or r.error) for r in homes) == ["429", "rate_limited", "rate_limited"]
    assert all(r.blocked for r in homes)


async def test_third_party_candidate_on_a_rate_limited_site_is_skipped(servers, server, store, fetcher):
    third = servers()
    third.expect_request("/vdp").respond_with_data("slow down", status=429)
    await fetcher.fetch("other.org", "hospital", third.url_for("/vdp"), "candidate", 1)  # third answers 429 once
    server.home = f"<title>Andalusia</title><a href='{third.url_for('/vdp')}'>Report a vulnerability</a>".encode()
    assert await crawl_domain(row(server), 1, fetcher, store, NOW) == "measured"
    assert "candidate" not in purposes(store) and purposes(store).count("probe") == 6
    assert len(third.log) == 1


# ---- residuals round, S6: the outage stop

async def test_a_failure_that_lands_after_the_outage_stop_is_outage_and_requested_again(store, fetcher):
    import asyncio

    from noroshi.crawl import LocalOutage, OutageWatch

    calls = []

    async def control():
        calls.append(1)
        if len(calls) > 1:
            await asyncio.Event().wait()  # a second control request would hang until the run is cancelled
        return False

    watch = OutageWatch(store, 1, control)
    rows = down_rows(21).to_dict("records")
    for r in rows[:19]:
        assert await crawl_domain(r, 1, fetcher, store, NOW, watch=watch) == "unreachable"
    with pytest.raises(LocalOutage):
        await crawl_domain(rows[19], 1, fetcher, store, NOW, watch=watch)  # the trip
    late = rows[20]  # the reviewer's site59.test: its homepage fails after the trip
    with pytest.raises(LocalOutage):
        await asyncio.wait_for(crawl_domain(late, 1, fetcher, store, NOW, watch=watch), 2)
    assert calls == [1]  # one control request per trip
    assert [r.error for r in store.requests_for(late["domain"], 1)] == ["outage"]
    assert store.done_status(late["domain"], 1) is None

    async def back():
        return True

    rerun = OutageWatch(store, 1, back)
    assert await crawl_domain(late, 1, fetcher, store, NOW, watch=rerun) == "unreachable"
    assert store.count_requests(late["domain"], 1) == 3  # requested again on the rerun, at both forms (X2)


async def test_each_control_request_is_logged_with_its_url_and_result(httpserver, tmp_path):
    from noroshi.crawl import live_control

    lines = []
    live = tmp_path / "LIVE"
    httpserver.expect_request("/info").respond_with_data("the study page")
    live.write_text(f"{httpserver.url_for('/info')}\n")
    assert await live_control(live, log=lines.append)() is True
    live.write_text("http://info.test:1/study\n")  # nothing listens there
    assert await live_control(live, log=lines.append)() is False
    assert httpserver.url_for("/info") in lines[0] and "ok" in lines[0]
    assert "http://info.test:1/study" in lines[1] and "failed" in lines[1]


# ---- closing fixes, C2: only a real answer is a measurement

@pytest.mark.parametrize("status", [204, 202])
async def test_a_homepage_answering_anything_but_200_is_unreachable(servers, store, fetcher, status):
    s = servers()
    s.expect_request("/").respond_with_data("", status=status)
    assert await crawl_domain(row(s), 1, fetcher, store, NOW) == "unreachable"
    assert purposes(store) == ["homepage"] and len(s.log) == 1


async def test_a_waf_challenge_answering_202_ends_blocked_after_the_browser_retry(servers, store, fetcher):
    import re

    s = servers()
    s.expect_request(re.compile(".*")).respond_with_data("<html>checking</html>", status=202,
                                                         headers={"x-amzn-waf-action": "challenge"})
    assert await crawl_domain(row(s), 1, fetcher, store, NOW) == "blocked"
    recs = store.requests_for("a.org", 1)
    assert [(r.purpose, r.status, r.blocked, r.ua_kind) for r in recs] == [
        ("homepage", 202, True, "identified"), ("homepage", 202, True, "browser")]
    assert [r.headers["User-Agent"] == config.UA_BROWSER for r, _ in s.log] == [False, True]


async def test_a_202_answer_is_not_reused_for_another_listed_domain(servers, store, fetcher):
    s = hub_server(servers, rest=lambda request: Response("<html>wait</html>", status=202))
    first, second = (row(s, f"alias{i}.test") | {"start_url": at(s, f"alias{i}.test")} for i in (1, 2))
    assert await crawl_domain(first, 1, fetcher, store, NOW) == "measured"
    sent = host_hits(s, "hub.test")
    assert sent == ["/", "/robots.txt", "/.well-known/security.txt", "/security.txt", "/cms-hpt.txt"]
    assert await crawl_domain(second, 1, fetcher, store, NOW) == "measured"
    assert host_hits(s, "hub.test")[len(sent):] == sent  # every request was sent again: none was an answer
    assert not any(r.reused for r in store.requests_for("alias2.test", 1))


# ---- post-pilot X8: at crawl time a domain ends mismatch only for the deterministic reasons

PILOT = Path(__file__).parent / "fixtures" / "pilot"


async def test_a_brand_homepage_whose_listed_name_differs_is_measured(servers, store, fetcher):
    """predilife.com, listed as "Statlife" (pilot #484): its own homepage was a false mismatch."""
    s = servers()
    s.expect_request("/").respond_with_data((PILOT / "predilife_home.html").read_bytes(), content_type="text/html")
    r = row(s, "predilife.com", sector="device", tokens="statlife")
    assert await crawl_domain(r, 1, fetcher, store, NOW) == "measured"
    assert purposes(store, "predilife.com")[:2] == ["homepage", "robots"]


async def test_a_page_naming_none_of_the_name_words_is_measured(servers, store, fetcher):
    s = servers()
    s.expect_request("/").respond_with_data(b"<title>Riverside Imaging</title>" + LONG, content_type="text/html")
    assert await crawl_domain(row(s, sector="device", tokens="andalusia"), 1, fetcher, store, NOW) == "measured"


def emercy(servers, final="ww547"):
    """e-mercy.com in the pilot (#14, #48): the identified homepage was sent to a ww<digits> parking host that
    answered 401; the browser retry got the parking script stub at www.e-mercy.com."""
    import re

    s = servers()
    stub = (PILOT / "emercy_home_browser.html").read_bytes()

    def handle(request):
        host = request.host.split(":")[0]
        if host == "www.e-mercy.com" and not request.headers["User-Agent"].startswith("Mozilla/"):
            return Response("", status=302, headers={"Location": f"http://{final}.e-mercy.com:{s.port}/?tkn=19vnMT3S"})
        if host == f"{final}.e-mercy.com":
            return Response("Forbidden", status=401, content_type="text/html; charset=utf-8")
        return Response(stub, content_type="text/html; charset=utf-8")

    s.expect_request(re.compile(".*")).respond_with_handler(handle)
    return row(s, "e-mercy.com") | {"start_url": f"http://www.e-mercy.com:{s.port}/"}


async def test_a_homepage_sent_to_a_ww_digits_host_of_the_listed_domain_is_parked(servers, store, fetcher, local_dns):
    local_dns.update({"www.e-mercy.com", "ww547.e-mercy.com"})
    assert await crawl_domain(emercy(servers), 1, fetcher, store, NOW) == "mismatch"
    recs = store.requests_for("e-mercy.com", 1)
    assert [(r.purpose, r.status, r.ua_kind) for r in recs] == [("homepage", 401, "identified"),
                                                                ("homepage", 200, "browser")]


@pytest.mark.parametrize("host", ["ww2.a.org", "ww.a.org", "www2.a.org", "ww2.b.org"])
async def test_a_start_host_alone_is_never_parked(servers, store, fetcher, local_dns, host):
    """Post-pilot XR5: a listed start host of the form ww<digits>.<listed domain> (ww2.westernstatehospital.org) is
    not a mismatch by the host alone."""
    local_dns.add(host)
    s = servers()
    s.expect_request("/").respond_with_data(HOME + LONG, content_type="text/html")
    r = row(s, "a.org") | {"start_url": at(s, host)}
    assert await crawl_domain(r, 1, fetcher, store, NOW) == "measured"


@pytest.mark.parametrize("target,status", [("ww2.a.org", "mismatch"), ("ww.a.org", "measured"),
                                           ("www2.a.org", "measured"), ("ww2.b.org", "measured")])
async def test_a_redirect_to_a_ww_digits_host_of_the_listed_domain_is_parked(servers, store, fetcher, local_dns, target,
                                                                            status):
    import re

    local_dns.update({"www.a.org", target})
    s = servers()

    def handle(request):
        if request.host.split(":")[0] == "www.a.org":
            return Response("", status=302, headers={"Location": at(s, target, "/?tkn=19vnMT3S")})
        return Response(HOME + LONG, content_type="text/html")

    s.expect_request(re.compile(".*")).respond_with_handler(handle)
    r = row(s, "a.org") | {"start_url": at(s, "www.a.org")}
    assert await crawl_domain(r, 1, fetcher, store, NOW) == status


# ---- post-pilot X3: a 403 on one path is not a refusal of the signature

THREE = {  # pilot: the identified attempt and its browser retry got the same 403 at /.well-known/security.txt
    "insulet.com": ("insulet_wellknown_403.txt", "text/plain", None),  # #753, #768
    "abbott.com": ("abbott_wellknown_403.html", "text/html", None),  # #803, #818 (CloudFront)
    "changehealthcare.com": ("optum_wellknown_403.html", "text/html; charset=iso-8859-1",  # #890, #907 (Apache)
                             "/en/.well-known/security.txt.html"),
}


def file_classes(store, recs):
    from noroshi.cmshpt_parse import classify_cms_hpt
    from noroshi.sectxt_parse import is_forbidden

    def body_of(r):
        return store.get_blob(r.body_sha256) if r.body_sha256 else None

    out = {}
    for r in recs:
        body = body_of(r)
        if r.purpose.startswith("sectxt"):
            out[r.purpose] = classify_security_txt(r.status, r.final_url, r.content_type, body, r.blocked, r.error, NOW,
                                                   forbidden=is_forbidden(recs, r.purpose, body_of))
        elif r.purpose == "cmshpt":
            out[r.purpose] = classify_cms_hpt(r.status, r.final_url, r.content_type, body, r.blocked, r.error,
                                              forbidden=is_forbidden(recs, r.purpose, body_of))
    return out


@pytest.mark.parametrize("name", list(THREE))
async def test_the_same_403_for_both_signatures_at_the_well_known_path_is_forbidden(servers, store, fetcher, name):
    body_file, ctype, moved = THREE[name]
    body = (PILOT / body_file).read_bytes()
    s = servers()
    s.expect_request("/").respond_with_data(HOME + LONG, content_type="text/html")
    if moved:  # business.optum.com sends both files to an .html address of its own
        s.expect_request("/.well-known/security.txt").respond_with_data("", status=301, headers={"Location": moved})
        s.expect_request(moved).respond_with_data(body, status=403, content_type=ctype)
        s.expect_request("/security.txt").respond_with_data("", status=301, headers={"Location": "/en/security.txt.html"})
    else:
        s.expect_request("/.well-known/security.txt").respond_with_data(body, status=403, content_type=ctype)
    assert await crawl_domain(row(s, sector="icsma", tokens=""), 1, fetcher, store, NOW) == "measured"
    recs = store.requests_for("a.org", 1)
    wk = [r for r in recs if r.purpose == "sectxt_wellknown"]
    assert [(r.status, r.ua_kind) for r in wk] == [(403, "identified"), (403, "browser")]
    assert file_classes(store, recs) == {"sectxt_wellknown": "forbidden", "sectxt_root": "absent"}  # no file at all
    later = recs[recs.index(wk[-1]) + 1:]
    assert [r.purpose for r in later] == ["sectxt_root"] + ["probe"] * 6
    assert {r.ua_kind for r in later} == {"identified"}  # the site was never switched
    assert not fetcher.uses_browser(s.host, 1)
    assert not needs_retry(store, "a.org")


async def test_a_cms_hpt_403_for_both_signatures_is_forbidden_and_not_retried(server, store, fetcher):
    server.expect_request("/cms-hpt.txt").respond_with_data("<h1>Forbidden</h1>", status=403, content_type="text/html")
    assert await crawl_domain(row(server), 1, fetcher, store, NOW) == "measured"
    recs = store.requests_for("a.org", 1)
    assert [r.ua_kind for r in recs if r.purpose == "cmshpt"] == ["identified", "browser"]
    assert file_classes(store, recs)["cmshpt"] == "forbidden"
    assert not needs_retry(store, "a.org")


async def test_a_refusal_whose_browser_retry_could_not_be_sent_stays_unmeasured(server, store, fetcher, monkeypatch):
    monkeypatch.setattr(config, "MAX_REQUESTS_PER_DOMAIN", 3)
    server.expect_request("/.well-known/security.txt").respond_with_data("Access denied", status=403)
    assert await crawl_domain(row(server, sector="device", tokens=""), 1, fetcher, store, NOW) == "measured"
    recs = store.requests_for("a.org", 1)
    assert [r.purpose for r in recs] == ["homepage", "robots", "sectxt_wellknown"]
    assert file_classes(store, recs) == {"sectxt_wellknown": "unmeasured"}
    assert needs_retry(store, "a.org")


async def test_a_homepage_refused_to_the_identified_signature_and_loaded_for_the_browser_still_switches(servers, store,
                                                                                                    fetcher):
    import re

    s = servers()

    def gate(request):
        if not request.headers["User-Agent"].startswith("Mozilla/"):
            return Response("<h1>Access Denied</h1>", status=403)
        return Response(HOME, content_type="text/html") if request.path == "/" else Response("nf", status=404)

    s.expect_request(re.compile(".*")).respond_with_handler(gate)
    assert await crawl_domain(row(s), 1, fetcher, store, NOW) == "measured"
    recs = store.requests_for("a.org", 1)
    assert [r.ua_kind for r in recs[:2]] == ["identified", "browser"] and {r.ua_kind for r in recs[2:]} == {"browser"}
    assert [r.headers["User-Agent"] == config.UA_BROWSER for r, _ in s.log] == [False] + [True] * (len(s.log) - 1)
    assert fetcher.uses_browser(s.host, 1)


# ---- post-pilot X10: pass 2 retries a measured domain whose planned core file has no record at all

async def test_a_measured_domain_whose_root_file_was_never_requested_is_retried(server, store, fetcher, monkeypatch):
    monkeypatch.setattr(config, "MAX_REQUESTS_PER_DOMAIN", 3)  # homepage, robots, well-known: the budget ends there
    assert await crawl_domain(row(server, sector="device", tokens=""), 1, fetcher, store, NOW) == "measured"
    assert purposes(store) == ["homepage", "robots", "sectxt_wellknown"]
    assert needs_retry(store, "a.org")


async def test_a_measured_hospital_whose_cms_hpt_was_never_requested_is_retried(server, store, fetcher, monkeypatch):
    monkeypatch.setattr(config, "MAX_REQUESTS_PER_DOMAIN", 4)
    assert await crawl_domain(row(server), 1, fetcher, store, NOW) == "measured"
    assert purposes(store) == ["homepage", "robots", "sectxt_wellknown", "sectxt_root"]
    assert needs_retry(store, "a.org")


@pytest.mark.parametrize("sector,core,retry", [
    ("device", ["sectxt_wellknown"], False),  # the root file was not called for: the well-known file is valid
    ("hospital", ["sectxt_wellknown"], True),  # cms-hpt.txt was called for
    ("hospital", ["sectxt_wellknown", "cmshpt"], False),
    ("device", [], True),  # the well-known file itself
])
def test_only_files_the_plan_called_for_count(store, sector, core, retry):
    url = {"homepage": "https://www.a.org/", "robots": "https://www.a.org/robots.txt",
           "sectxt_wellknown": "https://www.a.org/.well-known/security.txt", "cmshpt": "https://www.a.org/cms-hpt.txt"}
    body = {"homepage": HOME, "robots": None, "sectxt_wellknown": GOOD, "cmshpt": b"location-name: A\nmrf-url: x\n"}
    for purpose in ["homepage", "robots", *core]:
        store.record(stored("a.org", url[purpose], purpose, sector=sector, error=None, final_url=url[purpose],
                            status=404 if purpose == "robots" else 200, content_type="text/plain"), body[purpose])
    store.mark_done("a.org", 1, "measured")
    assert needs_retry(store, "a.org") is retry


# ---- post-pilot X2: the outage stop still relabels a domain waiting for its other host form

async def test_a_domain_waiting_for_its_other_host_form_at_the_outage_stop_is_relabelled(store):
    from noroshi.crawl import LocalOutage, OutageWatch

    async def control():
        return False

    watch = OutageWatch(store, 1, control)
    store.record(stored("wait.org", "https://wait.org/", "homepage", error="dns"), None)
    watch.other_form_pending("wait.org")  # its first form failed; the stop comes before its other form settles
    for i in range(20):
        rec = stored(f"d{i}.org", f"https://d{i}.org/", "homepage", error="dns")
        store.record(rec, None)
        if i < 19:
            await watch.homepage(f"d{i}.org", rec)
    with pytest.raises(LocalOutage):
        await watch.homepage("d19.org", rec)
    assert [r.error for r in store.requests_for("wait.org", 1)] == ["outage"]  # requested again on the rerun


# ---- post-pilot X5: a meta refresh on a page with almost no text is followed once

def refresh(target, delay=0):
    return f'<html><head><meta http-equiv="refresh" content="{delay}; url={target}"><title>Moved</title></head></html>'


async def test_the_va_policy_page_refresh_is_followed_and_its_target_stands_in(servers, store, fetcher, local_dns):
    """va.gov in the pilot (#286): its Policy: URL answered 200 with a 0-second meta refresh to another host and no
    text; the policy itself was never requested."""
    import re

    local_dns.update({"www.va.gov", "digital.va.gov"})
    s = servers()
    policy = f"http://www.va.gov:{s.port}/vulnerability-disclosure-policy/"
    real = f"http://digital.va.gov:{s.port}/vulnerability-disclosure-policy/"
    page = (PILOT / "va_policy_refresh.html").read_bytes().replace(b"https://digital.va.gov/", real.rsplit("/", 2)[0].encode() + b"/")
    assert real.encode() in page

    def handle(request):
        host = request.host.split(":")[0]
        if request.path == "/":
            return Response(HOME + LONG, content_type="text/html")
        if request.path == "/.well-known/security.txt":
            return Response(f"Contact: mailto:a@va.gov\nPolicy: {policy}\n", content_type="text/plain")
        if host == "www.va.gov" and request.path == "/vulnerability-disclosure-policy/":
            return Response(page, content_type="text/html")
        if host == "digital.va.gov" and request.path == "/vulnerability-disclosure-policy/":
            return Response("<h1>VA Vulnerability Disclosure Policy</h1>" + "<p>Report a vulnerability.</p>" * 9,
                            content_type="text/html")
        return Response("not here", status=404)

    s.expect_request(re.compile(".*")).respond_with_handler(handle)
    r = row(s, "va.gov", sector="federal", tokens="") | {"start_url": f"http://www.va.gov:{s.port}/"}
    assert await crawl_domain(r, 1, fetcher, store, NOW) == "measured"
    pages = [(x.url, x.status) for x in store.requests_for("va.gov", 1) if x.purpose == "policy_field"]
    assert pages == [(policy, 200), (real, 200)]
    assert purposes(store, "va.gov").count("probe") == 5  # the first conventional path is the Policy: URL
    assert [x.path for x, _ in s.log].count("/vulnerability-disclosure-policy/") == 2  # each sent once


async def test_a_homepage_meta_refresh_is_followed_and_the_crawl_continues_from_its_target(servers, store, fetcher,
                                                                                         local_dns):
    import re

    local_dns.add("www.moved.test")
    s = servers()
    target = f"http://www.moved.test:{s.port}/home"

    def handle(request):
        host = request.host.split(":")[0]
        if host != "www.moved.test":
            return Response(refresh(target) if request.path == "/" else "no", status=200 if request.path == "/" else 404)
        if request.path == "/home":
            return Response(HOME + LONG, content_type="text/html")
        return Response("not here", status=404)

    s.expect_request(re.compile(".*")).respond_with_handler(handle)
    assert await crawl_domain(row(s), 1, fetcher, store, NOW) == "measured"
    recs = store.requests_for("a.org", 1)
    assert [(x.purpose, x.url) for x in recs[:3]] == [("homepage", s.url_for("/")), ("homepage", target),
                                                      ("robots", f"http://www.moved.test:{s.port}/robots.txt")]
    assert all(x.host == "www.moved.test" for x in recs[1:])


async def test_a_refresh_loop_is_followed_once(server, store, fetcher):
    server.home = HOME + b"<a href='/a'>Vulnerability Disclosure</a>"
    server.expect_request("/a").respond_with_data(refresh("/b"), content_type="text/html")
    server.expect_request("/b").respond_with_data(refresh("/a"), content_type="text/html")
    assert await crawl_domain(row(server), 1, fetcher, store, NOW) == "measured"
    assert [x.url for x in store.requests_for("a.org", 1) if x.purpose == "candidate"] == [
        server.url_for("/a"), server.url_for("/b")]
    assert [x.path for x, _ in server.log].count("/a") == 1


async def test_a_homepage_refresh_back_to_itself_is_not_followed(servers, store, fetcher):
    s = servers()
    s.expect_request("/").respond_with_data(refresh("/"), content_type="text/html")
    assert await crawl_domain(row(s), 1, fetcher, store, NOW) == "measured"
    assert purposes(store).count("homepage") == 1


@pytest.mark.parametrize("page", [refresh("/b", delay=6), refresh("/b") + "<p>" + "words " * 40 + "</p>",
                                  '<meta http-equiv="refresh" content="0">', "<p>url=/b</p>"])
async def test_a_slow_refresh_a_page_with_text_or_no_target_is_not_followed(server, store, fetcher, page):
    server.home = HOME + b"<a href='/a'>Vulnerability Disclosure</a>"
    server.expect_request("/a").respond_with_data(page, content_type="text/html")
    assert await crawl_domain(row(server), 1, fetcher, store, NOW) == "measured"
    assert [x.url for x in store.requests_for("a.org", 1) if x.purpose == "candidate"] == [server.url_for("/a")]


async def test_a_refresh_target_robots_txt_disallows_is_not_requested(server, store, fetcher):
    server.expect_request("/robots.txt").respond_with_data("User-agent: *\nDisallow: /b\n", content_type="text/plain")
    server.home = HOME + b"<a href='/a'>Vulnerability Disclosure</a>"
    server.expect_request("/a").respond_with_data(refresh("/b"), content_type="text/html")
    assert await crawl_domain(row(server), 1, fetcher, store, NOW) == "measured"
    assert "/b" not in [x.path for x, _ in server.log]


# ---- post-pilot X9: fortwashingtonmc.org (pilot #10, #37), as the per-hop rule handles it

async def test_a_browser_retry_refused_by_a_redirect_target_that_never_refused_before_ends_blocked(servers, store,
                                                                                                   fetcher, local_dns):
    """#10: www.fortwashingtonmc.org refused the identified signature (Akamai 403). #37, its browser retry, was sent
    to that host with the browser signature and redirected to www.adventisthealthcare.com, which had not refused
    in the pass: that hop chose the identified signature and got Akamai's 403. One retry per refused request, and
    the browser signature only to a site that has refused (protocol section 5): the domain ends blocked."""
    import re

    local_dns.update({"www.fortwashingtonmc.org", "www.adventisthealthcare.com"})
    s = servers()
    target = f"http://www.adventisthealthcare.com:{s.port}/locations/profile/fort-washington-medical-center/"
    seen = []

    def handle(request):
        host = request.host.split(":")[0]
        browser = request.headers["User-Agent"] == config.UA_BROWSER
        seen.append((host, "browser" if browser else "identified"))
        if host == "www.fortwashingtonmc.org" and browser:
            return Response("", status=301, headers={"Location": target})
        page = "fortwashingtonmc_home_403.html" if host == "www.fortwashingtonmc.org" else "adventisthealthcare_403.html"
        return Response((PILOT / page).read_bytes(), status=403, content_type="text/html")

    s.expect_request(re.compile(".*")).respond_with_handler(handle)
    r = row(s, "fortwashingtonmc.org") | {"start_url": f"http://www.fortwashingtonmc.org:{s.port}/"}
    assert await crawl_domain(r, 1, fetcher, store, NOW) == "blocked"
    assert seen == [("www.fortwashingtonmc.org", "identified"), ("www.fortwashingtonmc.org", "browser"),
                    ("www.adventisthealthcare.com", "identified")]
    recs = store.requests_for("fortwashingtonmc.org", 1)
    assert [(x.status, x.ua_kind, x.final_url) for x in recs] == [(403, "identified", r["start_url"]),
                                                                   (403, "browser", target)]
    assert not fetcher.uses_browser("fortwashingtonmc.org", 1) and not fetcher.uses_browser("adventisthealthcare.com", 1)


# ---- post-pilot XR2: forbidden needs a plain denial, not a block page

def akamai_page():
    """The stored Akamai page (#10) as stored: it writes the address `errors&#46;edgesuite&#46;net` (post-pilot XR6)."""
    page = (PILOT / "fortwashingtonmc_home_403.html").read_bytes()
    assert b"errors&#46;edgesuite&#46;net" in page and b"errors.edgesuite.net" not in page
    return page


@pytest.mark.parametrize("body", [akamai_page(), b"<html><head><title>Just a moment...</title></head><body></body></html>"])
async def test_a_block_page_refused_to_both_signatures_at_a_file_stays_unmeasured_and_is_retried(server, store, fetcher,
                                                                                                 body):
    server.expect_request("/.well-known/security.txt").respond_with_data(body, status=403, content_type="text/html")
    assert await crawl_domain(row(server, sector="device", tokens=""), 1, fetcher, store, NOW) == "measured"
    recs = store.requests_for("a.org", 1)
    assert [(r.status, r.ua_kind) for r in recs if r.purpose == "sectxt_wellknown"] == [(403, "identified"),
                                                                                        (403, "browser")]
    assert needs_retry(store, "a.org")
    assert file_classes(store, recs) == {"sectxt_wellknown": "unmeasured", "sectxt_root": "absent"}


# ---- post-pilot fix round 1, F6: the refresh delay is compared as a number

@pytest.mark.parametrize("delay,followed", [("5.9", False), ("5.01", False), ("6", False), ("5.0", True), ("5", True),
                                            ("0", True), ("0.5", True)])
async def test_a_refresh_delay_is_read_as_a_number(server, store, fetcher, delay, followed):
    server.home = HOME + b"<a href='/a'>Vulnerability Disclosure</a>"
    server.expect_request("/a").respond_with_data(refresh("/b", delay=delay), content_type="text/html")
    assert await crawl_domain(row(server), 1, fetcher, store, NOW) == "measured"
    sent = [x.url for x in store.requests_for("a.org", 1) if x.purpose == "candidate"]
    assert sent == [server.url_for("/a")] + ([server.url_for("/b")] if followed else [])


# ---- post-pilot fix round 1, F7: a homepage meta refresh onto a ww<digits> host counts as being sent on

@pytest.mark.parametrize("start,target,status", [
    ("www.a.org", "ww2.a.org", "mismatch"),  # a parking service that uses a meta refresh instead of a 302
    ("www.a.org", "ww2.b.org", "measured"),  # not the listed domain's ww host
    ("ww2.a.org", "ww2.a.org", "measured"),  # the start host itself: not enough by itself
])
async def test_a_homepage_refresh_to_a_ww_digits_host_of_the_listed_domain_is_parked(servers, store, fetcher, local_dns,
                                                                                    start, target, status):
    import re

    local_dns.update({start, target})
    s = servers()

    def handle(request):
        if request.host.split(":")[0] == start and request.path == "/":
            return Response(refresh(at(s, target, "/?tkn=19vnMT3S")), content_type="text/html")
        return Response(HOME + LONG, content_type="text/html")

    s.expect_request(re.compile(".*")).respond_with_handler(handle)
    r = row(s, "a.org") | {"start_url": at(s, start)}
    assert await crawl_domain(r, 1, fetcher, store, NOW) == status
    assert [x.url for x in store.requests_for("a.org", 1) if x.purpose == "homepage"] == [
        at(s, start), at(s, target, "/?tkn=19vnMT3S")]


# ---- post-pilot fix round 1, F10: the two host forms of one registrable domain are one site

class RecordingTime(FakeTime):
    def __init__(self):
        super().__init__()
        self.sleeps: list[float] = []

    async def sleep(self, secs):
        self.sleeps.append(secs)
        await super().sleep(secs)


def shared_site(servers, local_dns):
    """shared-x.org does not resolve; www.shared-x.org does. Both are the site shared-x.org. /x refuses the
    identified signature and answers the browser one; every other path is a plain site."""
    import re

    local_dns.add("www.shared-x.org")
    s = servers()

    def handle(request):
        if request.path == "/x" and not request.headers["User-Agent"].startswith("Mozilla/"):
            return Response("no", status=403)
        if request.path in ("/", "/x"):
            return Response(HOME + LONG, content_type="text/html")
        return Response("not here", status=404)

    s.expect_request(re.compile(".*")).respond_with_handler(handle)
    return s, row(s, "shared-x.org", sector="device", tokens="") | {"start_url": f"http://shared-x.org:{s.port}/"}


async def test_the_other_form_waits_for_the_gap_after_the_first_forms_failure(servers, store, live, local_dns):
    s, r = shared_site(servers, local_dns)
    ft = RecordingTime()
    f = PoliteFetcher(store, frozenset(), live, clock=ft.clock, sleep=ft.sleep)
    assert await crawl_domain(r, 1, f, store, NOW) == "measured"
    homes = [x for x in store.requests_for("shared-x.org", 1) if x.purpose == "homepage"]
    assert [(x.error or x.status, x.site) for x in homes] == [("dns", "shared-x.org"), (200, "shared-x.org")]
    assert ft.sleeps[0] == pytest.approx(config.MIN_DELAY_SECONDS)  # one site: the second form waited the gap


async def test_a_429_from_the_shared_site_means_the_other_form_is_not_sent(servers, store, fetcher, local_dns):
    s, r = shared_site(servers, local_dns)
    s.clear()
    s.expect_request("/x").respond_with_data("slow down", status=429)
    real = fetcher.fetch

    async def fetch(domain, sector, url, purpose, pass_no, reuse=None):
        rec = await real(domain, sector, url, purpose, pass_no, reuse=reuse)
        if domain == "shared-x.org":  # meanwhile another listed domain gets a 429 from www.shared-x.org
            await real("p.org", "device", f"http://www.shared-x.org:{s.port}/x", "probe", 1)
        return rec

    fetcher.fetch = fetch
    assert await crawl_domain(r, 1, fetcher, store, NOW) == "blocked"
    assert [x.error for x in store.requests_for("shared-x.org", 1)] == ["dns"]
    assert [x.path for x, _ in s.log] == ["/x"]  # only the other listed domain's request reached the site


async def test_a_browser_switch_earned_on_one_form_holds_for_the_other(servers, store, fetcher, local_dns):
    s, r = shared_site(servers, local_dns)
    await fetcher.fetch("p.org", "device", f"http://www.shared-x.org:{s.port}/x", "probe", 1)  # refused, then answered
    assert fetcher.uses_browser("shared-x.org", 1)
    assert await crawl_domain(r, 1, fetcher, store, NOW) == "measured"
    homes = [x for x in store.requests_for("shared-x.org", 1) if x.purpose == "homepage"]
    assert [(x.error or x.status, x.ua_kind) for x in homes] == [("dns", "browser"), (200, "browser")]
    assert [(x.path, x.headers["User-Agent"] == config.UA_BROWSER) for x, _ in s.log][2] == ("/", True)


# ---- fix round 3, K1: while a site's browser retry is pending, other requests to that site wait

async def test_eight_domains_linking_one_refusing_page_send_it_two_requests(store, live, frozen):
    """hhs.gov in the second pilot: many listed domains link one page on a site that refuses the study's signature
    and answers the browser's. The first refusal holds the site until its browser retry is answered; the other
    domains then reuse that answer."""
    import re
    import threading
    import time

    s = HTTPServer(host="127.0.0.1", port=0, threaded=True)  # the listed domains are served side by side
    s.no_handler_status_code = 404
    s.start()
    try:
        domains = [f"d{i}.test" for i in range(8)]
        vdp = at(s, "t.test", "/vdp")
        all_asked = threading.Event()  # every listed domain has handed its request for T's page to the fetcher
        at_t = []

        def handle(request):
            host = request.host.split(":")[0]
            browser = request.headers.get("User-Agent", "").startswith("Mozilla/")
            if host == "t.test":
                at_t.append((request.path, "browser" if browser else "identified"))
                if browser:
                    return Response(b"<html><title>Vulnerability disclosure policy</title><p>"
                                    + b"Report a vulnerability to security@t.test. " * 8 + b"</p></html>",
                                    content_type="text/html")
                all_asked.wait(10)
                time.sleep(0.2)  # the other requests queue for T's turn meanwhile
                return Response("<html>Access denied</html>", status=403)
            if request.path == "/":
                return Response(f"<title>{host}</title><a href='{vdp}'>Vulnerability disclosure</a>",
                                content_type="text/html")
            return Response("not here", status=404)

        s.expect_request(re.compile(".*")).respond_with_handler(handle)
        ft = FakeTime()
        f = PoliteFetcher(store, frozenset(), live, clock=ft.clock, sleep=ft.sleep)
        fetch, asked = f.fetch, []

        async def fetch_and_count(domain, sector, url, purpose, pass_no, reuse=None):
            if url == vdp:
                asked.append(domain)
                if len(asked) == len(domains):
                    all_asked.set()
            return await fetch(domain, sector, url, purpose, pass_no, reuse=reuse)

        f.fetch = fetch_and_count
        df = table(*[row(s, d, "device") | {"start_url": at(s, d)} for d in domains])
        assert await crawl_sector(df, 1, f, store, frozen_file=frozen) == {"measured": 8}
        assert sorted(asked) == domains  # all eight asked for T's page before T answered the first
        assert at_t == [("/vdp", "identified"), ("/vdp", "browser")]  # the refused request and its browser retry
        pages = {d: [r for r in store.requests_for(d, 1) if r.purpose == "candidate"] for d in domains}
        assert all(rs[-1].status == 200 and rs[-1].url == vdp for rs in pages.values())  # measured for every one
        assert sorted(sum(r.reused for r in rs) for rs in pages.values()) == [0] + [1] * 7  # seven by reuse
        assert store.count_site_requests("t.test", 1) == 2
        assert f.uses_browser("t.test", 1)
    finally:
        s.stop()
