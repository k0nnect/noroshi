"""The look for a policy page: which pages the crawler meant to examine for a domain, which it examined, and why a
look was incomplete.

A page the crawler skipped leaves no record, so the crawler's choices for a pass are replayed from that pass's
stored records with the crawler's own rules (its robots decision, Policy-field rule, link discovery and URL
normalization are imported, not copied). Across passes the look is judged on both passes together: a gap that
either pass closed is not reported. Reasons that need the judgements (an unreadable page, a page with no
judgement) are added by derive; the reasons here need only the store.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from noroshi.blocking import MIN_VISIBLE_CHARS, visible_text
from noroshi.crawl import (_NOT_TEXT, PAGE_PURPOSES, _blob, _fetchable_policy, _norm_url, _sectxt_class,
                           refresh_target, robots_policy)
from noroshi.discover import PROBE_PATHS, extract_links, select_candidates
from noroshi.domains import normalize_url
from noroshi.fetch import _usable
from noroshi.sectxt_parse import parse_security_txt
from noroshi.store import NOT_OUTCOMES, FetchRecord, Store, homepage_record, is_answer, purged_key

ROBOTS_UNREADABLE = "robots_unreadable"  # robots.txt gave disallow-all: 5xx, failed, refused or no completed record
ROBOTS_DISALLOWED = "robots_disallowed"  # robots.txt was read and disallows all six conventional paths
CANDIDATE_DISALLOWED = "candidate_disallowed"  # a candidate link, Policy: URL or the meta-refresh target of a page
# was skipped for robots.txt
CANDIDATE_SKIPPED = "candidate_skipped"  # a candidate link or Policy: URL (or its meta-refresh target) was never
# requested for another reason, or any planned page whose record a purge of an opted-out site removed
REQUEST_FAILED = "request_failed"  # a candidate, Policy:, conventional-path or meta-refresh-target request was not an
# answer: it failed, was refused, or ended with a status other than 200 or a 4xx
UNREADABLE_PAGE = "unreadable_page"  # the judge could not read a stored page: pdf_unreadable, non_text, or
# unreadable (fewer than 200 visible characters, or cut at the size cap and screened out)
NOT_JUDGED = "not_judged"  # a stored page that should have been judged has no judgement row
HOMEPAGE_TRUNCATED = "homepage_truncated"  # the homepage was cut at the body cap, so links may be missing
HOMEPAGE_UNREADABLE = "homepage_unreadable"  # the homepage has fewer than 200 visible characters, so no links could
# be read from it (a script shell, a challenge, an error page)
REASONS = (ROBOTS_UNREADABLE, ROBOTS_DISALLOWED, CANDIDATE_DISALLOWED, CANDIDATE_SKIPPED, REQUEST_FAILED,
           UNREADABLE_PAGE, NOT_JUDGED, HOMEPAGE_TRUNCATED, HOMEPAGE_UNREADABLE)  # their order in the policy_look column
RETRY_REASONS = frozenset({ROBOTS_UNREADABLE, REQUEST_FAILED})  # what a retry in pass 2 can fix
_CORE = ("homepage", "robots", "sectxt_wellknown", "sectxt_root", "cmshpt")
_GAPS = (CANDIDATE_DISALLOWED, CANDIDATE_SKIPPED, REQUEST_FAILED)


def outcomes_only(records: list[FetchRecord]) -> list[FetchRecord]:
    """Records that are outcomes: not cancelled, still in flight, or relabelled outage."""
    return [r for r in records if r.error not in NOT_OUTCOMES]


def is_measurement(r: FetchRecord) -> bool:
    """A record that is an answer (`store.is_answer`): completed, not refused, and a 200 or a 4xx."""
    return is_answer(r.status, r.blocked, r.error)


def _answered_at(records) -> set[str]:
    """The final addresses (in the crawl's key) of the answered page records (`crawl.PAGE_PURPOSES`)."""
    return {_norm_url(r.final_url) for r in records if r.purpose in PAGE_PURPOSES and r.final_url and is_measurement(r)}


def _last(records: list[FetchRecord], purpose: str) -> FetchRecord | None:
    found = [r for r in records if r.purpose == purpose]
    return found[-1] if found else None


@dataclass
class Look:
    reasons: list[str]  # why the look was incomplete, in REASONS order; empty when complete
    probes_cut: int  # conventional paths robots.txt allowed that no pass requested (the request budget ran out)
    homepage_truncated: bool  # no measured pass read the whole homepage
    homepage_unreadable: bool = False  # no measured pass's homepage had 200 visible characters


@dataclass
class _PassLook:
    readable: bool
    all_probes_disallowed: bool
    truncated: bool
    unreadable: bool  # the homepage has fewer than MIN_VISIBLE_CHARS visible characters
    gaps: dict[str, set[str]]  # reason -> normalized URLs
    cut: set[str]  # normalized URLs of conventional paths never requested


def _pass_look(store: Store, records: list[FetchRecord]) -> _PassLook | None:
    """Replay one measured pass of `crawl_domain` after its homepage: the robots decision, the Policy: URL, the
    candidate links and the conventional paths, then compare with what the pass recorded."""
    recs = outcomes_only(records)
    home = homepage_record(recs)  # the record crawl_domain measured from
    if home is None or home.blocked or home.error or home.status != 200:  # as in crawl_domain: only a 200 measures
        return None
    final = urlsplit(normalize_url(home.final_url or home.url))  # as in crawl_domain: one spelling
    base = f"{final.scheme}://{final.netloc}"
    final_host = (final.hostname or "").lower().rstrip(".")
    policy = robots_policy(_last(recs, "robots"), store)

    def robots_ok(url: str) -> bool:  # as in crawl_domain: the final host's robots.txt covers that host only
        parts = urlsplit(url)
        if (parts.hostname or "").lower().rstrip(".") != final_host:
            return True
        return policy.allowed((parts.path or "/") + (f"?{parts.query}" if parts.query else ""))

    gaps: dict[str, set[str]] = {g: set() for g in _GAPS}
    planned: list[tuple[str, str]] = []
    # as crawl_domain's req(): an address counts as requested once it was requested, or once an answered page record
    # ended there (fix round 3, K2; fix round 4, N1)
    fetched = {_norm_url(r.url) for r in recs if r.purpose in _CORE} | _answered_at(r for r in recs if r.purpose in _CORE)
    # the last record of each requested URL (a browser retry after a refusal), by its one spelling
    by_url = {normalize_url(r.url): r for r in recs}

    def requested(url: str) -> None:
        fetched.add(_norm_url(url))
        rec = by_url.get(normalize_url(url))
        fetched.update(_answered_at([rec] if rec is not None else []))

    def disallowed(url: str) -> None:
        if not policy.unreachable:  # under an unreadable robots.txt this is ROBOTS_UNREADABLE
            gaps[CANDIDATE_DISALLOWED].add(_norm_url(url))

    def plan(kind: str, url: str) -> None:
        """As crawl_domain's page(): the page, then the target of its meta refresh with the same kind."""
        planned.append((kind, url))
        requested(url)
        target = refresh_target(by_url.get(normalize_url(url)), store)
        if target and _norm_url(target) not in fetched:
            if robots_ok(target):
                planned.append((kind, target))
                requested(target)
            else:
                disallowed(target)

    now = datetime.now(timezone.utc)  # only the text/not-text decision is needed: it does not depend on the time
    policy_url = None
    for rec in (_last(recs, "sectxt_wellknown"), _last(recs, "sectxt_root")):
        if rec is None or _sectxt_class(rec, store, now) in _NOT_TEXT:
            continue
        urls = parse_security_txt(_blob(store, rec) or b"", now).fields.get("policy", [])
        if urls:
            policy_url = urls[0]
            break
    n_candidates = 3
    if policy_url:
        if _fetchable_policy(policy_url, robots_ok):
            if _norm_url(policy_url) not in fetched:
                plan("policy_field", policy_url)
                n_candidates = 2
        elif _fetchable_policy(policy_url, lambda url: True):
            disallowed(policy_url)
    if home.status == 200:
        wanted, _platforms = select_candidates(extract_links(_blob(store, home) or b"", home.final_url or home.url))
        for url in wanted[:n_candidates]:
            if _norm_url(url) in fetched:
                continue
            if not robots_ok(url):
                disallowed(url)
                continue
            plan("candidate", url)
    for path in PROBE_PATHS:
        url = base + path
        if _norm_url(url) in fetched or not policy.allowed(path):
            continue
        plan("probe", url)

    cut: set[str] = set()
    purged = store.purged(home.domain, home.pass_no)  # pages whose records a purge of an opted-out site removed
    for kind, url in planned:
        rec = by_url.get(normalize_url(url))
        if rec is None and purged_key(url) in purged:  # its site opted out after the crawl: skipped, whatever its kind
            gaps[CANDIDATE_SKIPPED].add(_norm_url(url))
        elif rec is None:  # never sent: a request budget ran out, or a third party was opted out or rate-limited
            if kind == "probe":
                cut.add(_norm_url(url))
            elif _usable(url):
                gaps[CANDIDATE_SKIPPED].add(_norm_url(url))
        elif not is_measurement(rec):
            gaps[REQUEST_FAILED].add(_norm_url(url))
    return _PassLook(readable=not policy.unreachable,
                     all_probes_disallowed=not any(policy.allowed(path) for path in PROBE_PATHS),
                     truncated=home.truncated,
                     unreadable=len(visible_text(_blob(store, home) or b"")) < MIN_VISIBLE_CHARS, gaps=gaps, cut=cut)


def look(store: Store, records_by_pass: dict[int, list[FetchRecord]], measured_passes) -> Look:
    """The look over the passes that measured the domain, judged on both passes together:
    - robots.txt is unreadable only when no measured pass read it (a 200 or a 4xx);
    - robots.txt disallows the conventional paths only when every measured pass that read it does;
    - a skipped, disallowed or failed page is cleared when any pass holds a measurement of that URL;
    - the homepage is truncated only when no measured pass read it whole;
    - the homepage is unreadable only when every measured pass's homepage had fewer than 200 visible characters;
    - a conventional path is cut only when no pass requested it."""
    looks = [lk for p in measured_passes if (lk := _pass_look(store, records_by_pass.get(p, []))) is not None]
    every = [r for recs in records_by_pass.values() for r in outcomes_only(recs)]
    # a page counts as read, and as requested, through a record of its address or an answered page record that ended
    # there
    measured = {_norm_url(r.url) for r in every if is_measurement(r)} | _answered_at(every)
    requested = {_norm_url(r.url) for r in every} | _answered_at(every)
    reasons = set()
    readable = [lk for lk in looks if lk.readable]
    if not readable:
        reasons.add(ROBOTS_UNREADABLE)
    elif all(lk.all_probes_disallowed for lk in readable):
        reasons.add(ROBOTS_DISALLOWED)
    for gap in _GAPS:
        if set().union(*(lk.gaps[gap] for lk in looks)) - measured:
            reasons.add(gap)
    truncated = bool(looks) and all(lk.truncated for lk in looks)
    if truncated:
        reasons.add(HOMEPAGE_TRUNCATED)
    unreadable = bool(looks) and all(lk.unreadable for lk in looks)
    if unreadable:
        reasons.add(HOMEPAGE_UNREADABLE)
    cut = set().union(*(lk.cut for lk in looks)) - requested
    return Look([r for r in REASONS if r in reasons], len(cut), truncated, unreadable)


def retry_reasons(store: Store, pass1_records: list[FetchRecord]) -> set[str]:
    """The reasons a retry can fix that left a domain's pass-1 look incomplete (used by crawl.needs_retry)."""
    return set(look(store, {1: pass1_records}, (1,)).reasons) & RETRY_REASONS
