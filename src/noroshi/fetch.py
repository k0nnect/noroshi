"""Polite fetcher: the only code that sends requests to measured sites.

Limits come from `config` and are ethics commitments: GET only, a cap of sent attempts per (listed domain,
pass) and another per (site, pass), one request at a time per site with a minimum gap, and no traffic before
the LIVE file exists. A site is the registrable domain of the requested URL's host.

Each attempt is written to the store as an `in_flight` row just before it is sent and completed in place
after it ends. An attempt that is never sent is never recorded.
"""
import asyncio
import contextvars
import socket
import ssl
import time
from dataclasses import dataclass, field
from http.cookiejar import CookieJar, DefaultCookiePolicy
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from noroshi import config
from noroshi.blocking import detect_block
from noroshi.domains import normalize_url, registrable_domain
from noroshi.store import FetchRecord, Store, is_answer

_REDIRECTS = (301, 302, 303, 307, 308)
# set by the client's response hook: a redirect response arrived for the GET now being sent (per task)
_redirect_seen: contextvars.ContextVar[bool] = contextvars.ContextVar("redirect_seen", default=False)


async def _note_response(response: httpx.Response) -> None:
    # runs before httpx builds the redirect request, which can itself raise on a Location it cannot use
    _redirect_seen.set(response.has_redirect_location)


class BudgetExceeded(Exception):
    """The (listed domain, pass) already used its request cap."""


class SiteBudgetExceeded(Exception):
    """The (site, pass) already used its request cap, counted across every listed domain."""

    def __init__(self, site: str):
        super().__init__(site)
        self.site = site


class RateLimited(Exception):
    """The site answered 429 earlier in this pass: nothing more is sent to it. Not sent, not recorded."""

    def __init__(self, site: str):
        super().__init__(site)
        self.site = site


class NotLive(Exception):
    """info/LIVE does not exist; no measured site may be contacted."""


class OptedOut(Exception):
    """The domain is listed in info/optout.txt."""


class UnusableURL(Exception):
    """No request can be built from this URL; nothing was sent or recorded."""


class _TooManyRedirects(Exception):
    pass


class _BadRedirect(Exception):
    pass


class _RedirectToOptout(Exception):
    pass


class _Deadline(Exception):
    pass


class _HopRateLimited(Exception):
    """A redirect hop would go to a rate-limited site: the hop is not sent."""


class _Reused(Exception):
    """A measurement of the URL appeared while the attempt waited for its site: nothing is sent."""

    def __init__(self, rec: FetchRecord):
        super().__init__(rec.url)
        self.rec = rec


