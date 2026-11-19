"""Late opt-outs: whether a listed domain is opted out, and deleting what was collected for it.

The opt-out set is what `cli.load_optout` reads from info/optout.txt (each line's host and registrable domain). A
listed domain is opted out when the set holds its name, the host its homepage was requested at or landed on (in
any pass), or the registrable domain of any of these, or when a pass of it ended optout.
"""
import json
import os
import re
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from noroshi.domains import registrable_domain
from noroshi.store import Store, purged_key

_CUT_JUDGE_URL = re.compile(r'"prompt": "URL: ([^\s"\\]+)')  # a call written before `meta` existed
_CUT_PURPOSE = re.compile(r'^\{"purpose": "([^"\\]+)"')
_CUT_META = re.compile(r'"meta": \{(.*?)(?:\}, "system"|$)', re.DOTALL)  # the meta object, as far as it was written
_CUT_KEY = '"{}": "([^\\s"\\\\]+)"'  # one string field of it


def matches(host: str | None, optout: frozenset[str]) -> bool:
    host = (host or "").strip().lower().rstrip(".")
    return bool(host) and (host in optout or registrable_domain(host) in optout)


def url_matches(url: str | None, optout: frozenset[str]) -> bool:
    """True when the URL's host, or its registrable domain, is in the opt-out set."""
    if not optout:
        return False
    try:
        host = urlsplit(url or "").hostname
    except ValueError:
        return False
    return matches(host, optout)


def domain_hosts(store: Store, domain: str) -> set[str]:
    """The listed domain and every host its homepage was requested at or landed on, in either pass."""
    hosts = {domain}
    for p in (1, 2):
        for r in store.requests_for(domain, p):
            if r.purpose == "homepage":
                hosts |= {urlsplit(u).hostname or "" for u in (r.url, r.final_url) if u}
    return hosts - {""}


def is_opted_out(store: Store, domain: str, optout: frozenset[str]) -> bool:
    """Also true when a pass ended optout: the crawler found it opted out, or a purge of the site its homepage landed
    on deleted that homepage's record."""
    if "optout" in (store.done_status(domain, 1), store.done_status(domain, 2)):
        return True
    return bool(optout) and any(matches(h, optout) for h in domain_hosts(store, domain))


def pending_purges(db: Path) -> list[str]:
    """The domains whose purge was started on the store at `db` and not finished (`purge_pending`): until it is run
    again, the store and the files hold a half-done purge, so no other command may read or add to them. The store is
    opened read-only; a missing store, or one from before the table existed, has none."""
    import sqlite3

    if not Path(db).exists():
        return []
    con = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return [d for (d,) in con.execute("SELECT domain FROM purge_pending ORDER BY id")]
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def unpurged(db: Path, optout: frozenset[str]) -> list[str]:
    """The opted-out sites (registrable domains of the opt-out list) of which the store at `db` still holds records:
    a listed domain of that name, or any record requested at, redirected through or ending on a host of the site. The
    store is opened read-only; a missing store holds nothing."""
    import sqlite3

    if not optout or not Path(db).exists():
        return []
    con = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True)  # ?, # and % in the path are escaped
    try:
        left = []
        for site in sorted({registrable_domain(name) for name in optout}):
            rows = con.execute("SELECT domain, url, final_url, redirect_chain FROM requests WHERE domain = ? OR "
                               "instr(lower(url), ?) OR instr(lower(coalesce(final_url, '')), ?) OR "
                               "instr(lower(coalesce(redirect_chain, '')), ?)", (site, site, site, site)).fetchall()
            if any(matches(d, frozenset({site})) or site in {_site(u) for u in (u, f, *json.loads(c or "[]")) if u}
                   for d, u, f, c in rows):
                left.append(site)
        return left
    finally:
        con.close()


def _rewrite(path: Path, drop, change=None) -> int:
    """Rewrite a JSON-lines log without the lines `drop(line, row)` accepts (`row` is the parsed object, or None for
    a line that is not JSON), atomically: a temporary file beside it, then a replace. A kept row that `change(row)`
    returns a new object for is written as that object; every other line is kept byte for byte and in order. Returns
    how many lines were dropped."""
    if path is None or not path.exists():
        return 0
    kept, dropped, changed = [], 0, 0
    for line in path.read_bytes().splitlines(keepends=True):
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            row = None
        row = row if isinstance(row, dict) else None
        if drop(line, row):
            dropped += 1
        elif row is not None and change is not None and (new := change(row)) is not None:
            kept.append((json.dumps(new, ensure_ascii=False) + "\n").encode("utf-8"))
            changed += 1
        else:
            kept.append(line)
    if dropped or changed:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(b"".join(kept))
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return dropped


