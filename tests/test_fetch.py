import asyncio
import socket
import ssl

import httpx
import pytest
from werkzeug.wrappers import Response

from noroshi import config
from noroshi.fetch import BudgetExceeded, NotLive, OptedOut, PoliteFetcher, map_exception
from noroshi.store import Store


class FakeTime:
    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self):
        return self.now

    async def sleep(self, secs):
        self.sleeps.append(secs)
        self.now += secs


@pytest.fixture
def ft():
    return FakeTime()


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "db" / "c.sqlite")


@pytest.fixture
def live(tmp_path):
    p = tmp_path / "LIVE"
    p.write_text("go")
    return p


def make(store, live, ft, optout=frozenset()):
    return PoliteFetcher(store, optout, live, clock=ft.clock, sleep=ft.sleep)


async def test_refuses_without_live_file(httpserver, store, tmp_path, ft):
    f = make(store, tmp_path / "missing", ft)
    with pytest.raises(NotLive):
        await f.fetch("a.org", "hospital", httpserver.url_for("/"), "homepage", 1)
    assert store.count_requests("a.org", 1) == 0 and httpserver.log == []


async def test_refuses_opted_out_domain(httpserver, store, live, ft):
    f = make(store, live, ft, frozenset({"a.org"}))
    with pytest.raises(OptedOut):
        await f.fetch("a.org", "hospital", httpserver.url_for("/"), "homepage", 1)
    assert store.count_requests("a.org", 1) == 0 and httpserver.log == []


async def test_only_get_is_sent_with_identified_ua(httpserver, store, live, ft):
    httpserver.expect_request("/x").respond_with_data("hi")
    rec = await make(store, live, ft).fetch("a.org", "hospital", httpserver.url_for("/x"), "homepage", 1)
    assert [r.method for r, _ in httpserver.log] == ["GET"]
    assert httpserver.log[0][0].headers["User-Agent"] == config.UA_IDENTIFIED
    assert rec.status == 200 and rec.ua_kind == "identified" and rec.blocked is False
    assert store.get_blob(rec.body_sha256) == b"hi"
    assert store.count_requests("a.org", 1) == 1


async def test_two_second_gap_same_host(httpserver, store, live, ft):
    httpserver.expect_request("/x").respond_with_data("hi")
    f = make(store, live, ft)
    await f.fetch("a.org", "hospital", httpserver.url_for("/x"), "p", 1)
    assert ft.sleeps == []
    await f.fetch("a.org", "hospital", httpserver.url_for("/x"), "p", 1)
    assert len(ft.sleeps) == 1 and ft.sleeps[0] >= 2.0


async def test_gap_is_per_host_and_shrinks_as_time_passes(httpserver, store, live, ft):
    httpserver.expect_request("/x").respond_with_data("hi")
    f = make(store, live, ft)
    await f.fetch("a.org", "hospital", httpserver.url_for("/x"), "p", 1)
    ft.now += 1.5
    await f.fetch("a.org", "hospital", httpserver.url_for("/x"), "p", 1)
    assert ft.sleeps == [pytest.approx(0.5)]
    # a different host has its own clock: 'localhost' vs '127.0.0.1'
    other = httpserver.url_for("/x").replace("localhost", "127.0.0.1")
    if other != httpserver.url_for("/x"):
        ft.sleeps.clear()
        await f.fetch("b.org", "hospital", other, "p", 1)
        assert ft.sleeps == []


async def test_cap_is_twelve_including_retries(httpserver, store, live, ft):
    httpserver.expect_request("/x").respond_with_data("hi")
    f = make(store, live, ft)
    for _ in range(12):
        await f.fetch("a.org", "hospital", httpserver.url_for("/x"), "p", 1)
    n = len(httpserver.log)
    with pytest.raises(BudgetExceeded):
        await f.fetch("a.org", "hospital", httpserver.url_for("/x"), "p", 1)
    assert len(httpserver.log) == n == 12 and store.count_requests("a.org", 1) == 12
    # the cap is per (domain, pass); another listed domain on another site is unaffected
    await f.fetch("a.org", "hospital", httpserver.url_for("/x"), "p", 2)
    await f.fetch("b.org", "hospital", at(httpserver, "b.test"), "p", 1)


async def test_block_on_last_attempt_returns_blocked_record(httpserver, store, live, ft):
    httpserver.expect_request("/x").respond_with_data("hi")
    httpserver.expect_request("/blocked").respond_with_data("no", status=403)
    f = make(store, live, ft)
    for _ in range(11):
        await f.fetch("a.org", "hospital", httpserver.url_for("/x"), "p", 1)
    rec = await f.fetch("a.org", "hospital", httpserver.url_for("/blocked"), "p", 1)
    assert rec.blocked is True and rec.ua_kind == "identified"  # no budget left to retry
    assert store.count_requests("a.org", 1) == 12
    assert store.requests_for("a.org", 1)[-1].blocked is True
    with pytest.raises(BudgetExceeded):  # a call that starts with no budget still raises
        await f.fetch("a.org", "hospital", httpserver.url_for("/x"), "p", 1)


def ua_gate(request):
    if request.headers.get("User-Agent", "").startswith("Mozilla/"):
        return Response("ok", status=200)
    return Response("denied", status=403)


async def test_block_switches_to_browser_ua(httpserver, store, live, ft):
    httpserver.expect_request("/u").respond_with_handler(ua_gate)
    httpserver.expect_request("/u2").respond_with_handler(ua_gate)
    f = make(store, live, ft)
    url, url2 = httpserver.url_for("/u"), httpserver.url_for("/u2")
    assert not f.uses_browser("localhost", 1)
    rec = await f.fetch("a.org", "hospital", url, "homepage", 1)
    assert rec.status == 200 and rec.ua_kind == "browser" and f.uses_browser("localhost", 1)
    got = store.requests_for("a.org", 1)
    assert [r.ua_kind for r in got] == ["identified", "browser"]
    assert [r.blocked for r in got] == [True, False]
    assert len(ft.sleeps) == 1 and ft.sleeps[0] >= 2.0  # retry waits the normal delay
    rec2 = await f.fetch("a.org", "hospital", url2, "robots", 1)
    assert rec2.ua_kind == "browser" and store.count_requests("a.org", 1) == 3
    assert not f.uses_browser("other.org", 1)


async def test_browser_stickiness_is_per_pass(httpserver, store, live, ft):
    httpserver.expect_request("/u").respond_with_handler(ua_gate)
    f = make(store, live, ft)
    await f.fetch("a.org", "hospital", httpserver.url_for("/u"), "p", 1)
    rec = await f.fetch("a.org", "hospital", httpserver.url_for("/u"), "p", 2)
    assert [r.ua_kind for r in store.requests_for("a.org", 2)][0] == "identified"
    assert rec.ua_kind == "browser"


