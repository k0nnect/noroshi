"""Crawl orchestration: which files and pages are requested for each domain, and in what order.

The order, the caps (12 sent requests per listed domain in a pass; 12 per site beyond the homepage requests of
the listed domains that lead to it) and the stop conditions are published commitments. Do not reorder or add requests without changing the protocol text.
"""
import asyncio
import logging
import re
import sqlite3
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
import pandas as pd

from noroshi import config
from noroshi.blocking import MIN_VISIBLE_CHARS, VisibleText, looks_like_html
from noroshi.discover import PROBE_PATHS, extract_links, is_platform_host, select_candidates
from noroshi.domains import normalize_url, third_party_domains, url_key
from noroshi.fetch import (BudgetExceeded, NotLive, OptedOut, RateLimited, SiteBudgetExceeded, UnusableURL, _usable,
                           site_of, url_site)
from noroshi.robots import RobotsPolicy
from noroshi.sectxt_parse import FORBIDDEN, classify_security_txt, is_forbidden, parse_security_txt, precheck
from noroshi.store import NOT_OUTCOMES, FetchRecord, Store, homepage_record, is_answer

HEALTH_TERMS = ("hospital", "health", "medical", "clinic")
CORE_PURPOSES = ("homepage", "sectxt_wellknown", "sectxt_root", "cmshpt")
DONE_STATUSES = ("measured", "blocked", "unreachable", "mismatch", "optout")
_PARSED_OK = ("valid", "valid_with_problems")
_NOT_TEXT = ("unmeasured", "absent", "html_error", FORBIDDEN)
_TS = "%Y-%m-%dT%H:%M:%SZ"
log = logging.getLogger("noroshi.crawl")


class NotFrozen(Exception):
    """A full pass was requested but the protocol is not frozen: protocol/FROZEN.txt must name an existing
    commit at which protocol/PROTOCOL.md is identical to the working tree's."""


class _OutOfBudget(Exception):
    pass


class _OptedOut(Exception):
    pass


class _RateLimited(Exception):
    pass


class LocalOutage(Exception):
    """Many homepages in a row failed at the network level and the control request failed too."""


class _Text(VisibleText):
    """Visible text and the title (`blocking.VisibleText`), the meta tags that name the site, and meta refresh
    contents."""

    def __init__(self):
        super().__init__()
        self.meta: list[str] = []
        self.refresh: list[str] = []

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        if tag == "meta":
            a = {k.lower(): (v or "") for k, v in attrs}
            key = (a.get("name") or a.get("property") or "").lower()
            if key in ("description", "og:site_name", "og:title"):
                self.meta.append(a.get("content", ""))
            if a.get("http-equiv", "").strip().lower() == "refresh":
                self.refresh.append(a.get("content", ""))


FOR_SALE_MARKERS = ("domain is for sale", "this domain is for sale", "buy this domain", "domain for sale",
                    "this domain is parked", "domain parking")


def _parse(html: bytes) -> _Text:
    parser = _Text()
    parser.feed(html.decode("utf-8", errors="replace"))
    parser.close()
    return parser


def for_sale(html: bytes) -> bool:
    """True when the title or the visible text says the domain is for sale or parked."""
    parser = _parse(html)
    text = " ".join(" ".join([*parser.title, *parser.parts]).split()).lower()
    return any(m in text for m in FOR_SALE_MARKERS)


REFRESH_MAX_SECONDS = 5
_REFRESH = re.compile(r"\s*(\d+(?:\.\d*)?)[\d.]*\s*(?:[;,]\s*)?(?:url\s*=\s*)?(.*)", re.IGNORECASE | re.DOTALL)


def meta_refresh(html: bytes, base_url: str) -> str | None:
    """The target of `<meta http-equiv="refresh" content="N; url=TARGET">` (the first such tag) when N, read as a
    number, is 5 seconds or less and the page has fewer than 200 visible characters; None otherwise, or when no request
    can be built."""
    parser = _parse(html)
    if not parser.refresh or len(" ".join(" ".join(parser.parts).split())) >= MIN_VISIBLE_CHARS:
        return None
    found = _REFRESH.fullmatch(parser.refresh[0])
    if found is None or float(found[1]) > REFRESH_MAX_SECONDS:  # the delay as a number: 5.9 is more than 5
        return None
    target = found[2].strip()
    if target[:1] in ("'", '"'):
        target = target[1:].split(target[0], 1)[0]
    if not target:
        return None
    try:
        url = urljoin(base_url, target.strip())
    except ValueError:
        return None
    return url if _usable(url) else None