def _archive_rows(domain: str, site: str):
    """The drop rule for an Internet Archive result file: the purged domain's rows, and any row looked up at a host on
    its site (another listed domain's file that was found there). A row naming the site only as another domain's
    redirect target stays: it is a fact about that domain."""
    return lambda line, row: row is not None and (row.get("domain") == domain
                                                  or _site(f"https://{row.get('host') or ''}/") == site)


def _call_of(line: bytes, row: dict | None) -> tuple[str | None, list[str], str | None]:
    """(purpose, page URLs, listed domain) of a model-call log row: the URLs and the domain from its `meta` (post-pilot
    calls), or, for a judge call written before `meta` existed, the URL from its prompt's first line "URL: <page>". A
    line cut off by a killed run is read with patterns (`meta` is written before the prompt), so a cut call is not
    missed."""
    if row is not None:
        meta = row.get("meta")
        if isinstance(meta, dict):  # the requested URL, and the final URL a redirect led to
            urls = [u for u in (meta.get("url"), meta.get("final_url")) if isinstance(u, str) and u]
            return row.get("purpose"), urls, meta.get("domain")
        prompt = row.get("prompt")
        if isinstance(prompt, str) and prompt.startswith("URL: "):
            return row.get("purpose"), [prompt[len("URL: "):].split("\n", 1)[0].strip()], None
        return row.get("purpose"), [], None
    text = line.decode("utf-8", errors="replace")

    def first(pattern, within: str) -> str | None:
        found = re.search(pattern, within)
        return found.group(1) if found else None
    meta = first(_CUT_META, text) or ""
    urls = [u for u in (first(_CUT_KEY.format("url"), meta), first(_CUT_KEY.format("final_url"), meta),
                        first(_CUT_JUDGE_URL, text)) if u]
    return first(_CUT_PURPOSE, text), urls, first(_CUT_KEY.format("domain"), meta)