async def test_still_blocked_after_browser_retry(httpserver, store, live, ft):
    httpserver.expect_request("/x").respond_with_data("nope", status=403)
    rec = await make(store, live, ft).fetch("a.org", "hospital", httpserver.url_for("/x"), "p", 1)
    assert rec.blocked is True and rec.ua_kind == "browser" and rec.status == 403
    assert store.count_requests("a.org", 1) == 2  # no third try
    assert len(httpserver.log) == 2


async def test_redirect_chain_recorded_not_counted(httpserver, store, live, ft):
    httpserver.expect_request("/a").respond_with_data("", status=301, headers={"Location": "/b"})
    httpserver.expect_request("/b").respond_with_data("", status=302, headers={"Location": httpserver.url_for("/c")})
    httpserver.expect_request("/c").respond_with_data("end")
    rec = await make(store, live, ft).fetch("a.org", "hospital", httpserver.url_for("/a"), "p", 1)
    assert rec.status == 200 and rec.final_url == httpserver.url_for("/c")
    assert rec.redirect_chain == [httpserver.url_for("/a"), httpserver.url_for("/b")]
    assert store.count_requests("a.org", 1) == 1
    assert len(ft.sleeps) == 2  # every hop respects the delay
    assert store.get_blob(rec.body_sha256) == b"end"


async def test_too_many_redirects(httpserver, store, live, ft):
    httpserver.expect_request("/loop").respond_with_data("", status=302, headers={"Location": "/loop"})
    rec = await make(store, live, ft).fetch("a.org", "hospital", httpserver.url_for("/loop"), "p", 1)
    assert rec.error == "too_many_redirects" and rec.status is None
    assert len(httpserver.log) == config.MAX_REDIRECTS + 1
    assert store.count_requests("a.org", 1) == 1


def test_body_cap_is_one_mebibyte():
    assert config.MAX_BODY_BYTES == 1_048_576


async def test_body_truncated_at_cap(httpserver, store, live, ft):
    httpserver.expect_request("/big").respond_with_data(b"a" * (config.MAX_BODY_BYTES + 40_000))
    rec = await make(store, live, ft).fetch("a.org", "hospital", httpserver.url_for("/big"), "p", 1)
    assert rec.body_len == config.MAX_BODY_BYTES and rec.truncated is True
    assert len(store.get_blob(rec.body_sha256)) == config.MAX_BODY_BYTES


async def test_body_exactly_at_cap_not_truncated(httpserver, store, live, ft):
    httpserver.expect_request("/big").respond_with_data(b"a" * config.MAX_BODY_BYTES)
    rec = await make(store, live, ft).fetch("a.org", "hospital", httpserver.url_for("/big"), "p", 1)
    assert rec.body_len == config.MAX_BODY_BYTES and rec.truncated is False


async def test_timeout_recorded(httpserver, store, live, ft, monkeypatch):
    monkeypatch.setattr(config, "READ_TIMEOUT", 0.3)

    def slow(request):
        import time
        time.sleep(1.0)
        return Response("late")

    httpserver.expect_request("/slow").respond_with_handler(slow)
    rec = await make(store, live, ft).fetch("a.org", "hospital", httpserver.url_for("/slow"), "p", 1)
    assert rec.error == "timeout" and rec.status is None and rec.blocked is False
    assert store.count_requests("a.org", 1) == 1


async def test_dns_error_recorded(store, live, ft):
    rec = await make(store, live, ft).fetch("a.org", "hospital", "http://no-such-host.invalid/", "p", 1)
    assert rec.error == "dns" and rec.status is None


async def test_connect_error_recorded(store, live, ft):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here now
    rec = await make(store, live, ft).fetch("a.org", "hospital", f"http://127.0.0.1:{port}/", "p", 1)
    assert rec.error == "connect"


async def test_tls_error_recorded(httpserver, store, live, ft):
    url = httpserver.url_for("/").replace("http://", "https://")  # plain-HTTP server, TLS client
    rec = await make(store, live, ft).fetch("a.org", "hospital", url, "p", 1)
    assert rec.error == "tls"


def _chain(exc, cause):
    exc.__cause__ = cause
    return exc


def test_exception_mapping():
    assert map_exception(httpx.ReadTimeout("t")) == "timeout"
    assert map_exception(httpx.ConnectTimeout("t")) == "timeout"
    assert map_exception(httpx.PoolTimeout("t")) == "timeout"
    assert map_exception(_chain(httpx.ConnectError("x"), socket.gaierror(-2, "unknown"))) == "dns"
    assert map_exception(_chain(httpx.ConnectError("x"), ssl.SSLCertVerificationError("bad"))) == "tls"
    assert map_exception(httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] bad")) == "tls"
    assert map_exception(_chain(httpx.ConnectError("x"), ConnectionRefusedError())) == "connect"
    assert map_exception(httpx.ReadError("reset")) == "connect"
    assert map_exception(httpx.RemoteProtocolError("bad")) == "connect"


async def test_failed_attempt_counts_toward_cap(store, live, ft):
    f = make(store, live, ft)
    await f.fetch("a.org", "hospital", "http://no-such-host.invalid/", "p", 1)
    assert store.count_requests("a.org", 1) == 1


async def test_redirect_to_optout_domain_stops(httpserver, store, live, ft):
    target = httpserver.url_for("/t").replace("localhost", "127.0.0.1")
    assert "127.0.0.1" in target
    httpserver.expect_request("/a").respond_with_data("", status=302, headers={"Location": target})
    httpserver.expect_request("/t").respond_with_data("should never be fetched")
    src = httpserver.url_for("/a").replace("127.0.0.1", "localhost")
    f = make(store, live, ft, frozenset({"127.0.0.1"}))
    rec = await f.fetch("a.org", "hospital", src, "p", 1)
    assert rec.error == "redirect_to_optout" and rec.status is None
    assert rec.redirect_chain == [src]
    assert [r.path for r, _ in httpserver.log] == ["/a"]
    assert store.count_requests("a.org", 1) == 1


async def test_waiting_between_hops_does_not_count_toward_the_time_limit(httpserver, store, live, ft, monkeypatch):
    monkeypatch.setattr(config, "TOTAL_TIMEOUT", 5.0)
    for a, b in (("/a", "/b"), ("/b", "/c"), ("/c", "/d")):
        httpserver.expect_request(a).respond_with_data("", status=302, headers={"Location": b})
    httpserver.expect_request("/d").respond_with_data("end")
    rec = await make(store, live, ft).fetch("a.org", "hospital", httpserver.url_for("/a"), "p", 1)
    assert sum(ft.sleeps) >= 6.0  # three 2 s gaps waited between the hops ...
    assert rec.status == 200 and rec.error is None  # ... and none of it counted