def refresh_target(rec: FetchRecord | None, store: Store) -> str | None:
    """For a homepage, candidate, Policy: or conventional-path record that answered 200 with HTML carrying a meta
    refresh (`meta_refresh`): the target, requested once with the same purpose. Its response stands in for the page
    (for the homepage, it is the homepage). One step only; a refresh to the page's own address is not followed."""
    if rec is None or not is_answer(rec.status, rec.blocked, rec.error) or rec.status != 200:
        return None
    body = _blob(store, rec) or b""
    if not looks_like_html(rec.content_type, body):
        return None
    target = meta_refresh(body, rec.final_url or rec.url)
    if target is None or _norm_url(target) in (_norm_url(rec.url), _norm_url(rec.final_url or rec.url)):
        return None
    return target


def parked_host(host: str, domain: str) -> bool:
    """True for ww<digits>.<listed domain> (ww547.e-mercy.com): a parking service's host."""
    return re.fullmatch(r"ww\d+\." + re.escape(domain), host) is not None


def sent_on_to_parked_host(start: str | None, end: str | None, domain: str) -> bool:
    """True when a homepage was sent on from one host to ww<digits>.<listed domain>, by a redirect or by a followed
    meta refresh: a parked page. A start host of that form is not enough by itself."""
    if not start or not end:
        return False
    end_host = (urlsplit(end).hostname or "").rstrip(".")
    return parked_host(end_host, domain) and end_host != (urlsplit(start).hostname or "").rstrip(".")


def redirected_to_parked_host(rec: FetchRecord, domain: str) -> bool:
    """True when the attempt ended on ww<digits>.<listed domain> after starting on a different host."""
    return sent_on_to_parked_host(rec.url, rec.final_url, domain)


def homepage_matches(name_tokens: set[str], html: bytes, generic_terms: tuple[str, ...] = (),
                     domain: str = "") -> bool:
    """False only for a page with real text that names none of the expected words. Not used by the crawl (whether a
    homepage is the organization's site is decided after the crawl).

    Every occurrence of the full listed domain (for example medtronic.com, which also covers
    www.medtronic.com and addresses at it) is removed first, so a page that merely prints the domain does not
    match. The bare label is never removed: it is usually the company's own name. A page with under 200
    characters of visible text (typically a JavaScript shell) is never flagged.
    """
    if not name_tokens:
        return True
    parser = _parse(html)
    visible = " ".join(" ".join(parser.parts).split())
    if len(visible) < MIN_VISIBLE_CHARS:
        return True
    haystack = " ".join([*parser.title, visible, *parser.meta]).lower()
    if domain:
        haystack = haystack.replace(domain.lower(), " ")
    return any(t.lower() in haystack for t in (*name_tokens, *generic_terms))


def normalize_domain(raw: str) -> str:
    return str(raw).strip().lower().rstrip(".")


def _blob(store: Store, rec: FetchRecord | None) -> bytes | None:
    return store.get_blob(rec.body_sha256) if rec is not None and rec.body_sha256 else None


def _sectxt_class(rec: FetchRecord, store: Store, now: datetime, classifier=classify_security_txt) -> str:
    return classifier(rec.status, rec.final_url, rec.content_type, _blob(store, rec), rec.blocked, rec.error, now)


PAGE_PURPOSES = ("homepage", "policy_field", "candidate", "probe")  # an answered page of these purposes counts as
# requested at its final address too (fix round 3, K2; a core file's final address does not: fix round 4, N1)


def _norm_url(url: str) -> str:
    """The key of a URL in the set of URLs already requested (`domains.url_key`, also used to de-duplicate
    candidates)."""
    return url_key(url)


def other_host_form(start_url: str) -> str | None:
    """The start URL's other host form: www. added when its host has none, removed when it has one; same scheme,
    path /."""
    parts = urlsplit(start_url)
    host = (parts.hostname or "").rstrip(".")
    other = host[4:] if host.startswith("www.") else f"www.{host}"
    if "." not in other:
        return None
    return normalize_url(f"{parts.scheme}://{other}" + (f":{parts.port}" if parts.port else "") + "/")


