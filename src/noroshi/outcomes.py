"""One row per website domain: what was measured, and the outcomes built from it.

Unmeasured is never absent. A core file with no completed record for a domain that was measured is
`unmeasured`. Contact values are never copied out of a stored file: only the contact types are.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pandas as pd

from noroshi.cmshpt_parse import classify_cms_hpt
from noroshi.crawl import _norm_url, normalize_domain
from noroshi.domains import domain_label, registrable_domain
from noroshi.identity import EXEMPT, NOT_OWN, is_exempt, load_decisions, page_view, sectors_by_domain
from noroshi.judge import JUDGED_PURPOSES, UNREADABLE_SKIPS
from noroshi.discover import PAGE_TEXT_PATTERN, Link, extract_links, select_candidates
from noroshi.look import CANDIDATE_SKIPPED, NOT_JUDGED, REASONS, UNREADABLE_PAGE, is_measurement, look
from noroshi.optout import is_opted_out, url_matches
from noroshi.sectxt_parse import FORBIDDEN, classify_security_txt, is_forbidden, library_agrees, parse_security_txt
from noroshi.store import NOT_OUTCOMES, FetchRecord, Store, homepage_record, measured_homepage

_OK = ("valid", "valid_with_problems")
_FILE_CLASSES = ("valid", "valid_with_problems", "invalid")
WELLKNOWN_PATH = "/.well-known/security.txt"
ROOT_PATH = "/security.txt"
COLUMNS = ["sector", "domain", "n_units", "beds_sum", "status", "ua_kind", "host", "sectxt_class",
           "sectxt_location", "contact_types", "expires_status", "signed", "parsers_agree", "sectxt_text_plain",
           "lenient_contact", "n_malformed_contacts", "cmshpt_class", "policy_page", "policy_pages_unjudged",
           "policy_pages_missing_judgement", "safe_harbor", "platforms", "diodb", "cna", "listing", "redirected_offdomain", "main", "strict",
           "broad", "first_seen", "passes_disagree", "final_domain", "listing_matched_on", "policy_look", "probes_cut",
           "homepage_truncated", "policy_page_offdomain", "sectxt_url", "archive_redirect_target", "registry_listing",
           "requests_sent", "blocked_reason", "file_forbidden", "site_switched", "sectxt_offdomain",
           "cmshpt_offdomain", "identity", "identity_agreed", "mismatch_reason", "identity_page_sha256"]
FILE_PURPOSES = ("sectxt_wellknown", "sectxt_root", "cmshpt")


def _latest(recs_by_pass: dict[int, list[FetchRecord]], purpose: str, keep=None,
            usable=is_measurement) -> FetchRecord | None:
    """The record that counts for one file or page, over both passes (pass 2 after pass 1): the latest
    `usable` record (a measurement; for a file also the record standing for a forbidden file) that `keep` accepts
    (for a file: present and valid), else the latest usable record, else the latest record. A later failed or refused
    attempt never erases a result, and a valid file seen in either pass stands. Attempts that are not outcomes
    (cancelled, in flight, outage) are ignored."""
    done = [r for p in (1, 2) for r in recs_by_pass[p] if r.purpose == purpose and r.error not in NOT_OUTCOMES]
    found = [r for r in done if usable(r)]
    kept = [r for r in found if keep(r)] if keep else []
    return (kept or found or done or [None])[-1]


def _passes_disagree(recs_by_pass, purposes, classify, usable=is_measurement) -> bool:
    """True when, for one of these files, the latest result (`usable`) of pass 1 and of pass 2 classify differently."""
    for purpose in purposes:
        last = [[r for r in recs_by_pass[p] if r.purpose == purpose and usable(r)] for p in (1, 2)]
        if all(last) and classify(last[0][-1]) != classify(last[1][-1]):
            return True
    return False


def _forbidden_records(store: Store, recs_by_pass) -> set[int]:
    """The ids (`id()`) of the records that stand for a forbidden file: for each pass and file purpose where part X's
    shared rule `sectxt_parse.is_forbidden` holds on that pass's records, the last record of that purpose (the browser
    retry). Such a record is a file's result, like a measurement: the file is there and nobody outside can read it."""
    found = set()
    for p in (1, 2):
        for purpose in FILE_PURPOSES:
            if is_forbidden(recs_by_pass[p], purpose, lambda r: _blob(store, r)):
                found.add(id([r for r in recs_by_pass[p] if r.purpose == purpose and r.error not in NOT_OUTCOMES][-1]))
    return found