async def test_response_time_across_hops_still_counts(httpserver, store, live, ft, monkeypatch):
    monkeypatch.setattr(config, "TOTAL_TIMEOUT", 5.0)

    def slow_redirect(target):
        def handle(request):
            ft.now += 2.0  # each answer takes 2 s of the fake clock
            return Response("", status=302, headers={"Location": target})
        return handle

    for a, b in (("/a", "/b"), ("/b", "/c"), ("/c", "/d")):
        httpserver.expect_request(a).respond_with_handler(slow_redirect(b))
    httpserver.expect_request("/d").respond_with_data("end")
    rec = await make(store, live, ft).fetch("a.org", "hospital", httpserver.url_for("/a"), "p", 1)
    assert rec.error == "timeout" and rec.status is None  # 6 s of answers across three hops
    assert "/d" not in [r.path for r, _ in httpserver.log]


async def test_bad_redirect_scheme(httpserver, store, live, ft):
    httpserver.expect_request("/a").respond_with_data("", status=302, headers={"Location": "ftp://example.org/x"})
    rec = await make(store, live, ft).fetch("a.org", "hospital", httpserver.url_for("/a"), "p", 1)
    assert rec.error == "bad_redirect" and rec.status is None
    assert len(httpserver.log) == 1


@pytest.fixture
def raw_redirect_server():
    """Plain socket server that answers every request with a 302 and a verbatim Location."""
    import threading

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    hits = []

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            hits.append(conn.recv(4096))
            conn.sendall(f"HTTP/1.1 302 Found\r\nLocation: {serve.loc}\r\nContent-Length: 0\r\n"
                         "Connection: close\r\n\r\n".encode())
            conn.close()

    serve.loc = ""
    threading.Thread(target=serve, daemon=True).start()
    yield srv.getsockname()[1], serve, hits
    srv.close()


@pytest.mark.parametrize("loc", ["mailto:a@b.org", "javascript:alert(1)", "data:text/plain,hi",
                                 "http://127.0.0.1:70000/"])
async def test_unusable_redirect_targets_are_recorded(raw_redirect_server, store, live, ft, loc):
    port, serve, hits = raw_redirect_server
    serve.loc = loc
    rec = await make(store, live, ft).fetch("a.org", "hospital", f"http://127.0.0.1:{port}/a", "p", 1)
    assert rec.error == "bad_redirect" and rec.status is None
    assert store.count_requests("a.org", 1) == 1 and len(hits) == 1


async def test_bad_redirects_cannot_defeat_the_cap(httpserver, store, live, ft):
    httpserver.expect_request("/a").respond_with_data("", status=302, headers={"Location": "mailto:a@b.org"})
    f = make(store, live, ft)
    for _ in range(12):
        await f.fetch("a.org", "hospital", httpserver.url_for("/a"), "p", 1)
    with pytest.raises(BudgetExceeded):
        await f.fetch("a.org", "hospital", httpserver.url_for("/a"), "p", 1)
    assert len(httpserver.log) == 12 and store.count_requests("a.org", 1) == 12


async def test_unexpected_exception_is_still_recorded(httpserver, store, live, ft, monkeypatch):
    async def boom(self, *a, **k):  # fails once the attempt is sent
        raise RuntimeError("surprise")

    monkeypatch.setattr(httpx.AsyncClient, "send", boom)
    rec = await make(store, live, ft).fetch("a.org", "hospital", httpserver.url_for("/a"), "p", 1)
    assert rec.error == "internal" and store.count_requests("a.org", 1) == 1
    assert store.requests_for("a.org", 1)[0].error == "internal"


async def test_cancellation_is_recorded_then_propagates(httpserver, store, live, ft):
    import threading

    arrived, release = threading.Event(), threading.Event()

    def slow(request):
        arrived.set()
        release.wait(5)
        return Response("late")

    httpserver.expect_request("/a").respond_with_handler(slow)
    task = asyncio.create_task(make(store, live, ft).fetch("a.org", "hospital", httpserver.url_for("/a"), "p", 1))
    while not arrived.is_set():  # the request is at the server
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.count_requests("a.org", 1) == 1
    assert store.requests_for("a.org", 1)[0].error == "cancelled"
    release.set()  # let the shared server finish this request before the next test
    for _ in range(200):
        if httpserver.log:
            break
        await asyncio.sleep(0.01)


async def test_optout_checked_on_url_host_and_registrable_domain(httpserver, store, live, ft):
    httpserver.expect_request("/x").respond_with_data("hi")
    url = httpserver.url_for("/x")
    f = make(store, live, ft, frozenset({"optedout.org", "x.org"}))
    for domain, u in (("a.org", "https://optedout.org/x"), ("a.org", "https://www.optedout.org./x"),
                      ("www.x.org", url), ("a.org", "https://sub.x.org/x")):
        with pytest.raises(OptedOut):
            await f.fetch(domain, "hospital", u, "p", 1)
    assert httpserver.log == [] and store.count_requests("a.org", 1) == 0


async def test_429_is_recorded_but_not_retried(httpserver, store, live, ft):
    httpserver.expect_request("/x").respond_with_data("slow down", status=429)
    f = make(store, live, ft)
    rec = await f.fetch("a.org", "hospital", httpserver.url_for("/x"), "p", 1)
    assert rec.status == 429 and rec.blocked is True and rec.ua_kind == "identified"
    assert store.count_requests("a.org", 1) == 1 and len(httpserver.log) == 1
    assert not f.uses_browser("localhost", 1)


async def test_trailing_dot_host_shares_the_gap(httpserver, store, live, ft):
    httpserver.expect_request("/x").respond_with_data("hi")
    f = make(store, live, ft)
    url = httpserver.url_for("/x")
    await f.fetch("a.org", "hospital", url, "p", 1)
    host = url.split("//")[1].split(":")[0]
    await f.fetch("a.org", "hospital", url.replace(host, host + "."), "p", 1)
    assert len(ft.sleeps) == 1 and ft.sleeps[0] >= 2.0


async def test_gap_holds_when_sleep_returns_early(httpserver, store, live, ft):
    httpserver.expect_request("/x").respond_with_data("hi")

    async def early(secs):  # returns after only half the requested time
        ft.sleeps.append(secs)
        ft.now += secs / 2

    f = PoliteFetcher(store, frozenset(), live, clock=ft.clock, sleep=early)
    await f.fetch("a.org", "hospital", httpserver.url_for("/x"), "p", 1)
    t0 = ft.now
    await f.fetch("a.org", "hospital", httpserver.url_for("/x"), "p", 1)
    assert ft.now - t0 >= 2.0


# ---- A2: the gap and a second cap are per site (registrable domain), not per listed label

def at(httpserver, host, path="/x"):
    """The local server's URL under another host name (the test resolver maps it to 127.0.0.1)."""
    return f"http://{host}:{httpserver.port}{path}"