def robots_policy(robots: FetchRecord | None, store: Store) -> RobotsPolicy:
    """The robots decision for pages on the final host, from its robots.txt record: 200 is parsed; any 4xx allows;
    anything else (5xx, a network error, a block, no completed record) means disallow-all (RFC 9309). Derive
    recomputes the decision with this same function."""
    if robots is None or robots.blocked or robots.error or robots.status is None:
        return RobotsPolicy.disallow_all()
    if robots.status == 200:
        return RobotsPolicy((_blob(store, robots) or b"").decode("utf-8-sig", errors="replace"))
    if 400 <= robots.status < 500:
        return RobotsPolicy(None)
    return RobotsPolicy.disallow_all()


def _fetchable_policy(policy_url: str | None, robots_ok) -> bool:
    if not policy_url:
        return False
    try:  # the value is site-controlled and may be malformed
        return (urlsplit(policy_url).scheme in ("http", "https") and not is_platform_host(policy_url)
                and robots_ok(policy_url))
    except ValueError:
        return False


NETWORK_ERRORS = ("dns", "connect", "timeout", "tls")
OUTAGE_STREAK = 20


class OutageWatch:
    """Counts consecutive domains whose homepage failed with a network error (at both host forms). At
    OUTAGE_STREAK in a row it asks the control (the study's own info page) once: if the control fails too, those
    domains (and any whose first form failed while its other form is not settled) are unmarked, their homepage
    records relabelled `outage`, and the run stops; if it loads, the count starts again. Once it has stopped the
    run, a homepage network error that still lands is relabelled `outage` too, and its domain is left unmarked; the
    control is not asked again."""

    def __init__(self, store: Store, pass_no: int, control):
        self.store = store
        self.pass_no = pass_no
        self.control = control
        self.streak: list[str] = []
        self.pending: set[str] = set()  # first host form failed at the network level, other form not settled
        self._checking = False
        self.tripped = False

    def other_form_pending(self, domain: str) -> None:
        """The domain's first homepage form failed at the network level and its other form comes next: not counted
        (a domain is a network failure only when both forms failed), but relabelled if the run stops first."""
        self.pending.add(domain)

    def other_form_settled(self, domain: str) -> None:
        """The request for the domain's other form returned or raised, by whatever path (a fresh fetch, a stored or
        reused record, or a stop before any fetch): the domain is no longer pending, so a later stop never touches
        it."""
        self.pending.discard(domain)

    async def homepage(self, domain: str, rec: FetchRecord) -> None:
        self.pending.discard(domain)
        if rec.error not in NETWORK_ERRORS:
            if not self.tripped:
                self.streak.clear()
            return
        if self.tripped:  # landed after the stop, before cancellation reached it
            self.store.mark_outage([domain], self.pass_no, NETWORK_ERRORS)
            raise LocalOutage("a homepage failed at the network level after the outage stop")
        self.streak.append(domain)
        if len(self.streak) < OUTAGE_STREAK or self._checking:
            return
        caught = list(self.streak)
        self._checking = True
        try:
            ok = await self.control()
        finally:
            self._checking = False
        if ok:
            self.streak.clear()
            return
        self.tripped = True
        doomed = list(dict.fromkeys(caught + self.streak + sorted(self.pending)))
        self.store.mark_outage(doomed, self.pass_no, NETWORK_ERRORS)
        raise LocalOutage(f"{len(doomed)} homepages in a row failed at the network level and the control "
                          "request to the study's info page failed too; this looks like a local outage")


def live_url(live_file: Path) -> str | None:
    """The study's info page URL: the first http:// or https:// URL in the LIVE file, or None."""
    try:
        found = re.search(r"https?://\S+", Path(live_file).read_text(encoding="utf-8"))
    except OSError:
        return None
    return found.group(0) if found else None


def live_control(live_file: Path, log=None):
    """The control check: one GET of the info page URL written in the LIVE file. False when the file names
    no URL or the page does not load. Each request is reported to `log(line)` (the run log) with its URL and
    result: it goes to the study's own page, so it is the one request with no store row."""
    note = log or (lambda line: logging.getLogger("noroshi.crawl").warning("%s", line))

    async def control() -> bool:
        url = live_url(live_file)
        if url is None:
            note(f"control request not sent: {live_file} names no info page URL; counted as failed")
            return False
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30.0,
                                         headers={"User-Agent": config.UA_IDENTIFIED}) as client:
                resp = await client.get(url)
        except httpx.HTTPError as exc:
            note(f"control request to {url}: failed ({type(exc).__name__})")
            return False
        ok = resp.status_code < 400
        note(f"control request to {url}: {'ok' if ok else 'failed'} (status {resp.status_code})")
        return ok
    return control