def _site(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return registrable_domain(host) if host else ""


def _names(text: str | None, site: str) -> bool:
    """True when the text names the site or a host under it (www.gone.org names gone.org; boss.org does not)."""
    return bool(text) and re.search(rf"(?<![a-z0-9-]){re.escape(site)}(?![a-z0-9-])", text.lower()) is not None


def site_name(name: str) -> str:
    """The registrable domain of an opted-out entry however it is written: www. or not, any case, a trailing dot."""
    return registrable_domain(name.strip().lower().rstrip("."))


def _step(label: str) -> None:
    """A point between two steps of a purge (tests stop a purge here to show that running it again finishes it)."""


def _counted(path: Path | None, drop, change=None) -> int:
    """How many lines `_rewrite(path, drop, change)` would drop, without writing anything."""
    if path is None or not path.exists():
        return 0
    n = 0
    for line in path.read_bytes().splitlines(keepends=True):
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            row = None
        n += bool(drop(line, row if isinstance(row, dict) else None))
    return n


def _rules(plan: dict):
    """The drop and change rules of the purge's files, from its plan: (judgement rows, identity rows, Internet Archive
    rows, model-call log rows, and the change that takes the purged domain's name out of a model-call row that stays)."""
    from noroshi.identity import IDENTITY_PURPOSE  # identity and judge import this module
    from noroshi.judge import JUDGE_PURPOSE

    domain, site = plan["domain"], plan["site"]
    pages, read_it = {tuple(k) for k in plan["pages"]}, set(plan["read_it"])

    def its_judgement(line: bytes, row: dict | None) -> bool:
        return row is not None and (row.get("domain") == domain
                                    or (row.get("domain"), row.get("url"), row.get("body_sha256")) in pages)

    def its_decision(line: bytes, row: dict | None) -> bool:
        return row is not None and row.get("domain") in read_it

    def its_call(line: bytes, row: dict | None) -> bool:
        purpose, urls, listed = _call_of(line, row)
        if purpose == JUDGE_PURPOSE:  # a page on the purged domain's own site, whoever asked about it
            return any(_site(url) == site for url in urls)
        return purpose == IDENTITY_PURPOSE and listed in read_it

    def unnamed(row: dict) -> dict | None:
        """A call that stays (its page is on another site) keeps no mention of the purged listed domain."""
        meta = row.get("meta")
        if isinstance(meta, dict) and meta.get("domain") == domain:
            return {**row, "meta": {**meta, "domain": ""}}
        return None
    return its_judgement, its_decision, _archive_rows(domain, site), its_call, unnamed


def _plan(store: Store, domain: str, judgements: Path, archive_files: list[Path], model_log: Path,
          identity: Path | None) -> dict:
    """Everything one purge does, read from the records and the files before anything changes: the records to delete,
    their traces and counts, the markers, the blobs, the rows of each file and every count of the purge-log line. The
    plan is kept in the store with the deletions, so a stopped purge can be finished from it."""
    site = registrable_domain(domain)
    near = store.db.execute(  # a superset: the site's name appears in the record; the exact test follows
        "SELECT id, domain, url, final_url, body_sha256, reused, purpose, pass_no, status, site, redirect_chain, "
        "error_url, error_detail FROM requests WHERE domain = ? OR instr(lower(url), ?) "
        "OR instr(lower(coalesce(final_url, '')), ?) OR instr(lower(coalesce(redirect_chain, '')), ?) "
        "OR instr(lower(coalesce(error_url, '')), ?) OR instr(lower(coalesce(error_detail, '')), ?)",
        (domain, *[site] * 5)).fetchall()

    def names_site(r) -> bool:
        hops = [r[2], r[3], *json.loads(r[10] or "[]"), r[11]]
        return r[1] == domain or site in {_site(u) for u in hops if u} or _names(r[12], site)
    gone = [r for r in near if names_site(r)]
    # another listed domain whose homepage was requested at, redirected through or landed on the site is opted out
    # through it; with that record gone, its pass is marked optout so derive and judge still treat it as opted out
    landed = sorted({(r[1], r[7]) for r in gone if r[1] != domain and r[6] == "homepage"})
    # what a resumed pass reads of the removed records, kept as counts with no address of the site
    sent_for = Counter((r[1], r[7]) for r in gone if r[1] != domain and not r[5])
    sent_to = Counter((r[9], r[7]) for r in gone if not r[5] and r[6] != "homepage" and r[9] and r[9] != site)
    limited = {(s, r[7]) for r in gone if r[8] == 429 and (s := _site(r[3] or r[2])) and s != site}

    def still_used(column: str, values: tuple, n_gone: int) -> bool:
        where = " AND ".join(f"{c} = ?" for c in column.split(","))
        return store.db.execute(f"SELECT COUNT(*) FROM requests WHERE {where}", values).fetchone()[0] > n_gone
    shas = Counter(r[4] for r in gone if r[4])
    blobs = sorted(sha for sha, n in shas.items()
                   if not still_used("body_sha256", (sha,), n) and (store.blob_dir / sha).exists())
    keys = Counter((r[1], r[2], r[4]) for r in gone if r[4])  # removed pages, keyed as judgements are
    pages = sorted(k for k, n in keys.items() if not still_used("domain,url,body_sha256", k, n))
    plan = {"domain": domain, "site": site, "pages": pages,
            "read_it": sorted({domain} | {other for other, _ in landed}), "blobs": blobs,
            "stamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ids": [r[0] for r in gone], "landed": landed,
            "traces": [(r[1], r[7], r[6], purged_key(r[2])) for r in gone if r[1] != domain],
            "counts_kept": [(p, "domain", d, n) for (d, p), n in sorted(sent_for.items())]
            + [(p, "site", t, n) for (t, p), n in sorted(sent_to.items())]
            + [(p, "rate_limited", t, 1) for t, p in sorted(limited)]}
    its_judgement, its_decision, its_archive_row, its_call, _ = _rules(plan)
    plan["counts"] = {
        "records": len(gone), "requests sent": sum(1 for r in gone if not r[5]),
        "done markers": store.db.execute("SELECT COUNT(*) FROM done WHERE domain = ?", (domain,)).fetchone()[0],
        "browser-site rows": store.db.execute("SELECT COUNT(*) FROM browser_sites WHERE site = ?",
                                              (site,)).fetchone()[0],
        "blobs": len(blobs), "judgement rows": _counted(judgements, its_judgement),
        "identity rows": _counted(identity, its_decision),
        "internet archive rows": sum(_counted(f, its_archive_row) for f in archive_files),
        "model-log rows": _counted(model_log, its_call)}
    return plan


def _commit(store: Store, plan: dict) -> int:
    """One transaction: the deletions, their traces, the counts a resumed pass reads, the markers, the site's browser
    rows and the plan as a pending purge; deleted content is overwritten. Returns the pending entry's id."""
    domain, site = plan["domain"], plan["site"]
    store.db.execute("PRAGMA secure_delete = ON")  # deleted rows are overwritten, not left in free pages
    with store.db:
        store.db.executemany("DELETE FROM requests WHERE id = ?", [(i,) for i in plan["ids"]])
        store.db.executemany("INSERT INTO purged (domain, pass_no, purpose, url_sha256) VALUES (?, ?, ?, ?)",
                             plan["traces"])
        store.db.execute("DELETE FROM purged WHERE domain = ?", (domain,))  # earlier traces under the purged name
        store.db.execute("DELETE FROM purged_counts WHERE (kind = 'domain' AND name = ?) OR (kind != 'domain' AND "
                         "name = ?)", (domain, site))
        store.db.executemany("INSERT INTO purged_counts (pass_no, kind, name, n) VALUES (?, ?, ?, ?)",
                             plan["counts_kept"])
        store.db.execute("DELETE FROM done WHERE domain = ?", (domain,))
        store.db.executemany("INSERT INTO done (domain, pass_no, status) VALUES (?, ?, 'optout') "
                             "ON CONFLICT(domain, pass_no) DO UPDATE SET status = excluded.status", plan["landed"])
        store.db.execute("DELETE FROM browser_sites WHERE site = ?", (site,))
        pending = store.db.execute("INSERT INTO purge_pending (domain, plan) VALUES (?, ?)",
                                   (domain, json.dumps(plan))).lastrowid
    store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return pending


def _finish(store: Store, pending: int, plan: dict, judgements: Path, archive_files: list[Path], model_log: Path,
            identity: Path | None, purge_log: Path | None, store_name: str) -> dict[str, int]:
    """The steps after the store's transaction, each safe to repeat: the blobs, the files, the purge-log line (written
    once: a line with the plan's time stamp is not written again), then the pending entry goes."""
    for sha in plan["blobs"]:
        _step("blob")
        if store.db.execute("SELECT 1 FROM requests WHERE body_sha256 = ? LIMIT 1", (sha,)).fetchone() is None:
            (store.blob_dir / sha).unlink(missing_ok=True)  # a record written since the plan keeps its blob
    its_judgement, its_decision, its_archive_row, its_call, unnamed = _rules(plan)
    _step("judgements")
    _rewrite(judgements, its_judgement)
    _step("identity")
    _rewrite(identity, its_decision)
    for f in archive_files:
        _step("archive")
        _rewrite(f, its_archive_row)
    _step("model log")
    _rewrite(model_log, its_call, unnamed)
    _step("log line")
    counts = plan["counts"]
    if purge_log is not None:
        head = f"{plan['stamp']} purged {plan['domain']} from {store_name}: "
        if not purge_log.exists() or not any(line.startswith(head) for line in
                                             purge_log.read_text(encoding="utf-8").splitlines()):
            with purge_log.open("a", encoding="utf-8") as fh:
                fh.write(head + ", ".join(f"{n} {what}" for what, n in counts.items()) + "\n")
    with store.db:
        store.db.execute("DELETE FROM purge_pending WHERE id = ?", (pending,))
    store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return counts


def purge(store: Store, domain: str, judgements: Path, archive_files: list[Path], model_log: Path,
          identity: Path | None = None, purge_log: Path | None = None, store_name: str = "") -> dict[str, int]:
    """Delete everything collected for one opted-out domain: the request records of that listed domain and, under any
    other listed domain, every record that names its site (its requested URL, a redirect hop, its final URL or the
    address it failed at is on the site, or its error text names it); a trace of each record removed from another
    listed domain (`purged`, its URL hashed), so its look counts the page as skipped; what a resumed pass needs of the
    removed records, as counts with no address of the site (`purged_counts`: the attempts sent for each other listed
    domain and to each other site, and each other site that had answered 429), so the caps and the 429 stop hold; the
    domain's done markers, with an optout marker for another listed domain whose homepage record was removed; the
    site's `browser_sites` rows; all of it in one transaction, with deleted content overwritten. Then every blob no
    other record references, the judgement rows of those pages and of that listed domain, its Internet Archive rows
    and those looked up on its site, the identity decisions (`identity`) of that listed domain and of every other
    listed domain whose homepage record the purge removed (they read the opted-out site's homepage), and the model-call
    log rows of every judge or tie-break call about a page on its own site (requested there or ending there, whichever
    listed domain asked) and of every identity call for those listed domains; a judge call that stays (its page is on
    another site) keeps no mention of the purged domain (`meta.domain` is emptied). Resolution-pass rows name a company and a website, not page content, and stay. Last,
    one line in `purge_log` with the counts.

    Everything is planned before anything changes, and the plan is kept in the store (`purge_pending`) with the
    deletions; the steps after them are safe to repeat. So a purge stopped anywhere is finished by running a purge
    again: every pending purge is finished first, and a purge of the same domain then returns its counts. Returns what
    was removed, and how many of the removed records were requests sent, in a fixed order."""
    domain = site_name(domain)  # whichever spelling of the site it is given: its registrable domain (every listed
    # domain is one)
    files = (judgements, list(archive_files), model_log, identity, purge_log, store_name)
    store.db.execute("PRAGMA secure_delete = ON")  # also on a connection that only finishes a stopped purge
    finished = None
    for pending, pending_domain, plan in store.db.execute(
            "SELECT id, domain, plan FROM purge_pending ORDER BY id").fetchall():
        counts = _finish(store, pending, json.loads(plan), *files)
        if pending_domain == domain:
            finished = counts
    if finished is not None:
        return finished
    plan = _plan(store, domain, judgements, list(archive_files), model_log, identity)
    _step("planned")
    pending = _commit(store, plan)
    _step("committed")
    return _finish(store, pending, plan, *files)