def _causes(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


ERROR_DETAIL_CHARS = 300


def error_detail(exc: BaseException) -> str:
    """The exception's class name and message, cut to 300 characters."""
    message = str(exc)
    return (f"{type(exc).__name__}: {message}" if message else type(exc).__name__)[:ERROR_DETAIL_CHARS]


def _norm_host(host: str | None) -> str:
    return (host or "").lower().rstrip(".")


def site_of(host: str | None) -> str:
    """The site a request goes to: the registrable domain of its host."""
    host = _norm_host(host)
    return registrable_domain(host) if host else ""


def url_site(url: str) -> str:
    return site_of(urlsplit(url).hostname)


def map_exception(exc: BaseException) -> str:
    """Map a transport exception to one of: timeout, dns, connect, tls."""
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    chain = list(_causes(exc))
    if any(isinstance(e, socket.gaierror) for e in chain):
        return "dns"
    if any(isinstance(e, ssl.SSLError) for e in chain):
        return "tls"
    text = " ".join(str(e) for e in chain).upper()
    if "SSL" in text or "CERTIFICATE" in text:
        return "tls"
    return "connect"


def _usable(url: str) -> bool:
    """True when an http(s) request can be built from the URL."""
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError for an out-of-range port
        httpx.URL(url)
    except Exception:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


@dataclass
class _Sending:
    """One attempt: its row once written, the signature of each hop, and its time limit. Only time between
    sending a hop and finishing its answer counts (`spent`, on the fetcher's clock and on the loop's);
    waiting for a site's slot or gap never does. `deadline` is the current hop's, on the fetcher's clock."""
    pass_no: int
    max_bytes: int  # the body cap: MAX_HOMEPAGE_BYTES for a homepage request, else MAX_BODY_BYTES
    row_id: int | None = None
    deadline: float = float("inf")
    guard: asyncio.Timeout | None = None
    browser_hops: list[bool] = field(default_factory=list)
    trial: str | None = None  # a browser retry: the site that refused; its hops use the browser signature
    hop: str | None = None  # the address of the hop being sent or read
    jar: httpx.Cookies = field(default_factory=httpx.Cookies)  # cookies live only for this attempt's hops
    spent: float = 0.0
    spent_loop: float = 0.0
    answered_429: str | None = None  # the URL of a hop that answered 429, set the moment the answer arrives
    refusal: "_Refusal | None" = None  # the fetch call's: its own browser retry is never held by it


@dataclass(eq=False)
class _Refusal:
    """One per `PoliteFetcher.fetch` call. When the call's first attempt is refused (its last hop's answer refuses the
    identified signature), `key` names that site and pass, set while that hop still holds the site's turn, and the
    site is held until the call's browser retry has settled (`settled` is set when the call ends)."""
    key: tuple[str, int] | None = None
    settled: asyncio.Event | None = None


class PoliteFetcher:
    def __init__(self, store: Store, optout: frozenset[str], live_file: Path,
                 clock=time.monotonic, sleep=asyncio.sleep):
        self.store = store
        self.optout = frozenset(d.lower() for d in optout)
        self.live_file = Path(live_file)
        self._clock = clock
        self._sleep = sleep
        self._domain_locks: dict[str, asyncio.Lock] = {}
        self._site_locks: dict[str, asyncio.Lock] = {}
        self._last_end: dict[str, float] = {}  # site -> when its last request ended
        self._browser: set[tuple[str, int]] = set()  # (site switched to the browser signature, pass)
        self._browser_loaded: set[int] = set()
        self._rate_limited: set[tuple[str, int]] = set()  # (site that answered 429, pass)
        self._rate_loaded: set[int] = set()
        self._held: dict[tuple[str, int], _Refusal] = {}  # (site, pass) -> the refusal whose browser retry is pending
        self._clients: dict[bool, httpx.AsyncClient] = {}

    async def __aenter__(self) -> "PoliteFetcher":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        clients, self._clients = self._clients, {}
        for client in clients.values():
            await client.aclose()

    def _client(self, browser: bool) -> httpx.AsyncClient:
        """One client per signature for the life of the fetcher. Its own cookie jar refuses every cookie, so
        nothing a site sets is sent on a later request; each attempt keeps its own jar for its redirect hops."""
        if browser not in self._clients:
            timeout = httpx.Timeout(connect=config.CONNECT_TIMEOUT, read=config.READ_TIMEOUT,
                                    write=config.READ_TIMEOUT, pool=config.READ_TIMEOUT)
            ua = config.UA_BROWSER if browser else config.UA_IDENTIFIED
            self._clients[browser] = httpx.AsyncClient(follow_redirects=False, verify=True, timeout=timeout,
                                                       headers={"User-Agent": ua},
                                                       cookies=CookieJar(DefaultCookiePolicy(allowed_domains=[])),
                                                       event_hooks={"response": [_note_response]})
        return self._clients[browser]

    def _opted_out(self, name: str) -> bool:
        name = _norm_host(name)
        return bool(name) and (name in self.optout or registrable_domain(name) in self.optout)

    def uses_browser(self, site: str, pass_no: int) -> bool:
        """True once `site` was switched to the browser signature in this pass: it refused the identified
        signature and the browser retry of that request was not refused (after a restart, read back from the
        store). Every hop of every request chooses its signature by this, for its own site."""
        if pass_no not in self._browser_loaded:
            self._browser_loaded.add(pass_no)
            self._browser |= {(site, pass_no) for site in self.store.browser_sites(pass_no)}
        return (site, pass_no) in self._browser

    def rate_limited(self, site: str, pass_no: int) -> bool:
        """True once `site` answered 429 in this pass (after a restart, read back from the stored 429s):
        nothing more is sent to it in the pass, at any hop."""
        if pass_no not in self._rate_loaded:
            self._rate_loaded.add(pass_no)
            for url, final_url in self.store.rate_limits(pass_no):
                self._rate_limited.add((url_site(final_url or url), pass_no))
        return (site, pass_no) in self._rate_limited

    async def fetch(self, domain: str, sector: str, url: str, purpose: str, pass_no: int,
                    reuse=None) -> FetchRecord:
        """`reuse()`, when given, returns a stored copy of an earlier measurement of this URL, or None. It is
        asked again once the site slot is held, just before sending: a copy found then is returned instead."""
        if not self.live_file.exists():
            raise NotLive(str(self.live_file))
        if self._opted_out(domain):
            raise OptedOut(domain)
        if not _usable(url):
            raise UnusableURL(url)
        url = normalize_url(url)  # the record keeps the requested URL in one spelling
        if self._opted_out(urlsplit(url).hostname):
            raise OptedOut(domain)
        if self.rate_limited(url_site(url), pass_no):
            raise RateLimited(url_site(url))
        refusal = _Refusal()  # set only when the first attempt is refused (_turn); without a refusal nothing uses it
        async with self._domain_locks.setdefault(domain, asyncio.Lock()):
            try:
                try:
                    rec, refused_identified = await self._attempt(domain, sector, url, purpose, pass_no, reuse,
                                                                  refusal=refusal)
                except _Reused as found:
                    return found.rec
                if refused_identified:  # the answering hop refused the identified signature (a 429 never counts)
                    refusing = url_site(rec.final_url)
                    try:  # sent again from its first URL: hops at the refusing site use the browser signature, every
                        # other hop chooses its signature as usual
                        rec, _ = await self._attempt(domain, sector, url, purpose, pass_no, reuse, trial=refusing,
                                                     refusal=refusal)
                    except _Reused as found:
                        rec = found.rec
                    except (BudgetExceeded, SiteBudgetExceeded, RateLimited):
                        pass  # no budget left for the retry, or the site said 429 meanwhile: the refused attempt stands
                    else:
                        # the browser retry got an answer (completed, not refused, 200 or a 4xx): the site keeps the
                        # browser signature; a retry refused again, failed, or ended with any other status switches
                        # nothing
                        if is_answer(rec.status, rec.blocked, rec.error):
                            self._browser.add((refusing, pass_no))
                            self.store.mark_browser(refusing, pass_no)
                return rec
            finally:
                self._release(refusal)  # the retry settled, or was never made, whatever ended the call

    def _check_caps(self, domain: str, site: str, pass_no: int, purpose: str) -> None:
        if self.store.count_requests(domain, pass_no) >= config.MAX_REQUESTS_PER_DOMAIN:
            raise BudgetExceeded(f"{domain} pass {pass_no}")
        # a listed domain's own homepage request is never refused by the site cap, and is not counted in it
        if purpose != "homepage" and self.store.count_site_requests(site, pass_no) >= config.MAX_REQUESTS_PER_SITE:
            raise SiteBudgetExceeded(site)

    def _hold(self, site: str, sending: _Sending) -> None:
        """The site refused the identified signature at the last hop of a call's first attempt, which still holds the
        site's turn: until the call's browser retry has settled, other requests' turns at the site wait (_get_once)."""
        refusal = sending.refusal
        refusal.key, refusal.settled = (site, sending.pass_no), asyncio.Event()
        self._held[refusal.key] = refusal

    def _release(self, refusal: _Refusal) -> None:
        if refusal.key is None:
            return  # the call was never refused: nothing was held
        if self._held.get(refusal.key) is refusal:
            del self._held[refusal.key]
        refusal.settled.set()

    async def _attempt(self, domain, sector, url, purpose, pass_no, reuse=None,
                       trial: str | None = None, refusal: _Refusal | None = None) -> tuple[FetchRecord, bool]:
        """One attempt and whether its answering hop refused the identified signature. `trial` (a browser retry)
        is the site whose hops use the browser signature in this attempt."""
        host = _norm_host(urlsplit(url).hostname)
        site = site_of(host)

        def reused() -> None:
            found = reuse() if reuse is not None else None
            if found is not None:
                raise _Reused(found)

        reused()
        self._check_caps(domain, site, pass_no, purpose)  # fail fast; checked again just before sending
        rec = FetchRecord(
            domain=domain, sector=sector, host=host, url=url,
            purpose=purpose, pass_no=pass_no, ua_kind="identified",
            started_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            status=None, final_url=None, redirect_chain=[], content_type=None,
            body_sha256=None, body_len=0, truncated=False, error=None, blocked=False, site=site,
        )
        sending = _Sending(pass_no, max_bytes=config.MAX_HOMEPAGE_BYTES if purpose == "homepage"
                           else config.MAX_BODY_BYTES, trial=trial, refusal=refusal)

        def start() -> None:
            # the site slot is held, the gap has passed and the first hop's signature is chosen: from here on
            # the attempt is sent and timed
            reused()  # another listed domain may have measured this URL while this one waited
            self._check_caps(domain, site, pass_no, purpose)
            rec.ua_kind = "browser" if sending.browser_hops[0] else "identified"
            rec.started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            sending.row_id = self.store.begin(rec)

        def keep_429() -> None:
            if sending.answered_429 is not None:  # a 429 arrived: its row says so however the attempt ended
                rec.status, rec.final_url, rec.blocked = 429, sending.answered_429, True

        body: bytes | None = None
        try:
            try:
                async with asyncio.timeout(None) as sending.guard:
                    status, headers, body = await self._follow(url, rec, sending, start)
                rec.status = status
                rec.content_type = headers.get("content-type")
                rec.blocked = detect_block(status, headers, body or b"")
            except _TooManyRedirects as exc:
                rec.error, rec.error_detail = "too_many_redirects", error_detail(exc)
            except _BadRedirect as exc:
                rec.error, rec.error_detail = "bad_redirect", error_detail(exc)
            except _RedirectToOptout as exc:
                rec.error, rec.error_detail = "redirect_to_optout", error_detail(exc)
            except _HopRateLimited as exc:
                # a redirect led into a site that answered 429 in this pass
                rec.error, rec.error_detail = "rate_limited", error_detail(exc)
                rec.blocked = True
            except (_Deadline, TimeoutError) as exc:
                rec.error, rec.error_detail = "timeout", error_detail(exc)
            except httpx.DecodingError as exc:
                rec.error, rec.error_detail = "decode", error_detail(exc)
                body = None
            except httpx.HTTPError as exc:
                rec.error, rec.error_detail = map_exception(exc), error_detail(exc)
                body = None
            except Exception as exc:
                if sending.row_id is None:
                    raise  # never sent (a cap was reached, or the store failed): nothing to record
                rec.error = "internal"  # our own bug, kept apart from site failures
                rec.error_detail = error_detail(exc)
                body = None
        except BaseException as exc:
            if sending.row_id is not None:
                rec.error, rec.error_detail, rec.error_url = "cancelled", error_detail(exc), sending.hop
                keep_429()
                self.store.finish(sending.row_id, rec, None)
            raise
        if any(sending.browser_hops):
            rec.ua_kind = "browser"  # some hop of the attempt used the browser signature
        if rec.error is not None:
            rec.error_url = sending.hop
        keep_429()
        self.store.finish(sending.row_id, rec, body)
        refused_identified = (rec.blocked and rec.status is not None and rec.status != 429
                              and not sending.browser_hops[-1])
        return rec, refused_identified

    async def _follow(self, url: str, rec: FetchRecord, sending: _Sending, start):
        current = url
        hops = 0
        while True:
            sending.hop = current
            try:
                resp_status, headers, location, body, truncated = await self._get_once(
                    current, sending, start if hops == 0 else None)
            except _BadRedirect:
                rec.redirect_chain.append(current)
                raise
            if resp_status in _REDIRECTS and location:
                rec.redirect_chain.append(current)
                if hops >= config.MAX_REDIRECTS:
                    raise _TooManyRedirects
                try:
                    nxt = urljoin(current, location)
                except ValueError:
                    raise _BadRedirect(location) from None
                if not _usable(nxt):  # anything no request can be built from
                    raise _BadRedirect(nxt)
                if self._opted_out(urlsplit(nxt).hostname):
                    raise _RedirectToOptout  # no address of the opted-out site goes into the record
                current = nxt
                hops += 1
                continue
            rec.final_url = current
            rec.truncated = truncated
            return resp_status, headers, body

    async def _get_once(self, url: str, sending: _Sending, start=None):
        """One GET, alone on its site and at least the minimum gap after the site's last request, with the
        browser signature only when this hop's site was switched to it in this pass, or is the site whose refusal
        this attempt retries. `start` runs once the slot is held, just before the first hop of an attempt is sent.

        While another call's browser retry is pending at the site (`_hold`), a hop whose turn comes gives the turn
        back and waits until that retry has settled, at most RETRY_WAIT_SECONDS for each pending retry; then it takes
        its turn again and decides everything afresh in `_turn`. A call's own retry is never held by its own refusal,
        and no site lock is held while waiting. With no refusal anywhere nothing is held, and each hop is one turn."""
        site = url_site(url)
        lock = self._site_locks.setdefault(site, asyncio.Lock())
        waited_out = None  # a pending retry this hop already waited the whole bound for: it no longer holds this hop
        while True:
            async with lock:
                held = self._held.get((site, sending.pass_no))
                if held is None or held is sending.refusal or held is waited_out:
                    return await self._turn(url, site, sending, start)
            try:
                async with asyncio.timeout(config.RETRY_WAIT_SECONDS):
                    await held.settled.wait()
            except TimeoutError:
                waited_out = held

    async def _turn(self, url: str, site: str, sending: _Sending, start=None):
        """The hop, once its turn at the site has come (the site lock is held)."""
        last = self._last_end.get(site)
        while last is not None:
            wait = last + config.MIN_DELAY_SECONDS - self._clock()
            if wait <= 0:
                break
            await self._sleep(wait)
        if self.rate_limited(site, sending.pass_no):  # checked again when this request's turn comes
            if start is not None:
                raise RateLimited(site)  # the first hop: not sent, not recorded
            raise _HopRateLimited(url)  # a redirect hop: not sent; the attempt ends refused
        browser = self.uses_browser(site, sending.pass_no) or site == sending.trial
        sending.browser_hops.append(browser)
        client = self._client(browser)
        if start is not None:
            start()
        remaining = config.TOTAL_TIMEOUT - sending.spent
        remaining_loop = config.TOTAL_TIMEOUT - sending.spent_loop
        if remaining <= 0 or remaining_loop <= 0:
            raise _Deadline  # earlier hops' answers used the whole limit; this hop is not sent
        loop = asyncio.get_running_loop()
        sent, sent_loop = self._clock(), loop.time()
        sending.deadline = sent + remaining
        sending.guard.reschedule(sent_loop + remaining_loop)  # the clock runs only while this hop is out
        _redirect_seen.set(False)
        try:
            request = client.build_request("GET", url)
            sending.jar.set_cookie_header(request)  # only cookies set earlier in this same attempt
            resp = await client.send(request, stream=True)
            try:
                if resp.status_code == 429:  # before anything else, while the slot is still held
                    self._rate_limited.add((site, sending.pass_no))
                    sending.answered_429 = url
                    self.store.mark_429(sending.row_id, url)
                    return 429, {k.lower(): v for k, v in resp.headers.items()}, None, None, False  # body unread
                sending.jar.extract_cookies(resp)
                body = bytearray()
                truncated = False
                if resp.status_code not in _REDIRECTS:
                    async for chunk in resp.aiter_bytes():
                        body += chunk
                        if self._clock() > sending.deadline:
                            raise _Deadline
                        if len(body) > sending.max_bytes:
                            del body[sending.max_bytes:]
                            truncated = True
                            break
                headers = {k.lower(): v for k, v in resp.headers.items()}
                location, body = resp.headers.get("location"), bytes(body)
                if (sending.refusal is not None and sending.trial is None and not browser
                        and not (resp.status_code in _REDIRECTS and location)
                        and detect_block(resp.status_code, headers, body)):
                    # the last hop of a call's first attempt refused the identified signature (a 429 returned above):
                    # held from now, while this hop still holds the site's turn, until the call's retry has settled
                    self._hold(site, sending)
                return resp.status_code, headers, location, body, truncated
            finally:
                await resp.aclose()
        except (httpx.InvalidURL, httpx.RemoteProtocolError) as exc:
            if _redirect_seen.get():  # httpx could not build a request from the Location it received
                raise _BadRedirect(url) from exc
            raise
        finally:
            self._last_end[site] = self._clock()
            sending.spent += self._clock() - sent
            sending.spent_loop += loop.time() - sent_loop
            if not sending.guard.expired():
                sending.guard.reschedule(None)  # stopped while the next hop waits for its slot