async def crawl_domain(row, pass_no: int, fetcher, store: Store, now: datetime,
                       classifier=classify_security_txt, watch: OutageWatch | None = None) -> str:
    domain = normalize_domain(row["domain"])
    sector = row["sector"]
    start_url = row["start_url"]
    own_sites = {domain, url_site(start_url)}  # the measured site; its final host's site joins after the homepage
    other = other_host_form(start_url)

    # records left by an interrupted run are reused, so a restart never repeats a completed request.
    # A cancelled, in-flight (hard kill) or outage attempt is not an outcome, and neither is a blocked
    # identified attempt whose browser retry never completed (status 429 is final: it is never retried).
    # Every one of them still counts toward the caps.
    prior: dict[tuple[str, str], FetchRecord] = {}
    for r in store.requests_for(domain, pass_no):
        if r.error in NOT_OUTCOMES:
            continue
        prior[(r.purpose, normalize_url(r.url))] = r
    for key, r in list(prior.items()):
        if r.blocked and r.ua_kind == "identified" and r.status is not None and r.status != 429:
            del prior[key]
    fetched_urls: set[str] = set()

    def reuse(url: str, purpose: str) -> FetchRecord | None:
        """A copy of a measurement of this URL made earlier in the pass (for any listed domain), so the site
        is not asked twice. The copy counts toward no cap."""
        found = store.find_measurement(url, pass_no)
        if found is None:
            return None
        copy = replace(found, domain=domain, sector=sector, purpose=purpose, reused=True)
        store.record(copy, None)
        return copy

    def rate_limited() -> bool:
        return any(fetcher.rate_limited(site, pass_no) for site in own_sites)

    def other_form(first: FetchRecord | None) -> str | None:
        """The other host form to request after the first homepage attempt: only after a network error, and not when
        any homepage attempt of the first form in this pass was refused (decided from the store, so a restart
        decides the same), when the other form was already a redirect hop of that attempt, or when the error came at
        the other form's own host."""
        if other is None or first is None or first.error not in NETWORK_ERRORS:
            return None
        start = _norm_url(start_url)
        if any(r.purpose == "homepage" and r.blocked and _norm_url(r.url) == start
               for r in store.requests_for(domain, pass_no)):
            return None  # the identified attempt was refused: no other form after a refusal
        if _norm_url(other) in {_norm_url(u) for u in first.redirect_chain}:
            return None  # requested already, and answered with a redirect
        failed_at = (urlsplit(first.error_url).hostname or "").rstrip(".") if first.error_url else ""
        return None if failed_at == urlsplit(other).hostname else other

    def stored_other_form() -> FetchRecord | None:
        """On a rerun after an outage stop: the other form's stored answer, when the first form's failure was
        relabelled `outage` (it has no outcome) and the other form had answered before the stop."""
        if other is None or ("homepage", normalize_url(start_url)) in prior:
            return None
        rec = prior.get(("homepage", normalize_url(other)))
        return rec if rec is not None and is_answer(rec.status, rec.blocked, rec.error) else None

    async def req(url: str, purpose: str, next_form=None) -> FetchRecord | None:
        """The record for this request, or None when it is not made: a third party opted out, used its site
        cap or answered 429 earlier in the pass, or no request can be built from the URL. `next_form`: for the first
        homepage request, `other_form`, which says whether another homepage request follows."""
        if rate_limited():
            raise _RateLimited  # the measured site answered 429 in this pass: nothing more for this domain
        url = normalize_url(url)  # one spelling for the request, its record and every lookup
        fetched_urls.add(_norm_url(url))
        rec = prior.get((purpose, url)) or reuse(url, purpose)
        if rec is None:
            if store.count_requests(domain, pass_no) >= config.MAX_REQUESTS_PER_DOMAIN:
                raise _OutOfBudget
            try:
                rec = await fetcher.fetch(domain, sector, url, purpose, pass_no, reuse=lambda: reuse(url, purpose))
            except BudgetExceeded:
                raise _OutOfBudget from None
            except SiteBudgetExceeded as exc:
                if exc.site in own_sites:
                    raise _OutOfBudget from None  # the measured site used its cap: the domain ends here
                return None
            except OptedOut:
                if url_site(url) in own_sites:
                    raise _OptedOut from None
                return None
            except RateLimited as exc:
                if exc.site in own_sites:
                    raise _RateLimited from None
                return None
            except UnusableURL:
                return None
            if watch is not None and purpose == "homepage" and not rec.reused:
                # a domain is a network failure only when both forms failed: when the other form follows, the
                # caller marks the domain pending just before sending it, and the watch hears of the other form
                if not (next_form is not None and next_form(rec) and not watch.tripped):
                    await watch.homepage(domain, rec)
        if rec.purpose in PAGE_PURPOSES and rec.final_url and is_answer(rec.status, rec.blocked, rec.error):
            fetched_urls.add(_norm_url(rec.final_url))  # a page this domain got an answer from is not asked again
        if rate_limited() or (rec.status == 429 and url_site(rec.final_url or url) in own_sites):
            raise _RateLimited
        return rec

    def finish(status: str) -> str:
        store.mark_done(domain, pass_no, status)
        return status

    # The homepage. For a domain that ends measured, the record it is measured from is always the newest homepage
    # record of the pass among outcomes (store.homepage_record, which look, needs_retry and is_forbidden read): the
    # start URL, then the other host form, then a meta-refresh target, each newer than the last, and a stored answer
    # is never followed by a resent earlier form.
    try:
        if stored_other_form() is not None:
            fetched_urls.add(_norm_url(start_url))  # sent before the stop; its failure was relabelled outage
            home = await req(other, "homepage")  # the stored answer of the other form: nothing is sent
        else:
            home = await req(start_url, "homepage", next_form=other_form)
        if (second := other_form(home)) is not None:
            if watch is not None:
                watch.other_form_pending(domain)  # no await between the first form's answer and this mark
            try:
                home = await req(second, "homepage") or home  # once per pass, counted in the 12; no plain-http fallback
            finally:
                if watch is not None:
                    watch.other_form_settled(domain)
        refreshed_to_parked = False
        target = refresh_target(home, store)
        if target and _norm_url(target) not in fetched_urls:  # a meta refresh: its target is the homepage
            own_sites.add(url_site(target))  # an opted-out or rate-limited target ends the domain, as a redirect would
            refreshed_to_parked = sent_on_to_parked_host(home.final_url or home.url, target, domain)
            home = await req(target, "homepage") or home
    except _OutOfBudget:
        raise RuntimeError(f"no budget for the homepage of {domain}") from None
    except _OptedOut:
        return finish("optout")
    except _RateLimited:
        return finish("blocked")
    if home is None:
        raise RuntimeError(f"no request can be built from the start URL of {domain}: {start_url}")
    if refreshed_to_parked or any(redirected_to_parked_host(r, domain) for r in store.requests_for(domain, pass_no)
                                  if r.purpose == "homepage" and r.error not in NOT_OUTCOMES):
        return finish("mismatch")  # the homepage was sent on to ww<digits>.<listed domain>: a parked page
    if home.blocked:
        return finish("blocked")
    if home.error == "redirect_to_optout":
        return finish("optout")
    if home.error or home.status != 200:
        return finish("unreachable")  # only a homepage that answers 200 is measured

    final = urlsplit(normalize_url(home.final_url or start_url))  # every address below is built in one spelling
    base = f"{final.scheme}://{final.netloc}"
    final_host = (final.hostname or "").lower().rstrip(".")
    final_site = site_of(final_host)
    if final_site in third_party_domains(sector):
        return finish("mismatch")  # the homepage lands on a third-party platform or a parking service
    home_body = _blob(store, home) or b""
    if for_sale(home_body):
        return finish("mismatch")
    own_sites.add(final_site)

    try:
        robots = await req(base + "/robots.txt", "robots")
        policy = robots_policy(robots, store)

        def robots_ok(url: str) -> bool:
            parts = urlsplit(url)
            if (parts.hostname or "").lower().rstrip(".") != final_host:
                return True  # another site's robots.txt is never fetched
            return policy.allowed((parts.path or "/") + (f"?{parts.query}" if parts.query else ""))

        async def page(url: str, purpose: str) -> None:
            """A Policy:, candidate or conventional-path request and, when it answered with a meta refresh, one
            request to the target with the same purpose (robots.txt permitting)."""
            target = refresh_target(await req(url, purpose), store)
            if target and _norm_url(target) not in fetched_urls and robots_ok(target):
                await req(target, purpose)

        files: dict[str, FetchRecord | None] = {}
        files["wellknown"] = await req(base + "/.well-known/security.txt", "sectxt_wellknown")
        wk = files["wellknown"]
        if not (wk and _sectxt_class(wk, store, now, classifier) in _PARSED_OK):
            files["root"] = await req(base + "/security.txt", "sectxt_root")
        if sector == "hospital":
            await req(base + "/cms-hpt.txt", "cmshpt")

        policy_url = None
        for key in ("wellknown", "root"):
            rec = files.get(key)
            if rec is None or _sectxt_class(rec, store, now) in _NOT_TEXT:
                continue
            urls = parse_security_txt(_blob(store, rec) or b"", now).fields.get("policy", [])
            if urls:
                policy_url = urls[0]
                break
        n_candidates = 3
        if _fetchable_policy(policy_url, robots_ok) and _norm_url(policy_url) not in fetched_urls:
            await page(policy_url, "policy_field")
            n_candidates = 2

        if home.status == 200:
            wanted, _platform = select_candidates(extract_links(home_body, home.final_url or start_url))
            for url in wanted[:n_candidates]:
                if _norm_url(url) in fetched_urls or not robots_ok(url):
                    continue
                await page(url, "candidate")

        for path in PROBE_PATHS:
            url = base + path
            if _norm_url(url) in fetched_urls or not policy.allowed(path):
                continue
            await page(url, "probe")
    except _OutOfBudget:
        pass
    except _OptedOut:
        return finish("optout")
    except _RateLimited:
        return finish("blocked")  # a 429 from the measured site ends the domain for the pass
    return finish("measured")