def _blob(store: Store, rec: FetchRecord | None) -> bytes | None:
    return store.get_blob(rec.body_sha256) if rec is not None and rec.body_sha256 else None


def _when(rec: FetchRecord, fallback: datetime) -> datetime:
    """A file is judged (for Expires) at the moment it was fetched, never at the time `derive` runs."""
    try:
        return datetime.strptime(rec.started_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return fallback


def _sectxt_class(store: Store, rec: FetchRecord | None, now: datetime, forbidden: bool = False) -> str:
    if rec is None:
        return "unmeasured"
    return classify_security_txt(rec.status, rec.final_url, rec.content_type, _blob(store, rec), rec.blocked,
                                 rec.error, _when(rec, now), forbidden=forbidden)


def _cms_class(store: Store, rec: FetchRecord | None, forbidden: bool = False) -> str:
    if rec is None:
        return "unmeasured"
    return classify_cms_hpt(rec.status, rec.final_url, rec.content_type, _blob(store, rec), rec.blocked, rec.error,
                            forbidden=forbidden)


def _choose_sectxt(classes: list[tuple[str, FetchRecord | None, str]]) -> tuple[FetchRecord | None, str, str]:
    """(record used, class, location) from (location, record, class) of the well-known and the root file, by this
    order:
    1. a location that is valid or valid_with_problems (well-known first; valid over valid_with_problems);
    2. otherwise unmeasured if either location is (a root never requested counts, but the crawler skips the
       root request on purpose when the well-known file parsed, which is case 1);
    3. otherwise invalid if either is (well-known first), with that file's details;
    4. otherwise html_error if either is; otherwise forbidden if either is (no file at either location, at least one
       that nobody outside can read); else absent."""
    ok = [c for c in classes if c[2] in _OK]
    if ok:
        where, rec, cls = min(ok, key=lambda c: c[2] != "valid")  # min is stable: well-known wins a tie
        return rec, cls, where
    if any(c[2] == "unmeasured" for c in classes):
        return None, "unmeasured", ""
    bad = [c for c in classes if c[2] == "invalid"]
    if bad:
        where, rec, cls = bad[0]
        return rec, cls, where
    for no_file in ("html_error", FORBIDDEN):
        if any(c[2] == no_file for c in classes):
            return None, no_file, ""
    return None, "absent", ""


def _usable_url(value: str) -> bool:
    """A Policy value is site-controlled and may be malformed; such a value is not a usable link."""
    try:
        parts = urlsplit(value)
        return parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        return False


def _platform_name(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".").split(".")[-2]


def _theirs(link, name: str, listed: set[str], tokens: set[str]) -> bool:
    if name in listed or PAGE_TEXT_PATTERN.search(link.text):
        return True
    segment = unquote(urlsplit(link.url).path.strip("/").split("/")[0]).lower()
    return any(t and t in segment for t in tokens)


def _platforms(links, domains: list[str], tokens: set[str], registry_hits: list[str]) -> list[str]:
    """Registry hits plus linked platform programmes that are plausibly this site's own (`domains`: the listed
    domain and the final site)."""
    found = set(registry_hits)
    keep = {*(domain_label(d).lower() for d in domains if d), *tokens}
    for link in links:
        if select_candidates([link])[1] and _theirs(link, _platform_name(link.url), set(registry_hits), keep):
            found.add(_platform_name(link.url))
    return sorted(found)


def _load_judgements(path: Path) -> dict[str, dict[tuple, dict]]:
    """domain -> (url, body_sha256) -> the judgement row of that page (the last one written)."""
    out: dict[str, dict[tuple, dict]] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or "domain" not in row:
            continue
        out.setdefault(row["domain"], {})[(row.get("url"), row.get("body_sha256"))] = row
    return out


def _is_policy(row: dict) -> bool:
    final = row.get("final")
    return bool(final and final.get("invites_reports") and final.get("contact_method"))


def _unread(row: dict) -> bool:
    """A judgement row of a page the judge could not read (pdf_unreadable, non_text or unreadable). A catch-all page,
    a page a followed meta refresh stands in for, an image and a page that is the homepage were read and are not
    policy pages."""
    return row.get("skipped") in UNREADABLE_SKIPS


def _judgement_reasons(rows: dict[tuple, dict], should: set[tuple]) -> set[str]:
    """unreadable_page: a stored page the judge could not read (`_unread`), unless another stored page of the same
    URL was read; not_judged: a stored page that should have been judged has no judgement row."""
    stored = {key: row for key, row in rows.items() if key in should}
    read = {_norm_url(url) for (url, _), row in stored.items() if not _unread(row)}
    reasons = set()
    if any(_unread(row) and _norm_url(url) not in read for (url, _), row in stored.items()):
        reasons.add(UNREADABLE_PAGE)
    if should - set(rows):
        reasons.add(NOT_JUDGED)
    return reasons


def _jsonl(path: Path):
    """The JSON objects of a resumable log; a line cut off by a killed run is skipped."""
    if not path or not Path(path).exists():
        return
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            yield row


def _load_first_seen(path: Path) -> dict[tuple[str, str, str], str]:
    """(domain, host, path) -> the earliest first-seen date found for that file."""
    out: dict[tuple[str, str, str], str] = {}
    for row in _jsonl(path):
        if row.get("first_seen"):
            key = (row.get("domain"), row.get("host"), row.get("path"))
            out[key] = min(row["first_seen"], out.get(key, row["first_seen"]))
    return out


def _load_redirects(path: Path | None) -> dict[str, str]:
    """domain -> where its host's last archived capture redirected (the last row written for it)."""
    return {row["domain"]: row.get("redirect_target") or "" for row in _jsonl(path) if "domain" in row}


def _file_key(url: str | None) -> tuple[str, str]:
    """(host, path) of a file's final URL, as the first-seen lookup uses them."""
    parts = urlsplit(url or "")
    return (parts.hostname or "").lower(), parts.path or "/"


_NO_HITS = {"platforms": [], "diodb": False, "cna": False}


def _registry_hits(registries, name: str, final_domain: str) -> tuple[dict, str]:
    """The union of the registry hits for the listed domain and for the final site, and which of the two
    matched: listed, final, both, or empty."""
    listed = registries.hits(name)
    final = registries.hits(final_domain) if final_domain and final_domain != registrable_domain(name) else _NO_HITS
    union = {"platforms": sorted(set(listed["platforms"]) | set(final["platforms"])),
             "diodb": listed["diodb"] or final["diodb"], "cna": listed["cna"] or final["cna"]}

    def any_hit(h):
        return bool(h["platforms"] or h["diodb"] or h["cna"])
    matched = {(True, True): "both", (True, False): "listed", (False, True): "final"}.get(
        (any_hit(listed), any_hit(final)), "")
    return union, matched


def _site(url: str | None) -> str:
    host = (urlsplit(url or "").hostname or "").lower().rstrip(".")
    return registrable_domain(host) if host else ""


def _browser_sites(store: Store, pass_no: int) -> set[str]:
    """Sites that saw the browser signature in this pass: a site that refused the identified signature (a refusal
    is a refused row with a status other than 429; its final URL's site refused) and was then reached by a sent
    request whose record used the browser signature, with that site among its hops. A record says only that some
    hop used the browser signature; a hop chooses it only for a site that has refused, so a hop at a refusing
    site after its first refusal is a browser hop."""
    sent = store.sent(pass_no)
    refused: dict[str, int] = {}
    for row_id, r in sent:
        if r.blocked and r.status is not None and r.status != 429:
            refused.setdefault(_site(r.final_url or r.url), row_id)
    saw = set()
    for row_id, r in sent:
        if r.ua_kind != "browser":
            continue
        for site in {_site(u) for u in (r.url, *r.redirect_chain, r.final_url) if u}:
            if site in refused and row_id > refused[site]:
                saw.add(site)
    return saw


def _own_sites(recs: list[FetchRecord], name: str, start_host: str) -> set[str]:
    """The domain's own sites in one pass: the listed domain's, its start URL host's, and each homepage's final host's."""
    own = {registrable_domain(name), registrable_domain(start_host)}
    return own | {_site(r.final_url) for r in recs if r.purpose == "homepage" and r.final_url}


def _ua_kind(recs_by_pass, name: str, start_host: str, browser_sites: dict[int, set[str]]) -> str:
    """browser when the domain's own site (the listed domain's, or a homepage's final host's) saw the browser
    signature in a pass; identified when the domain has records but its own site never did; empty without records."""
    if not any(recs_by_pass.values()):
        return ""
    for p, recs in recs_by_pass.items():
        if _own_sites(recs, name, start_host) & browser_sites[p]:
            return "browser"
    return "identified"


def _site_switched(recs_by_pass, name: str, start_host: str, switched: dict[int, set[str]]) -> bool | None:
    """True when the domain's own site was switched to the browser signature in a pass (`Store.browser_sites`: its
    browser retry got an answer, so the rest of its requests used that signature). A site that refused one path to
    both signatures saw the browser signature once and was not switched. None without records."""
    if not any(recs_by_pass.values()):
        return None
    return any(_own_sites(recs, name, start_host) & switched[p] for p, recs in recs_by_pass.items())


def _listed(hits: dict) -> bool:
    """A registry listing: a bug bounty platform's scope, disclose.io or a CNA entry (no crawl needed)."""
    return bool(hits["platforms"] or hits["diodb"] or hits["cna"])


def _blocked_reason(store: Store, name: str, recs_by_pass) -> str:
    """Why a blocked domain ended blocked, in the pass whose status counts: `refused` when its homepage was refused
    outright (a block status other than 429, or a challenge page); otherwise `rate_limited`: a 429 from its own site,
    a redirect into a site that had answered 429, or a site already rate-limited before anything was sent."""
    p = 2 if store.done_status(name, 2) else 1
    home = homepage_record(recs_by_pass[p])
    if home is not None and home.blocked and home.status is not None and home.status != 429:
        return "refused"
    return "rate_limited"


def _num(v):
    n = pd.to_numeric(v, errors="coerce")
    return None if pd.isna(n) else n


def _status(store: Store, name: str) -> str:
    """optout if any pass ended optout; measured if any pass measured it; else the latest pass's status."""
    ended = [store.done_status(name, p) for p in (1, 2)]
    if "optout" in ended:
        return "optout"
    if "measured" in ended:
        return "measured"
    return ended[1] or ended[0] or "not_crawled"


def derive_domains(store: Store, domains: pd.DataFrame, judgements: Path, registries, wayback: Path,
                   now: datetime, optout: frozenset[str] = frozenset(), redirects: Path | None = None,
                   identity: Path | None = None) -> pd.DataFrame:
    """One row per listed domain. `optout` is the set `cli.load_optout` reads from info/optout.txt: a domain in it
    (by its name, its homepage's final host, or either one's registrable domain) is `optout` with empty outcomes,
    whatever was collected before the opt-out arrived. `wayback` and `redirects` are the Internet Archive result
    files: first-seen dates matched on the domain and the host and path of its chosen file's final URL, and the
    archived redirect target reported for unreachable hospital domains. `identity` is the identity check's result
    file (`noroshi identity`): a domain that would be measured and whose homepage, as derive reads it, was decided
    `other` or `unclear` is `mismatch` with empty outcomes; one with no decision for that homepage yet has an empty
    `identity` and stays measured (the main analysis refuses to run while any is left)."""
    judged = _load_judgements(Path(judgements))
    decisions = load_decisions(identity)
    sectors_of = sectors_by_domain(domains)
    seen = _load_first_seen(Path(wayback))
    moved = _load_redirects(redirects)
    browser_sites = {p: _browser_sites(store, p) for p in (1, 2)}
    switched = {p: store.browser_sites(p) for p in (1, 2)}
    rows = []
    for _, src in domains.iterrows():
        name = normalize_domain(src["domain"])
        sector = src["sector"]
        status = "optout" if is_opted_out(store, name, optout) else _status(store, name)
        measured = status == "measured"
        tokens = set(str(src.get("name_tokens") or "").lower().split())
        hits, matched_on = _registry_hits(registries, name, "")
        start_host = (urlsplit(str(src.get("start_url") or "")).hostname or name).lower()
        row = dict.fromkeys(COLUMNS)
        row.update(sector=sector, domain=name, n_units=_num(src.get("n_units")), beds_sum=_num(src.get("beds_sum")),
                   status=status, ua_kind="", host=start_host, sectxt_class="unmeasured", sectxt_location="",
                   contact_types="", expires_status="", cmshpt_class="" if sector != "hospital" else "unmeasured",
                   policy_page="unmeasured", safe_harbor="", platforms="", diodb=hits["diodb"], cna=hits["cna"],
                   policy_pages_unjudged=0,
                   policy_pages_missing_judgement=0, first_seen="", final_domain="",
                   listing_matched_on=matched_on, policy_look="", sectxt_url="", registry_listing=_listed(hits),
                   blocked_reason="", identity="", mismatch_reason="", identity_page_sha256="",
                   archive_redirect_target=moved.get(name, "") if sector == "hospital" and status == "unreachable" else "")
        recs = {p: store.requests_for(name, p) for p in (1, 2)}
        row["requests_sent"] = sum(not r.reused for p in (1, 2) for r in recs[p])  # reused copies were not sent
        # a judgement row counts only when a stored record of this listed domain stands behind it (a purge stopped
        # between its store transaction and its file rewrite leaves rows with none)
        stored = {(r.url, r.body_sha256) for p in (1, 2) for r in recs[p]}
        judged_here = {key: j for key, j in judged.get(name, {}).items() if key in stored}
        row["policy_pages_unjudged"] = sum(1 for j in judged_here.values() if _unread(j))
        if status == "blocked":
            row["blocked_reason"] = _blocked_reason(store, name, recs)
        if status == "optout":  # empty outcomes: nothing collected about it is reported
            row.update(ua_kind="", diodb=None, cna=None, listing_matched_on="", policy_pages_unjudged=None,
                       registry_listing=None)
            rows.append(row)
            continue
        row["ua_kind"] = _ua_kind(recs, name, start_host, browser_sites)
        row["site_switched"] = _site_switched(recs, name, start_host, switched)
        if measured:
            # only a pass that measured the domain gives its homepage: one that ended mismatch, unreachable, blocked or
            # anything else did not land on the organization's site. The homepage of that pass is the one that
            # answered (store.homepage_record, as for look, needs_retry, is_forbidden and the identity check)
            measured_passes = [p for p in (1, 2) if store.done_status(name, p) == "measured"]
            home = measured_homepage(recs, measured_passes)
            if is_exempt(sectors_of.get(name, [sector])):
                row["identity"] = EXEMPT  # the .gov registry controls these names
            elif home is not None and (found := decisions.get((name, page_view(store, home).key))):
                row.update(identity=found["site"], identity_agreed=bool(found["agreed"]),
                           identity_page_sha256=found["page_sha256"])
            if row["identity"] in NOT_OWN:  # not the organization's working site: could not measure
                row.update(status="mismatch", mismatch_reason=f"identity_{row['identity']}")
                measured = False
        elif status == "mismatch":
            row["mismatch_reason"] = "crawl_rule"
        if not measured:
            rows.append(row)
            continue

        if home is not None and home.final_url:
            row["host"] = (urlsplit(home.final_url).hostname or start_host).lower()
            row["redirected_offdomain"] = registrable_domain(row["host"]) != registrable_domain(name)
        row["final_domain"] = registrable_domain(row["host"])
        hits, row["listing_matched_on"] = _registry_hits(registries, name, row["final_domain"])
        row.update(diodb=hits["diodb"], cna=hits["cna"], registry_listing=_listed(hits))

        forbidden = _forbidden_records(store, recs)

        def is_result(r):  # a file's result: a measurement, or the record standing for a forbidden file
            return is_measurement(r) or id(r) in forbidden

        def sectxt_cls(r):
            return _sectxt_class(store, r, now, forbidden=r is not None and id(r) in forbidden)

        def cms_cls(r):
            return _cms_class(store, r, forbidden=r is not None and id(r) in forbidden)

        located = [(where, rec, sectxt_cls(rec)) for where, rec in
                   (("well-known", _latest(recs, "sectxt_wellknown", lambda r: sectxt_cls(r) in _OK, is_result)),
                    ("root", _latest(recs, "sectxt_root", lambda r: sectxt_cls(r) in _OK, is_result)))]
        chosen, cls, where = _choose_sectxt(located)
        file_classes = [c for _, _, c in located]
        disagree = _passes_disagree(recs, ("sectxt_wellknown", "sectxt_root"), sectxt_cls, is_result)
        if sector == "hospital":
            disagree = disagree or _passes_disagree(recs, ("cmshpt",), cms_cls, is_result)
        row["passes_disagree"] = disagree
        row.update(sectxt_class=cls, sectxt_location=where)
        if chosen is not None and cls in _FILE_CLASSES:
            row["sectxt_url"] = chosen.final_url or chosen.url
            row["first_seen"] = seen.get((name, *_file_key(row["sectxt_url"])), "")
            row["sectxt_offdomain"] = _site(row["sectxt_url"]) != row["final_domain"]  # read on another site
        policy_urls: list[str] = []
        if cls in _FILE_CLASSES:
            body = _blob(store, chosen) or b""
            parsed = parse_security_txt(body, _when(chosen, now))
            row.update(contact_types=";".join(sorted(parsed.contact_types)), expires_status=parsed.expires_status,
                       signed=parsed.signed, parsers_agree=library_agrees(body, parsed),
                       sectxt_text_plain=(chosen.content_type or "").strip().lower().startswith("text/plain"),
                       lenient_contact=parsed.lenient_contact, n_malformed_contacts=len(parsed.malformed_contacts))
            policy_urls = parsed.fields.get("policy", [])
        if sector == "hospital":
            cms = _latest(recs, "cmshpt", lambda r: cms_cls(r) == "present_valid", is_result)
            row["cmshpt_class"] = cms_cls(cms)
            file_classes.append(row["cmshpt_class"])
            if row["cmshpt_class"] in ("present_valid", "present_invalid"):
                row["cmshpt_offdomain"] = _site(cms.final_url or cms.url) != row["final_domain"]
        row["file_forbidden"] = FORBIDDEN in file_classes  # a file the domain asked for was refused to both signatures

        links = []
        if home is not None and home.status == 200 and home.body_sha256:
            links = extract_links(_blob(store, home) or b"", home.final_url or home.url)
        links += [Link(u, "") for u in policy_urls if _usable_url(u)]
        plats = _platforms(links, [name, row["final_domain"]], tokens, hits["platforms"])
        row["platforms"] = ";".join(plats)

        # a page whose final URL is on a site that opted out after it was collected counts as never requested
        withheld = {(r.url, r.body_sha256) for p in (1, 2) for r in recs[p]
                    if r.purpose in JUDGED_PURPOSES and url_matches(r.final_url or r.url, optout)}
        pages = {key: j for key, j in judged_here.items() if key not in withheld}
        row["policy_pages_unjudged"] = sum(1 for j in pages.values() if _unread(j))
        should = {(r.url, r.body_sha256) for p in (1, 2) for r in recs[p] if r.purpose in JUDGED_PURPOSES
                  and r.status == 200 and is_measurement(r) and r.body_sha256} - withheld
        row["policy_pages_missing_judgement"] = len(should - set(pages))
        found = [j for j in pages.values() if _is_policy(j)]
        the_look = look(store, recs, measured_passes)
        row.update(probes_cut=the_look.probes_cut, homepage_truncated=the_look.homepage_truncated)
        reasons = set(the_look.reasons) | _judgement_reasons(pages, should)
        if withheld:
            reasons.add(CANDIDATE_SKIPPED)
        if found:
            row["policy_page"] = "yes"
            row["safe_harbor"] = "yes" if any(j["final"].get("safe_harbor") for j in found) else "no"
            stored_at = {(r.url, r.body_sha256): r.final_url or r.url for p in (1, 2) for r in recs[p]}
            row["policy_page_offdomain"] = all(_site(stored_at.get((j.get("url"), j.get("body_sha256")), j.get("url")))
                                               != row["final_domain"] for j in found)
        elif reasons:
            row["policy_page"] = "unmeasured"  # the look was incomplete: never reported as no policy page
            row["policy_look"] = ";".join(r for r in REASONS if r in reasons)
        else:
            row["policy_page"] = "no"
        row["listing"] = bool(plats or hits["diodb"] or hits["cna"])
        if cls != "unmeasured":
            row["main"] = cls in _OK
            row["strict"] = cls == "valid"
        policy = row["policy_page"] == "yes"
        if row["main"] or policy or row["listing"]:
            row["broad"] = True
        elif row["main"] is not None and row["policy_page"] != "unmeasured":
            row["broad"] = False
        rows.append(row)
    out = pd.DataFrame(rows, columns=COLUMNS, dtype=object)  # counts stay whole numbers next to empty cells
    for c in COLUMNS:
        out[c] = out[c].where(out[c].notna(), None)
    return out


def derive_hospitals(hospitals: pd.DataFrame, derived: pd.DataFrame) -> pd.DataFrame:
    """Kept hospitals, each with the outcome columns of its domain."""
    exclusion = hospitals["exclusion"].fillna("").astype(str).str.strip()
    kept = hospitals[exclusion == ""].copy()
    dom = derived[derived.sector == "hospital"].set_index("domain")
    cols = ["n_units", "status", "main", "strict", "policy_page", "listing", "broad", "registry_listing"]
    for c in cols:
        kept[c] = kept["domain"].map(dom[c])
    keep = ["hifld_id", "name", "state", "type", "owner", "beds", "rural", "domain", *cols]
    out = kept[keep].reset_index(drop=True)
    for c in cols:
        out[c] = out[c].astype(object).where(out[c].notna(), None)
    return out


def wayback_inputs(derived: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The two CSVs `noroshi wayback` reads: files to date (every main-outcome domain, at the host and path of its
    chosen file's final URL, once even when the domain is in several sector tables) and hosts to check for a
    redirect (hospital domains that were unreachable)."""
    hit = derived[derived["main"].map(lambda v: v is True)]
    keys = [_file_key(u) for u in hit["sectxt_url"]]
    first = pd.DataFrame({"domain": list(hit["domain"]), "host": [k[0] for k in keys], "path": [k[1] for k in keys]},
                         columns=["domain", "host", "path"]).drop_duplicates().reset_index(drop=True)
    down = derived[(derived.sector == "hospital") & (derived.status == "unreachable")]
    return first, down[["domain", "host"]].reset_index(drop=True)