async def test_sibling_hosts_of_one_site_share_the_gap(httpserver, store, live, ft, local_dns):
    local_dns.update({"www.sib.org", "security.sib.org"})
    httpserver.expect_request("/x").respond_with_data("hi")
    f = make(store, live, ft)
    await f.fetch("a.org", "hospital", at(httpserver, "www.sib.org"), "p", 1)
    await f.fetch("b.org", "hospital", at(httpserver, "security.sib.org"), "p", 1)
    assert len(ft.sleeps) == 1 and ft.sleeps[0] >= 2.0
    assert {r.site for d in ("a.org", "b.org") for r in store.requests_for(d, 1)} == {"sib.org"}


async def test_site_cap_counts_every_listed_domain(httpserver, store, live, ft):
    from noroshi.fetch import SiteBudgetExceeded

    httpserver.expect_request("/x").respond_with_data("hi")
    f = make(store, live, ft)
    for i in range(config.MAX_REQUESTS_PER_SITE):
        await f.fetch(f"d{i}.org", "hospital", at(httpserver, "hub.test"), "p", 1)
    with pytest.raises(SiteBudgetExceeded) as e:
        await f.fetch("late.org", "hospital", at(httpserver, "hub.test"), "p", 1)
    assert e.value.site == "hub.test"
    assert len(httpserver.log) == 12 and store.count_requests("late.org", 1) == 0  # not sent, not recorded
    await f.fetch("late.org", "hospital", at(httpserver, "other.test"), "p", 1)  # another site is fine
    await f.fetch("late.org", "hospital", at(httpserver, "hub.test"), "p", 2)  # so is another pass


async def test_browser_retry_respects_the_site_cap(httpserver, store, live, ft):
    httpserver.expect_request("/x").respond_with_data("hi")
    httpserver.expect_request("/no").respond_with_data("no", status=403)
    f = make(store, live, ft)
    for i in range(config.MAX_REQUESTS_PER_SITE - 1):
        await f.fetch(f"d{i}.org", "hospital", at(httpserver, "hub.test"), "p", 1)
    rec = await f.fetch("last.org", "hospital", at(httpserver, "hub.test", "/no"), "p", 1)
    assert rec.blocked and rec.ua_kind == "identified" and len(httpserver.log) == 12


# ---- A4: the browser signature is keyed by the site that refused

async def test_refusal_switches_only_the_refusing_site(httpserver, store, live, ft):
    httpserver.expect_request("/no").respond_with_handler(ua_gate)  # refuses the identified signature only (X3)
    httpserver.expect_request("/x").respond_with_data("hi")
    f = make(store, live, ft)
    rec = await f.fetch("a.test", "device", at(httpserver, "third.test", "/no"), "candidate", 1)
    assert rec.ua_kind == "browser" and f.uses_browser("third.test", 1)
    rec = await f.fetch("a.test", "device", at(httpserver, "a.test"), "probe", 1)
    assert rec.ua_kind == "identified" and not f.uses_browser("a.test", 1)


async def test_refusal_is_read_back_by_site_after_a_restart(httpserver, store, live, ft):
    httpserver.expect_request("/no").respond_with_handler(ua_gate)  # refuses the identified signature only (X3)
    httpserver.expect_request("/x").respond_with_data("hi")
    await make(store, live, ft).fetch("a.test", "device", at(httpserver, "third.test", "/no"), "candidate", 1)
    f2 = make(store, live, ft)  # a restarted run
    assert f2.uses_browser("third.test", 1) and not f2.uses_browser("a.test", 1)
    assert not f2.uses_browser("third.test", 2)


# ---- A8: the time limit starts when the request is sent

async def test_waiting_for_the_gap_does_not_count_toward_the_time_limit(httpserver, store, live, ft, monkeypatch):
    monkeypatch.setattr(config, "TOTAL_TIMEOUT", 1.0)
    httpserver.expect_request("/x").respond_with_data("hi")
    f = make(store, live, ft)
    await f.fetch("a.org", "hospital", httpserver.url_for("/x"), "p", 1)
    rec = await f.fetch("a.org", "hospital", httpserver.url_for("/x"), "p", 1)  # waits 2 s for the gap first
    assert ft.sleeps and rec.status == 200 and rec.error is None


async def test_an_attempt_cancelled_before_it_is_sent_is_not_recorded(httpserver, store, live, ft):
    httpserver.expect_request("/x").respond_with_data("hi")
    waiting = asyncio.Event()

    async def stuck(secs):
        waiting.set()
        await asyncio.Event().wait()

    f = PoliteFetcher(store, frozenset(), live, clock=ft.clock, sleep=stuck)
    await f.fetch("a.org", "hospital", httpserver.url_for("/x"), "p", 1)
    task = asyncio.create_task(f.fetch("a.org", "hospital", httpserver.url_for("/x"), "p", 1))
    await waiting.wait()  # the second attempt is waiting for the gap
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.count_requests("a.org", 1) == 1 and len(httpserver.log) == 1


# ---- A14: one client per signature for the life of the fetcher