def needs_retry(store: Store, domain: str) -> bool:
    """Pass 2 retries a domain that pass 1 left blocked or unreachable, or measured with a core file unmeasured (a
    `forbidden` file is not), with a core file the plan called for and no record of it at all (the well-known
    security.txt; the root one unless the well-known file parsed; cms-hpt.txt for a hospital), or with a look for a
    policy page left incomplete for a reason a retry can fix (look.RETRY_REASONS)."""
    from noroshi.look import retry_reasons  # look replays this module's rules, so it imports this module

    status = store.done_status(domain, 1)
    if status in ("blocked", "unreachable"):
        return True
    if status != "measured":
        return False  # mismatch, optout, or never crawled
    records = store.requests_for(domain, 1)
    home = homepage_record(records)  # the record the crawl measured from
    if home is None or precheck(home.status, home.content_type, _blob(store, home), home.blocked,
                                home.error) == "unmeasured":
        return True
    last: dict[str, FetchRecord] = {}
    for r in records:
        if r.purpose in CORE_PURPOSES and r.purpose != "homepage":
            last[r.purpose] = r  # a browser retry replaces the blocked attempt; an in-flight row is unmeasured
    if any(precheck(r.status, r.content_type, _blob(store, r), r.blocked, r.error,
                    is_forbidden(records, purpose, lambda x: _blob(store, x))) == "unmeasured"
           for purpose, r in last.items()):
        return True
    wk = last.get("sectxt_wellknown")  # the files crawl_domain called for, as it decided them
    planned = ["sectxt_wellknown"]
    if not (wk and _sectxt_class(wk, store, datetime.now(timezone.utc)) in _PARSED_OK):
        planned.append("sectxt_root")
    if records and records[0].sector == "hospital":
        planned.append("cmshpt")
    if any(purpose not in last for purpose in planned):
        return True
    return bool(retry_reasons(store, records))


def _pass1_start(store: Store, domain: str) -> datetime | None:
    recs = store.requests_for(domain, 1)
    if not recs:
        return None
    return datetime.strptime(recs[0].started_at, _TS).replace(tzinfo=timezone.utc)


def is_pilot(limit: int | None, n_domains: int) -> bool:
    """A limit below the table size is a pilot; anything else is a full pass."""
    return limit is not None and limit < n_domains


def frozen_problem(frozen_file: Path) -> str | None:
    """None when `frozen_file` (protocol/FROZEN.txt) names a commit that exists and protocol/PROTOCOL.md in the
    working tree is identical to its version at that commit; otherwise what is wrong."""
    frozen_file = Path(frozen_file)
    if not frozen_file.exists():
        return f"{frozen_file} does not exist"
    words = frozen_file.read_text(encoding="utf-8").split()
    if not words or not re.fullmatch(r"[0-9a-f]{7,40}", words[0]):
        return f"{frozen_file} does not hold a commit hash"
    commit, protocol = words[0], frozen_file.parent
    git = ["git", "-C", str(protocol)]
    if subprocess.run(git + ["cat-file", "-e", f"{commit}^{{commit}}"], capture_output=True).returncode != 0:
        return f"commit {commit} named in {frozen_file} does not exist in this repository"
    shown = subprocess.run(git + ["show", f"{commit}:./PROTOCOL.md"], capture_output=True)
    if shown.returncode != 0:
        return f"protocol/PROTOCOL.md does not exist at commit {commit}"
    current = protocol / "PROTOCOL.md"
    if not current.exists() or current.read_bytes() != shown.stdout:
        return f"protocol/PROTOCOL.md differs from its version at commit {commit}"
    return None