async def test_one_client_per_signature_closed_on_exit(httpserver, store, live, ft, monkeypatch):
    made = []
    real = httpx.AsyncClient

    class Counting(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            made.append(self)

    monkeypatch.setattr(httpx, "AsyncClient", Counting)
    httpserver.expect_request("/x").respond_with_data("hi")
    httpserver.expect_request("/u").respond_with_handler(ua_gate)
    async with make(store, live, ft) as f:
        for _ in range(3):
            await f.fetch("a.org", "hospital", httpserver.url_for("/x"), "p", 1)
        await f.fetch("b.org", "hospital", at(httpserver, "b.test", "/u"), "p", 1)  # refused, then browser
        await f.fetch("b.org", "hospital", at(httpserver, "b.test", "/u"), "p", 1)
    assert len(made) == 2 and all(c.is_closed for c in made)


# ---- A15: labels

@pytest.mark.parametrize("loc", ["http://127.0.0.1:70000/", "http://[::1/", "mailto:a@b.org"])
async def test_unbuildable_location_is_bad_redirect_with_the_redirecting_url(raw_redirect_server, store, live, ft, loc):
    port, serve, hits = raw_redirect_server
    serve.loc = loc
    url = f"http://127.0.0.1:{port}/a"
    rec = await make(store, live, ft).fetch("a.org", "hospital", url, "p", 1)
    assert rec.error == "bad_redirect" and rec.redirect_chain == [url] and len(hits) == 1


async def test_undecodable_body_is_labelled_decode(httpserver, store, live, ft):
    httpserver.expect_request("/z").respond_with_data(b"this is not gzip", headers={"Content-Encoding": "gzip"})
    rec = await make(store, live, ft).fetch("a.org", "hospital", httpserver.url_for("/z"), "p", 1)
    assert rec.error == "decode" and rec.status is None and store.count_requests("a.org", 1) == 1


async def test_unusable_url_is_neither_sent_nor_recorded(httpserver, store, live, ft):
    from noroshi.fetch import UnusableURL

    with pytest.raises(UnusableURL):
        await make(store, live, ft).fetch("a.org", "hospital", "http://a.org:99999/x", "candidate", 1)
    assert store.count_requests("a.org", 1) == 0


# ---- rulings round, R4: homepage attempts are outside the site cap

async def test_homepage_attempts_are_neither_counted_nor_refused_by_the_site_cap(httpserver, store, live, ft):
    from noroshi.fetch import SiteBudgetExceeded

    httpserver.expect_request("/x").respond_with_data("hi")
    httpserver.expect_request("/no").respond_with_data("no", status=403)
    f = make(store, live, ft)
    for i in range(config.MAX_REQUESTS_PER_SITE):
        await f.fetch(f"d{i}.org", "hospital", at(httpserver, "hub.test"), "probe", 1)
    rec = await f.fetch("hub.test", "hospital", at(httpserver, "hub.test", "/no"), "homepage", 1)
    assert rec.ua_kind == "browser" and len(httpserver.log) == 14  # the homepage and its one browser retry
    assert store.count_site_requests("hub.test", 1) == 12 and store.count_requests("hub.test", 1) == 2
    with pytest.raises(SiteBudgetExceeded):  # any other request to the site is still refused
        await f.fetch("hub.test", "hospital", at(httpserver, "hub.test", "/robots.txt"), "robots", 1)
    assert len(httpserver.log) == 14


# ---- residuals round, S2: every hop chooses its signature from its own site

@pytest.fixture
def seen(httpserver):
    """A catch-all handler playing m.test (a measured site that never refuses) and third.test (refuses the
    identified signature); records (host, path, signature) of every request."""
    import re

    log = []

    def handle(request):
        host = request.host.split(":")[0]
        kind = "browser" if request.headers.get("User-Agent", "").startswith("Mozilla/") else "identified"
        log.append((host, request.path, kind))
        if host == "m.test" and request.path == "/security":
            return Response("", status=301, headers={"Location": at(httpserver, "third.test", "/vdp")})
        if host == "third.test" and kind == "identified":
            return Response("<html>Forbidden</html>", status=403)
        if host == "third.test" and request.path == "/go":
            return Response("", status=301, headers={"Location": at(httpserver, "m.test", "/page")})
        return Response("ok")

    httpserver.expect_request(re.compile(".*")).respond_with_handler(handle)
    return log


async def test_refusal_behind_a_redirect_sends_the_browser_signature_only_to_the_refusing_site(seen, httpserver, store,
                                                                                            live, ft):
    f = make(store, live, ft)
    rec = await f.fetch("m.test", "device", at(httpserver, "m.test", "/security"), "probe", 1)
    assert seen == [("m.test", "/security", "identified"), ("third.test", "/vdp", "identified"),
                    ("m.test", "/security", "identified"), ("third.test", "/vdp", "browser")]
    assert rec.status == 200 and rec.ua_kind == "browser"  # a hop of the attempt used the browser signature
    assert f.uses_browser("third.test", 1) and not f.uses_browser("m.test", 1)


async def test_redirect_from_a_refusing_site_into_one_that_never_refused_is_identified(seen, httpserver, store, live, ft):
    f = make(store, live, ft)
    await f.fetch("a.test", "device", at(httpserver, "third.test", "/vdp"), "candidate", 1)  # third.test refuses
    seen.clear()
    rec = await f.fetch("a.test", "device", at(httpserver, "third.test", "/go"), "candidate", 1)
    assert seen == [("third.test", "/go", "browser"), ("m.test", "/page", "identified")]
    assert rec.status == 200 and rec.ua_kind == "browser"


# ---- residuals round, S4: a 429 stops every request to that site for the pass

@pytest.fixture
def slow_down(httpserver):
    """hub.test answers 429 to everything; x.test/go redirects into hub.test; other.test answers 200."""
    import re

    def handle(request):
        host = request.host.split(":")[0]
        if host == "hub.test":
            return Response("slow down", status=429)
        if host == "x.test" and request.path == "/go":
            return Response("", status=301, headers={"Location": at(httpserver, "hub.test", "/")})
        return Response("ok")

    httpserver.expect_request(re.compile(".*")).respond_with_handler(handle)


def hub_hits(httpserver):
    return [r for r, _ in httpserver.log if r.host.split(":")[0] == "hub.test"]


async def test_after_a_429_nothing_more_is_sent_to_that_site(slow_down, httpserver, store, live, ft):
    from noroshi.fetch import RateLimited

    f = make(store, live, ft)
    rec = await f.fetch("a.org", "hospital", at(httpserver, "hub.test", "/"), "homepage", 1)
    assert rec.status == 429 and f.rate_limited("hub.test", 1)
    with pytest.raises(RateLimited) as e:  # a first hop to it: not sent, not recorded
        await f.fetch("b.org", "hospital", at(httpserver, "hub.test", "/x"), "candidate", 1)
    assert e.value.site == "hub.test" and store.count_requests("b.org", 1) == 0
    rec = await f.fetch("c.org", "hospital", at(httpserver, "x.test", "/go"), "homepage", 1)  # a hop into it
    assert rec.error == "rate_limited" and rec.blocked and rec.status is None
    assert rec.redirect_chain == [at(httpserver, "x.test", "/go")]
    assert len(hub_hits(httpserver)) == 1
    assert store.find_measurement(at(httpserver, "x.test", "/go"), 1) is None  # never reused
    await f.fetch("d.org", "hospital", at(httpserver, "other.test", "/"), "homepage", 1)  # other sites go on
    with pytest.raises(RateLimited):  # and the mark is read back after a restart
        await make(store, live, ft).fetch("e.org", "hospital", at(httpserver, "hub.test", "/y"), "probe", 1)
    assert len(hub_hits(httpserver)) == 1


async def test_a_request_waiting_for_the_slot_is_dropped_after_a_429(slow_down, httpserver, store, live, ft):
    from noroshi.fetch import RateLimited

    f = make(store, live, ft)
    got = await asyncio.gather(*(f.fetch(d, "hospital", at(httpserver, "hub.test", "/"), "homepage", 1)
                                 for d in ("a.org", "b.org")), return_exceptions=True)
    assert sorted(type(g).__name__ for g in got) == ["FetchRecord", "RateLimited"]
    assert any(isinstance(g, RateLimited) for g in got)
    assert len(hub_hits(httpserver)) == 1
    assert store.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1


# ---- residuals round, S5: cookies do not carry between requests

@pytest.fixture
def cookie_site(httpserver):
    cookies = []

    def handle(request):
        cookies.append((request.path, request.headers.get("Cookie")))
        if request.path == "/set":
            return Response("ok", headers={"Set-Cookie": "a=1; Path=/"})
        if request.path == "/r":
            return Response("", status=302, headers={"Location": "/dest", "Set-Cookie": "b=2; Path=/"})
        return Response("ok")

    for path in ("/set", "/next", "/r", "/dest"):
        httpserver.expect_request(path).respond_with_handler(handle)
    return cookies


async def test_a_cookie_set_on_one_request_is_not_sent_on_the_next(cookie_site, httpserver, store, live, ft):
    f = make(store, live, ft)
    await f.fetch("a.org", "hospital", at(httpserver, "c.test", "/set"), "p", 1)
    await f.fetch("a.org", "hospital", at(httpserver, "c.test", "/next"), "p", 1)
    await f.fetch("b.org", "hospital", at(httpserver, "c.test", "/next"), "p", 1)
    assert cookie_site == [("/set", None), ("/next", None), ("/next", None)]


async def test_a_cookie_set_on_a_redirect_is_sent_on_the_next_hop_of_that_request(cookie_site, httpserver, store,
                                                                                  live, ft):
    f = make(store, live, ft)
    await f.fetch("a.org", "hospital", at(httpserver, "c.test", "/r"), "p", 1)
    await f.fetch("a.org", "hospital", at(httpserver, "c.test", "/next"), "p", 1)
    assert cookie_site == [("/r", None), ("/dest", "b=2"), ("/next", None)]


# ---- closing fixes, C5: a 429 is remembered across a restart

@pytest.fixture
def stalled_429():
    """Plain socket server: every request gets a 429 whose body never finishes (100 bytes promised, 4 sent); the
    connection is held until the client hangs up."""
    import threading

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    hits = []

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            hits.append(conn.recv(4096))
            conn.sendall(b"HTTP/1.1 429 Too Many Requests\r\nContent-Type: text/plain\r\nContent-Length: 100\r\n"
                         b"\r\nslow")
            conn.settimeout(10)
            try:
                while conn.recv(4096):
                    pass
            except OSError:
                pass
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    yield srv.getsockname()[1], hits
    srv.close()


async def test_a_429_whose_body_stalls_is_remembered_by_a_restarted_fetcher(stalled_429, store, live, ft, monkeypatch):
    from noroshi.fetch import RateLimited

    monkeypatch.setattr(config, "TOTAL_TIMEOUT", 1.0)
    port, hits = stalled_429
    url = f"http://127.0.0.1:{port}/x"
    await make(store, live, ft).fetch("a.org", "hospital", url, "probe", 1)
    with pytest.raises(RateLimited):  # a restart in the same pass: that site is sent nothing
        await make(store, live, ft).fetch("b.org", "hospital", url, "probe", 1)
    assert len(hits) == 1
    rec, = store.requests_for("a.org", 1)
    assert (rec.status, rec.blocked, rec.error, rec.final_url) == (429, True, None, url)
    assert (rec.body_sha256, rec.body_len) == (None, 0)  # the body of a 429 is not read


async def test_a_429_is_on_its_row_before_the_attempt_ends(httpserver, store, live, ft, monkeypatch):
    """A kill or a cancel that comes after the 429 arrived still leaves the row with status 429."""
    from noroshi.fetch import RateLimited

    httpserver.expect_request("/x").respond_with_data("slow down", status=429)
    seen = []
    real_aclose = httpx.Response.aclose

    async def cancelled_while_closing(self):
        seen.append(store.db.execute("SELECT status, error FROM requests").fetchall())  # what a kill here leaves
        await real_aclose(self)
        raise asyncio.CancelledError  # the outage stop arrives now

    monkeypatch.setattr(httpx.Response, "aclose", cancelled_while_closing)
    with pytest.raises(asyncio.CancelledError):
        await make(store, live, ft).fetch("a.org", "hospital", httpserver.url_for("/x"), "probe", 1)
    monkeypatch.setattr(httpx.Response, "aclose", real_aclose)
    assert seen == [[(429, "in_flight")]]
    rec, = store.requests_for("a.org", 1)
    assert (rec.status, rec.error, rec.final_url) == (429, "cancelled", httpserver.url_for("/x"))
    with pytest.raises(RateLimited):
        await make(store, live, ft).fetch("b.org", "hospital", httpserver.url_for("/y"), "probe", 1)
    assert len(httpserver.log) == 1


# ---- post-pilot X7: the error text of a failed attempt is kept

async def test_a_failed_attempt_keeps_the_exception_class_and_message(httpserver, store, live, ft):
    rec = await make(store, live, ft).fetch("a.org", "hospital", "http://no-such-host.invalid/", "p", 1)
    assert rec.error == "dns" and rec.error_detail.startswith("ConnectError: ")
    assert "no-such-host.invalid" in rec.error_detail
    assert store.requests_for("a.org", 1)[0].error_detail == rec.error_detail
    httpserver.expect_request("/x").respond_with_data("hi")
    ok = await make(store, live, ft).fetch("a.org", "hospital", httpserver.url_for("/x"), "p", 1)
    assert ok.error is None and ok.error_detail is None


async def test_error_detail_is_cut_to_300_characters(httpserver, store, live, ft, monkeypatch):
    async def boom(self, *a, **k):
        raise RuntimeError("x" * 1000)

    monkeypatch.setattr(httpx.AsyncClient, "send", boom)
    rec = await make(store, live, ft).fetch("a.org", "hospital", httpserver.url_for("/a"), "p", 1)
    assert rec.error == "internal" and rec.error_detail == ("RuntimeError: " + "x" * 1000)[:300]


async def test_too_many_redirects_and_timeouts_keep_a_detail(httpserver, store, live, ft, monkeypatch):
    httpserver.expect_request("/loop").respond_with_data("", status=302, headers={"Location": "/loop"})
    rec = await make(store, live, ft).fetch("a.org", "hospital", httpserver.url_for("/loop"), "p", 1)
    assert rec.error == "too_many_redirects" and rec.error_detail
    monkeypatch.setattr(config, "READ_TIMEOUT", 0.3)

    def slow(request):
        import time
        time.sleep(1.0)
        return Response("late")

    httpserver.expect_request("/slow").respond_with_handler(slow)
    rec = await make(store, live, ft).fetch("b.org", "hospital", httpserver.url_for("/slow"), "p", 1)
    assert rec.error == "timeout" and "Timeout" in rec.error_detail


# ---- post-pilot X6: a larger body cap for the homepage only

def test_homepage_cap_is_four_mebibytes():
    assert config.MAX_HOMEPAGE_BYTES == 4_194_304 and config.MAX_BODY_BYTES == 1_048_576


async def test_homepage_body_is_read_to_four_mebibytes_and_other_bodies_to_one(httpserver, store, live, ft):
    two = b"a" * (2 * 1_048_576)
    httpserver.expect_request("/two").respond_with_data(two)
    httpserver.expect_request("/big").respond_with_data(b"b" * (config.MAX_HOMEPAGE_BYTES + 40_000))
    f = make(store, live, ft)
    home = await f.fetch("a.org", "hospital", httpserver.url_for("/two"), "homepage", 1)
    assert home.body_len == len(two) and home.truncated is False
    page = await f.fetch("a.org", "hospital", httpserver.url_for("/two"), "probe", 1)
    assert page.body_len == config.MAX_BODY_BYTES and page.truncated is True
    cut = await f.fetch("a.org", "hospital", httpserver.url_for("/big"), "homepage", 1)
    assert cut.body_len == config.MAX_HOMEPAGE_BYTES and cut.truncated is True
    assert len(store.get_blob(cut.body_sha256)) == config.MAX_HOMEPAGE_BYTES


# ---- post-pilot X3: a refusal that the browser retry gets too is a refusal of the path, not of the signature

def signatures(httpserver):
    return ["browser" if r.headers["User-Agent"] == config.UA_BROWSER else "identified" for r, _ in httpserver.log]


async def test_a_refusal_the_browser_retry_also_gets_leaves_the_site_on_the_identified_signature(httpserver, store,
                                                                                               live, ft):
    """abbott.com, optum.com, insulet.com in the pilot: the same 403 at /.well-known/security.txt for both signatures."""
    httpserver.expect_request("/.well-known/security.txt").respond_with_data("Access denied", status=403,
                                                                            content_type="text/plain")
    httpserver.expect_request("/private").respond_with_data("Access denied", status=403, content_type="text/plain")
    httpserver.expect_request("/x").respond_with_data("not here", status=404)
    f = make(store, live, ft)
    rec = await f.fetch("a.org", "icsma", at(httpserver, "a.test", "/.well-known/security.txt"), "sectxt_wellknown", 1)
    assert (rec.status, rec.blocked, rec.ua_kind) == (403, True, "browser")
    assert not f.uses_browser("a.test", 1)
    await f.fetch("a.org", "icsma", at(httpserver, "a.test", "/x"), "probe", 1)  # stays identified
    await f.fetch("a.org", "icsma", at(httpserver, "a.test", "/private"), "probe", 1)  # a later refusal: retried again
    assert signatures(httpserver) == ["identified", "browser", "identified", "identified", "browser"]
    assert store.count_requests("a.org", 1) == 5  # each retry counts
    assert not make(store, live, ft).uses_browser("a.test", 1)  # nor after a restart


async def test_a_switch_is_read_back_after_a_restart_and_a_refusal_alone_is_not(httpserver, store, live, ft):
    httpserver.expect_request("/u").respond_with_handler(ua_gate)
    httpserver.expect_request("/no").respond_with_data("no", status=403)
    f = make(store, live, ft)
    await f.fetch("a.test", "device", at(httpserver, "gate.test", "/u"), "candidate", 1)
    await f.fetch("a.test", "device", at(httpserver, "wall.test", "/no"), "candidate", 1)
    assert f.uses_browser("gate.test", 1) and not f.uses_browser("wall.test", 1)
    f2 = make(store, live, ft)  # a restarted run
    assert f2.uses_browser("gate.test", 1) and not f2.uses_browser("wall.test", 1)
    assert not f2.uses_browser("gate.test", 2)


async def test_a_refusal_whose_browser_retry_was_never_sent_does_not_switch(httpserver, store, live, ft):
    httpserver.expect_request("/x").respond_with_data("hi")
    httpserver.expect_request("/u").respond_with_handler(ua_gate)
    f = make(store, live, ft)
    for _ in range(11):
        await f.fetch("a.org", "hospital", httpserver.url_for("/x"), "p", 1)
    rec = await f.fetch("a.org", "hospital", httpserver.url_for("/u"), "p", 1)  # the 12th: no budget for the retry
    assert rec.blocked and rec.ua_kind == "identified" and not f.uses_browser("localhost", 1)


# ---- post-pilot XR3: the hop at which an attempt failed is kept

async def test_a_failed_attempt_keeps_the_address_of_the_hop_it_failed_at(httpserver, store, live, ft):
    f = make(store, live, ft)
    rec = await f.fetch("a.org", "hospital", "http://no-such-host.invalid/", "p", 1)
    assert rec.error == "dns" and rec.error_url == "http://no-such-host.invalid/"
    httpserver.expect_request("/go").respond_with_data("", status=302, headers={"Location": "http://gone.invalid/x"})
    rec = await f.fetch("b.org", "hospital", httpserver.url_for("/go"), "p", 1)
    assert rec.error == "dns" and rec.error_url == "http://gone.invalid/x" and rec.final_url is None
    assert store.requests_for("b.org", 1)[0].error_url == "http://gone.invalid/x"
    httpserver.expect_request("/x").respond_with_data("hi")
    ok = await f.fetch("c.org", "hospital", httpserver.url_for("/x"), "p", 1)
    assert ok.error is None and ok.error_url is None


# ---- post-pilot fix round 1, F5: a site is switched only when the browser retry got an answer

async def test_a_browser_retry_that_times_out_does_not_switch_the_site(httpserver, store, live, ft, monkeypatch):
    import time

    monkeypatch.setattr(config, "READ_TIMEOUT", 0.3)

    def gate(request):
        if request.headers.get("User-Agent", "").startswith("Mozilla/"):
            time.sleep(0.8)
            return Response("late")
        return Response("no", status=403)

    httpserver.expect_request("/u").respond_with_handler(gate)
    httpserver.expect_request("/x").respond_with_data("hi")
    f = make(store, live, ft)
    rec = await f.fetch("a.org", "hospital", at(httpserver, "g.test", "/u"), "p", 1)
    assert (rec.error, rec.ua_kind) == ("timeout", "browser")
    assert not f.uses_browser("g.test", 1) and not make(store, live, ft).uses_browser("g.test", 1)
    await f.fetch("a.org", "hospital", at(httpserver, "g.test", "/x"), "p", 1)
    assert signatures(httpserver)[-1] == "identified"


@pytest.mark.parametrize("answer,switched", [(404, True), (200, True), (503, False)])
async def test_a_browser_retry_switches_the_site_only_with_an_answer(httpserver, store, live, ft, answer, switched):
    def gate(request):
        if request.headers.get("User-Agent", "").startswith("Mozilla/"):
            return Response("browser", status=answer)
        return Response("no", status=403)

    httpserver.expect_request("/u").respond_with_handler(gate)
    f = make(store, live, ft)
    rec = await f.fetch("a.org", "hospital", at(httpserver, "g.test", "/u"), "p", 1)
    assert rec.status == answer and rec.ua_kind == "browser"
    assert f.uses_browser("g.test", 1) is switched


# ---- post-pilot fix round 1, F8: a redirect to an opted-out site leaves no address of that site in the record

async def test_a_redirect_to_an_opted_out_site_stores_none_of_its_address(httpserver, store, live, ft):
    target = at(httpserver, "optedout.test", "/t?id=7")
    httpserver.expect_request("/a").respond_with_data("", status=302, headers={"Location": target})
    src = at(httpserver, "a.test", "/a")
    rec = await make(store, live, ft, frozenset({"optedout.test"})).fetch("a.org", "hospital", src, "p", 1)
    assert rec.error == "redirect_to_optout" and rec.error_url == src
    stored, = store.requests_for("a.org", 1)
    assert not any("optedout" in str(v) for v in (stored.error_detail, stored.error_url, stored.final_url,
                                                  *stored.redirect_chain))


# ---- fix round 3, K1: while a site's browser retry is pending, no other request is sent to that site

def gate_to(location=None, browser_status=200):
    """403 to the identified signature. To the browser's: a redirect to `location` when given, else
    `browser_status`."""
    def handle(request):
        if request.headers.get("User-Agent", "").startswith("Mozilla/"):
            if location is not None:
                return Response("", status=302, headers={"Location": location})
            return Response("browser", status=browser_status)
        return Response("<html>Access denied</html>", status=403)
    return handle


def test_the_wait_for_a_pending_retry_is_bounded_by_one_requests_time_limit():
    assert 0 < config.RETRY_WAIT_SECONDS <= config.TOTAL_TIMEOUT


async def test_a_retry_that_fails_lets_the_waiting_requests_go_on_identified_each_once(httpserver, store, live, ft):
    httpserver.expect_request("/vdp").respond_with_handler(gate_to("http://gone.invalid/x"))  # the retry: a dns error
    f = make(store, live, ft)
    url = at(httpserver, "t.test", "/vdp")
    recs = await asyncio.gather(*(f.fetch(d, "device", url, "candidate", 1) for d in ("a.test", "b.test", "c.test")))
    # each listed domain's identified request went out once, and never while another one's retry was pending
    assert signatures(httpserver) == ["identified", "browser"] * 3
    assert [(r.domain, r.error, r.ua_kind) for r in recs] == [(d, "dns", "browser") for d in ("a.test", "b.test",
                                                                                             "c.test")]
    assert not f.uses_browser("t.test", 1)  # never switched


async def test_a_request_that_waited_decides_afresh_when_the_retry_got_a_429(httpserver, store, live, ft, monkeypatch):
    from noroshi.fetch import RateLimited

    monkeypatch.setattr(config, "RETRY_WAIT_SECONDS", 30.0)  # a mark left behind would hold the request this long
    httpserver.expect_request("/vdp").respond_with_handler(gate_to(browser_status=429))
    f = make(store, live, ft)
    url = at(httpserver, "t.test", "/vdp")
    async with asyncio.timeout(10):
        got = await asyncio.gather(*(f.fetch(d, "device", url, "candidate", 1) for d in ("a.test", "b.test")),
                                   return_exceptions=True)
    assert got[0].status == 429 and isinstance(got[1], RateLimited)  # not sent, not recorded
    assert signatures(httpserver) == ["identified", "browser"] and store.count_requests("b.test", 1) == 0


async def test_two_retries_that_need_each_others_site_go_on_after_the_bound(httpserver, store, live, ft, monkeypatch):
    import re

    monkeypatch.setattr(config, "RETRY_WAIT_SECONDS", 0.5)

    def handle(request):  # x.test and y.test refuse the identified signature; each sends the browser's to the other
        host = request.host.split(":")[0]
        if request.headers.get("User-Agent", "").startswith("Mozilla/") and request.path == "/start":
            return Response("", status=302, headers={"Location": at(httpserver, {"x.test": "y.test",
                                                                                 "y.test": "x.test"}[host], "/p")})
        return Response("<html>Access denied</html>", status=403)

    httpserver.expect_request(re.compile(".*")).respond_with_handler(handle)
    f = make(store, live, ft)
    loop = asyncio.get_running_loop()
    began = loop.time()
    async with asyncio.timeout(10):  # neither waits for ever
        a, b = await asyncio.gather(f.fetch("a.test", "device", at(httpserver, "x.test", "/start"), "candidate", 1),
                                    f.fetch("b.test", "device", at(httpserver, "y.test", "/start"), "candidate", 1))
    assert loop.time() - began >= 0.5  # each retry's hop waited the bound for the other's retry, then went on
    assert (a.status, a.blocked, a.final_url) == (403, True, at(httpserver, "y.test", "/p"))
    assert (b.status, b.blocked, b.final_url) == (403, True, at(httpserver, "x.test", "/p"))
    assert not f.uses_browser("x.test", 1) and not f.uses_browser("y.test", 1)


@pytest.mark.parametrize("ending", ["refused_again", "not_sent", "cancelled"])
async def test_the_wait_ends_when_the_retry_ends_however_it_ends(httpserver, store, live, ft, monkeypatch, ending):
    import threading

    monkeypatch.setattr(config, "RETRY_WAIT_SECONDS", 30.0)  # a mark left behind would hold b.test this long
    retry_sent = threading.Event()

    def handle(request):
        if request.headers.get("User-Agent", "").startswith("Mozilla/"):
            first = not retry_sent.is_set()
            retry_sent.set()
            if ending == "cancelled" and first:
                import time
                time.sleep(1.0)  # the retry is still out when its task is cancelled
            return Response("<html>Access denied</html>", status=403)
        return Response("<html>Access denied</html>", status=403)

    httpserver.expect_request("/vdp").respond_with_handler(handle)
    httpserver.expect_request("/x").respond_with_data("hi")
    f = make(store, live, ft)
    if ending == "not_sent":  # t.test's 12th request is the refused one: no limit is left for its retry
        for i in range(config.MAX_REQUESTS_PER_SITE - 1):
            await f.fetch(f"d{i}.test", "device", at(httpserver, "t.test", "/x"), "probe", 1)
    url = at(httpserver, "t.test", "/vdp")
    a = asyncio.create_task(f.fetch("a.test", "device", url, "candidate", 1))
    b = asyncio.create_task(f.fetch("b.test", "device", url, "candidate", 1))
    if ending == "cancelled":
        await asyncio.to_thread(retry_sent.wait, 5)
        a.cancel()
    async with asyncio.timeout(10):
        got = await asyncio.gather(a, b, return_exceptions=True)
    if ending == "refused_again":
        assert [(r.status, r.ua_kind) for r in got] == [(403, "browser"), (403, "browser")]
    elif ending == "not_sent":
        from noroshi.fetch import SiteBudgetExceeded

        assert (got[0].status, got[0].ua_kind) == (403, "identified") and isinstance(got[1], SiteBudgetExceeded)
    else:
        assert isinstance(got[0], asyncio.CancelledError) and (got[1].status, got[1].ua_kind) == (403, "browser")