def excluded_rows(domains: pd.DataFrame, exclude: frozenset[str]) -> pd.Series:
    """The rows of a sector table whose listed domain is in `exclude` (the names `cli.load_optout` reads from the
    `--exclude` file)."""
    return domains["domain"].map(normalize_domain).isin(exclude)


def draw_sample(domains: pd.DataFrame, limit: int, seed: int | None,
                exclude: frozenset[str] = frozenset()) -> pd.DataFrame:
    """A pilot's sample of one sector table: `limit` rows drawn with `seed`, after the excluded domains are removed
    (all that remain when fewer than `limit` do). Without an exclusion the sample is exactly the one drawn before
    exclusions existed."""
    if exclude:
        domains = domains[~excluded_rows(domains, exclude)]
    return domains.sample(n=min(limit, len(domains)), random_state=seed)


def require_frozen(limit: int | None, n_domains: int, frozen_file: Path | None = None) -> None:
    frozen_file = config.paths().frozen if frozen_file is None else Path(frozen_file)
    if is_pilot(limit, n_domains):
        return
    problem = frozen_problem(frozen_file)
    if problem:
        raise NotFrozen(f"a full pass needs a frozen protocol: {problem}; use --limit for a pilot")


async def crawl_sector(domains: pd.DataFrame, pass_no: int, fetcher, store: Store, limit: int | None = None,
                       seed: int | None = None, *, now: datetime | None = None,
                       frozen_file: Path | None = None, control=None,
                       watch: OutageWatch | None = None, on_done=None,
                       exclude: frozenset[str] = frozenset()) -> dict[str, int]:
    """`watch` carries the outage count across sectors of one run; without it a new one is made with
    `control` (by default the info page URL in the fetcher's LIVE file). `on_done(status)` is called as each
    domain ends. `exclude` (a pilot only) names listed domains left out before the sample is drawn; whether the run is
    a pilot is decided from the whole table."""
    require_frozen(limit, len(domains), frozen_file)
    now = now or datetime.now(timezone.utc)
    if watch is None:
        watch = OutageWatch(store, pass_no, control or live_control(fetcher.live_file))
    if is_pilot(limit, len(domains)):
        domains = draw_sample(domains, limit, seed, exclude)
    elif exclude:
        raise ValueError("an exclusion list is for a pilot sample only")
    counts: dict[str, int] = {}

    def count(key: str) -> None:
        counts[key] = counts.get(key, 0) + 1

    sem = asyncio.Semaphore(config.GLOBAL_CONCURRENCY)

    async def one(row) -> str:
        name = normalize_domain(row["domain"])
        done = store.done_status(name, pass_no)
        if done:
            return done  # resume
        if pass_no >= 2:
            if not needs_retry(store, name):
                return ""
            start = _pass1_start(store, name)
            if start is not None and now - start < timedelta(hours=config.RETRY_PASS_MIN_HOURS):
                return "skipped_too_early"
        async with sem:
            try:
                return await crawl_domain(row, pass_no, fetcher, store, now, watch=watch)
            except (NotLive, LocalOutage, sqlite3.Error, OSError):
                raise  # the whole run must stop: no live page, a local outage, or a store that cannot record
            except Exception:
                # one bad domain must not wedge the sector; no done marker, so a rerun tries it again
                log.exception("crawl failed for %s (pass %d)", name, pass_no)
                return "error"

    tasks = [asyncio.create_task(one(row)) for _, row in domains.iterrows()]
    try:
        for fut in asyncio.as_completed(tasks):
            key = await fut
            if key:
                count(key)
            if on_done is not None:
                on_done(key)
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return counts
