"""Command-line entry point."""
import argparse
import fcntl
import os
import re
import sys
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from noroshi import config

SECTOR_TABLES = {"hospital": "hospital_domains.csv", "federal": "federal_domains.csv",
                 "device": "device_domains.csv", "icsma": "icsma_domains.csv", "ehr": "ehr_domains.csv"}
SECTOR_ORDER = ("hospital", "federal", "device", "icsma", "ehr")  # `--sector all` runs them in this order
PROGRESS_EVERY = 50


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="noroshi",
        description="Measure which healthcare websites publish a way to report security vulnerabilities.",
    )
    sub = parser.add_subparsers(dest="command")
    frames = sub.add_parser("frames", help="build the study frames (never once protocol/FROZEN.txt exists)")
    fsub = frames.add_subparsers(dest="frame", required=True)
    force = argparse.ArgumentParser(add_help=False)
    force.add_argument("--force", action="store_true", help="overwrite outputs that already exist")
    fsub.add_parser("hospitals", parents=[force], help="download HIFLD hospitals and build the hospital frame")
    fsub.add_parser("audit", parents=[force], help="list kept hospital rows that may sit on a third-party site")
    fsub.add_parser("federal", parents=[force], help="download the .gov list and build the federal frame")
    fsub.add_parser("companies", parents=[force], help="download company sources and build the three company tables")
    res = fsub.add_parser("resolve", parents=[force], help="resolve company websites with Claude models")
    res.add_argument("sector", choices=sorted(COMPANY_SECTORS))
    res.add_argument("--workers", type=int, default=6)
    crawl = sub.add_parser("crawl", help="request the public files and pages for one sector, or all of them")
    crawl.add_argument("--sector", required=True, choices=[*sorted(SECTOR_TABLES), "all"],
                       help="one sector, or all (in the order hospital, federal, device, icsma, ehr)")
    crawl.add_argument("--pass", dest="pass_no", type=int, required=True, choices=(1, 2))
    crawl.add_argument("--limit", type=int, help="trial run on a random sample of this many domains per sector")
    crawl.add_argument("--seed", type=int, help="seed for the --limit sample (required with --limit)")
    crawl.add_argument("--exclude", metavar="FILE",
                       help="with --limit only: a file of listed domains (one per line) left out of every sector table "
                            "before the sample is drawn, for example an earlier pilot's")
    jd = sub.add_parser("judge", help="judge stored pages: which are vulnerability disclosure policy pages")
    jd.add_argument("--store", choices=("main", "pilot"), default="main",
                    help="main (default) or the pilot store; a pilot writes pilot_judgements.jsonl")
    idn = sub.add_parser("identity", help="check with two models that each measured homepage is the listed "
                                          "organization's working website")
    idn.add_argument("--store", choices=("main", "pilot"), default="main",
                     help="main (default) or the pilot store; a pilot writes pilot_identity.jsonl")
    idn.add_argument("--workers", type=_workers, default=4,
                     help="domains decided at once, 1 to 8 (default 4)")
    regs = sub.add_parser("registries", help="public registries of disclosure programs")
    rsub = regs.add_subparsers(dest="registry_action", required=True)
    rsub.add_parser("fetch", help="download the three registries from GitHub, pinned by commit and hash")
    wb = sub.add_parser("wayback", help="Internet Archive lookups")
    wb.add_argument("--store", choices=("main", "pilot"), default="main",
                    help="main (default) or the pilot run; a pilot writes pilot_wayback.jsonl and "
                         "pilot_wayback_redirects.jsonl")
    wb.add_argument("--first-seen", metavar="CSV", help="CSV with columns domain,host,path: when each file first appeared")
    wb.add_argument("--redirects", metavar="CSV", help="CSV with columns domain,host: where each host's last capture redirected")
    pg = sub.add_parser("purge", help="delete everything collected for one opted-out domain, and log the purge")
    pg.add_argument("domain", help="the listed domain or site; it must already be in info/optout.txt")
    pg.add_argument("--store", choices=("main", "pilot"), default="main",
                    help="main (default) or the pilot store and its files")
    for name, text in (("derive", "build the per-domain outcome table and the Internet Archive input files"),
                       ("analyze", "build the paper's tables, figures and numbers from the derived tables")):
        sp = sub.add_parser(name, help=text)
        sp.add_argument("--store", choices=("main", "pilot"), default="main",
                        help="main (default) or the pilot run; a pilot writes under a pilot/ folder")
        if name == "analyze":
            sp.add_argument("--allow-incomplete", action="store_true",
                            help="analyze the main run although some domains are not_crawled (they leave every share) "
                                 "or some measured domains have no identity decision (they count as the "
                                 "organizations' own sites); both numbers are printed")
    return parser


def _workers(value: str) -> int:
    from noroshi.identity import MAX_WORKERS

    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number") from None
    if not 1 <= n <= MAX_WORKERS:
        raise argparse.ArgumentTypeError(f"--workers must be 1 to {MAX_WORKERS}")
    return n


class OptoutError(ValueError):
    """A line of info/optout.txt that cannot be read as a website."""


def load_optout(path: Path) -> frozenset[str]:
    """Every non-blank line not starting with # is read like a website (a URL, a host or a bare domain); both its
    host and its registrable domain are kept. A line that cannot be read raises OptoutError. A pilot's exclusion file
    (`noroshi crawl --exclude`) is read the same way."""
    from noroshi.domains import normalize_website, registrable_domain

    if not path.exists():
        return frozenset()
    found: set[str] = set()
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        host = normalize_website(line)
        if host is None:
            raise OptoutError(f"{path} line {n} cannot be read as a website: {line!r}")
        found |= {host, registrable_domain(host)}
    return frozenset(found)


@contextmanager
def _run_lock(store_path: Path, nothing_done: str = "nothing was requested", what: str = "noroshi crawl"):
    """Hold a lock file next to the store for the whole run of a command that reads or changes what the store holds
    (`what`: crawl, judge, identity or purge); while one holds it, every other one on the same store stops, with a
    message naming the command that holds it."""
    lock_path = store_path.with_name(store_path.name + ".lock")
    handle = open(lock_path, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        found = re.fullmatch(r"pid (\d+) (.+)", handle.read().strip())
        handle.close()
        holder = f"`{found.group(2)}` (pid {found.group(1)})" if found else "another noroshi command"
        sys.exit(f"noroshi: {store_path} is in use by {holder}, which holds {lock_path}; {nothing_done}")
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid {os.getpid()} {what}\n")
        handle.flush()
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


class _Progress:
    """Every PROGRESS_EVERY domains: one line with the count so far, each status and the elapsed time, printed
    and appended to the run log."""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.start = time.monotonic()
        self.done = 0
        self.counts: Counter[str] = Counter()

    def __call__(self, status: str) -> None:
        self.done += 1
        if status:
            self.counts[status] += 1
        if self.done % PROGRESS_EVERY == 0:
            self.write()

    def note(self, message: str) -> None:
        """One timestamped line, printed and appended to the run log."""
        line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {message}"
        print(line, flush=True)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def write(self, prefix: str = "") -> None:
        secs = int(time.monotonic() - self.start)
        statuses = ", ".join(f"{k} {v}" for k, v in sorted(self.counts.items())) or "none yet"
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = (f"{stamp} {prefix}{self.done} domains done ({statuses}); "
                f"elapsed {secs // 3600}:{secs // 60 % 60:02d}:{secs % 60:02d}")
        print(line, flush=True)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _crawl(sector: str, pass_no: int, limit: int | None, seed: int | None, exclude_file: str | None = None) -> None:
    import asyncio

    import pandas as pd

    from noroshi.crawl import (LocalOutage, NotFrozen, OutageWatch, crawl_sector, frozen_problem, is_pilot,
                               live_control, live_url)
    from noroshi.fetch import NotLive, PoliteFetcher
    from noroshi.store import Store

    if limit is not None and seed is None:
        sys.exit("noroshi: --limit needs --seed, so the same sample can be drawn again")
    if exclude_file is not None and limit is None:
        sys.exit("noroshi: --exclude is for a pilot sample and needs --limit and --seed; a full pass crawls every "
                 "listed domain")
    exclude: frozenset[str] = frozenset()
    if exclude_file is not None:
        if not Path(exclude_file).exists():
            sys.exit(f"noroshi: the exclusion file {exclude_file} does not exist")
        try:
            exclude = load_optout(Path(exclude_file))
        except OptoutError as exc:
            sys.exit(f"noroshi: {exc}; nothing was requested")
    p = config.paths()
    sectors = SECTOR_ORDER if sector == "all" else (sector,)
    tables = {}
    for name in sectors:
        table = p.frames / SECTOR_TABLES[name]
        if not table.exists():
            sys.exit(f"noroshi: the domain table {table} is missing; build it with `noroshi frames` first")
        tables[name] = pd.read_csv(table, dtype=str, keep_default_na=False)
    live = p.info / "LIVE"
    if not live.exists():
        sys.exit(f"noroshi: the info page is not live ({live} does not exist); nothing was requested")
    if live_url(live) is None:
        sys.exit(f"noroshi: {live} holds no http:// or https:// URL of the study's info page; write the URL "
                 "there first (it is also the control for a local outage); nothing was requested")
    tables = {name: t for name, t in tables.items() if len(t)}  # an empty table has nothing to request
    kinds = {is_pilot(limit, len(t)) for t in tables.values()}  # a limit below the table size is a pilot
    if len(kinds) > 1:
        sys.exit(f"noroshi: --limit {limit} is below the size of some sector tables and not others; "
                 "use a smaller limit for a pilot, or no limit for a full pass")
    pilot = kinds.pop() if kinds else limit is not None
    if exclude_file is not None and not pilot:
        sys.exit(f"noroshi: --limit {limit} is not below the size of the sector tables, so this is a full pass; "
                 "--exclude is for a pilot sample only")
    if not pilot:
        problem = frozen_problem(p.frozen)
        if problem:
            sys.exit(f"noroshi: a full pass needs a frozen protocol: {problem}; use --limit with --seed for a pilot")
    try:
        optout = load_optout(p.info / "optout.txt")
    except OptoutError as exc:
        sys.exit(f"noroshi: {exc}; fix info/optout.txt first, nothing was requested")
    if exclude_file is not None:
        from noroshi.crawl import excluded_rows

        for name, t in tables.items():
            print(f"{name}: {int(excluded_rows(t, exclude).sum())} domains excluded")
    p.raw.mkdir(parents=True, exist_ok=True)
    store_path = p.raw / ("pilot.sqlite" if pilot else "crawl.sqlite")  # a pilot never touches main data
    progress = _Progress(p.raw / ("pilot_crawl.log" if pilot else "crawl.log"))

    async def run(store: Store) -> Counter:
        totals: Counter[str] = Counter()
        async with PoliteFetcher(store, optout, live) as fetcher:  # one fetcher, closed at the end
            watch = OutageWatch(store, pass_no, live_control(live, log=progress.note))  # one outage count
            for name in tables:  # in SECTOR_ORDER; a domain done under an earlier sector is not requested again
                totals.update(await crawl_sector(tables[name], pass_no, fetcher, store, limit=limit, seed=seed,
                                                 frozen_file=p.frozen, watch=watch, on_done=progress,
                                                 exclude=exclude))
        return totals

    with _run_lock(store_path):
        _no_pending_purge(store_path, pilot)
        print(f"store: {store_path}")
        try:
            with Store(store_path, p.raw / "pilot_blobs" if pilot else None) as store:
                counts = asyncio.run(run(store))
        except NotLive:
            sys.exit("noroshi: the info page went offline during the run; stopped")
        except NotFrozen as exc:
            sys.exit(f"noroshi: {exc}")
        except LocalOutage as exc:
            progress.write("stopped after ")
            sys.exit(f"noroshi: stopped: {exc}. Those domains are unmarked, so running this pass again "
                     "tries them once the network is back.")
        progress.write("finished: ")
    for status, n in sorted(counts.items()):
        print(f"{status}: {n}")


def _read_optout() -> frozenset[str]:
    try:
        return load_optout(config.paths().info / "optout.txt")
    except OptoutError as exc:
        sys.exit(f"noroshi: {exc}; fix info/optout.txt first")


def _frame_outputs(frame: str, sector: str | None = None) -> list[Path]:
    """The files a frame command writes."""
    p = config.paths()
    f = p.frames
    return {
        "hospitals": [f / "hifld_raw.json", f / "hifld_raw.meta.json", f / "hospitals.csv", f / "hospital_domains.csv"],
        "audit": [f / "hospital_audit_candidates.csv"],
        "federal": [p.raw / "current-federal.csv", f / "federal_domains.csv"],
        "companies": [f / "fda_ai_devices.csv", p.raw / "lantern.csv", p.raw / "csaf", f / "device_companies.csv",
                      f / "icsma_vendors.csv", f / "ehr_developers.csv", f / "company_sources.meta.json"],
        "resolve": [f / COMPANY_SECTORS[sector][1]] if sector else [],
    }[frame]


def _frames_guard(frame: str, sector: str | None, force: bool) -> None:
    """Frame tables are never rebuilt once the protocol is frozen, and never overwritten without --force."""
    frozen = config.paths().frozen
    if frozen.exists():
        sys.exit(f"noroshi: the protocol is frozen ({frozen} exists); the frames are not rebuilt")
    existing = [str(x) for x in _frame_outputs(frame, sector) if x.exists()]
    if existing and not force:
        sys.exit("noroshi: these outputs already exist: " + ", ".join(existing) + "; give --force to overwrite them")


COMPANY_SECTORS = {
    "device": ("device_companies.csv", "device_domains.csv", "medical device manufacturers"),
    "icsma": ("icsma_vendors.csv", "icsma_domains.csv", "medical device manufacturers"),
    "ehr": ("ehr_developers.csv", "ehr_domains.csv", "electronic health record software developers"),
}


def _frames_companies() -> None:
    import json

    from noroshi.frames import companies as c

    p = config.paths()
    fda, lantern, csaf = p.frames / "fda_ai_devices.csv", p.raw / "lantern.csv", p.raw / "csaf"
    meta = {"fda_ai_devices": c.download_fda(fda), "lantern": c.download_lantern(lantern),
            "csaf_icsma": c.download_csaf(csaf)}
    tables = {
        "fda_ai_devices": c.load_fda_ai_list(fda),
        "csaf_icsma": c.load_icsma_vendors(csaf),
        "lantern": c.load_ehr_developers(lantern),
    }
    for name, sector in (("fda_ai_devices", "device"), ("csaf_icsma", "icsma"), ("lantern", "ehr")):
        meta[name]["rows"] = len(tables[name])
        tables[name].to_csv(p.frames / COMPANY_SECTORS[sector][0], index=False)
        print(f"{sector}: {len(tables[name])} companies")
    (p.frames / "company_sources.meta.json").write_text(json.dumps(meta, indent=2) + "\n")


def _frames_resolve(sector: str, workers: int) -> None:
    import pandas as pd

    from noroshi import resolve as r
    from noroshi.config import JUDGE_MODELS

    p = config.paths()
    src, dest, context = COMPANY_SECTORS[sector]
    rows = pd.read_csv(p.frames / src, dtype=str, keep_default_na=False)
    a, b, s = (p.passes / f"{sector}_{x}.jsonl" for x in ("memory_a", "memory_b", "search"))
    r.memory_pass(rows, context, model=JUDGE_MODELS[1], out=a)
    r.memory_pass(rows, context, model=JUDGE_MODELS[0], out=b)
    r.search_pass(r.disagreements(a, b), context, out=s, workers=workers)
    resolved = r.reconcile(a, b, s)
    r.to_domain_table(resolved, sector).to_csv(p.frames / dest, index=False)
    counts = resolved.resolution.value_counts()
    print(f"{sector}: {len(resolved)} names, {counts.get('agreed', 0)} agreed, "
          f"{counts.get('search', 0)} search, {counts.get('unresolved', 0)} unresolved")


def _frames_hospitals() -> None:
    from noroshi.frames.hospitals import build_hospital_frame, download_hifld

    p = config.paths()
    raw = p.frames / "hifld_raw.json"
    download_hifld(raw)
    hospitals, domains = build_hospital_frame(raw, p.rucc)
    hospitals.to_csv(p.frames / "hospitals.csv", index=False)
    domains.to_csv(p.frames / "hospital_domains.csv", index=False)
    print(f"{len(hospitals)} records, {int(hospitals.exclusion.isna().sum())} kept, {len(domains)} domains")


def _frames_audit() -> None:
    import pandas as pd

    from noroshi.frames.hospitals import audit_candidates

    frames = config.paths().frames
    cand = audit_candidates(pd.read_csv(frames / "hospitals.csv", dtype=str))
    cand.to_csv(frames / "hospital_audit_candidates.csv", index=False)
    print(f"{len(cand)} audit candidates")


def _frames_federal() -> None:
    from noroshi.frames.federal import build_federal_frame, download_federal

    p = config.paths()
    raw = p.raw / "current-federal.csv"
    download_federal(raw)
    frame = build_federal_frame(raw)
    frame.to_csv(p.frames / "federal_domains.csv", index=False)
    print(f"{len(frame)} federal domains")



def _judge(which: str = "main") -> None:
    from noroshi import judge, llm
    from noroshi.store import Store

    raw = config.paths().raw
    pilot = which == "pilot"
    db = raw / ("pilot.sqlite" if pilot else "crawl.sqlite")
    if not db.exists():
        sys.exit(f"noroshi: {db} does not exist; run `noroshi crawl` first")
    out = raw / ("pilot_judgements.jsonl" if pilot else "judgements.jsonl")
    optout = _read_optout()
    with (_run_lock(db, "nothing was judged", "noroshi judge"), Store(db, raw / "pilot_blobs" if pilot else None) as store,
          llm.stop_on_signals()):
        _no_pending_purge(db, pilot)
        try:
            counts = judge.run_judging(store, out, optout=optout)
        except judge.JudgingStopped as stop:
            for name, n in stop.counts.items():
                print(f"{name}: {n}")
            sys.exit(f"noroshi: {stop}; every page judged so far is kept in {out}. Run `noroshi judge"
                     f"{' --store pilot' if pilot else ''}` again later to resume.")
        except llm.CallsStopped:
            stopped = True
        else:
            stopped = False
    if stopped:  # the lock is released only now, after the call in flight was logged
        sys.exit(f"noroshi: judging was stopped by a signal; the call in flight finished and was logged, and every page "
                 f"judged before it is kept in {out}. Run `noroshi judge{' --store pilot' if pilot else ''}` again to "
                 "resume.")
    for name, n in counts.items():
        print(f"{name}: {n}")


def _domain_tables() -> "pd.DataFrame":
    """Every sector table of the frames, one after the other (a domain in several tables has a row in each)."""
    import pandas as pd

    p = config.paths()
    tables = [p.frames / name for name in SECTOR_TABLES.values() if (p.frames / name).exists()]
    if not tables:
        sys.exit(f"noroshi: no domain tables in {p.frames}; build them with `noroshi frames` first")
    return pd.concat([pd.read_csv(t, dtype=str, keep_default_na=False) for t in tables], ignore_index=True)


def _identity(which: str = "main", workers: int = 4) -> None:
    from noroshi import identity, llm
    from noroshi.store import Store

    p = config.paths()
    pilot = which == "pilot"
    db = p.raw / ("pilot.sqlite" if pilot else "crawl.sqlite")
    if not db.exists():
        sys.exit(f"noroshi: {db} does not exist; run `noroshi crawl` first")
    domains = _domain_tables()
    names = identity.listings(p.frames)
    out = p.raw / ("pilot_identity.jsonl" if pilot else "identity.jsonl")
    optout = _read_optout()
    stopped = False
    with (_run_lock(db, "nothing was asked", "noroshi identity"),
          Store(db, p.raw / "pilot_blobs" if pilot else None) as store, llm.stop_on_signals()):
        _no_pending_purge(db, pilot)
        missing = identity.unnamed(store, domains, names, optout)
        if missing:
            files = sorted({f for _, sectors in missing for sector in sectors if sector != "federal"
                            for f in identity.name_sources(p.frames, sector)})
            sys.exit(f"noroshi: {len(missing)} measured domains have no organization name to put in the prompt: "
                     + ", ".join(f"{d} ({'/'.join(sectors)})" for d, sectors in missing[:20])
                     + (", ..." if len(missing) > 20 else "") + "; their names come from "
                     + ", ".join(f"{f.relative_to(p.root)} ({'no name for it there' if f.exists() else 'missing'})"
                                 for f in files) + "; nothing was asked")
        try:
            counts = identity.run_identity(store, domains, names, out, optout=optout, workers=workers)
        except identity.IdentityStopped as stop:
            for name, n in stop.counts.items():
                print(f"{name}: {n}")
            sys.exit(f"noroshi: {stop}; every decision so far is kept in {out}. Run `noroshi identity"
                     f"{' --store pilot' if pilot else ''}` again later to resume.")
        except OSError as exc:
            sys.exit(f"noroshi: a decision could not be written to {out} ({exc}); the run stopped, and every decision "
                     "written before is kept")
        except llm.CallsStopped:
            stopped = True  # every worker has ended: the lock is released below, after the last call was logged
    if stopped:
        sys.exit(f"noroshi: the identity check was stopped by a signal; the calls in flight finished and were logged, and "
                 f"every complete decision is kept in {out}. Run `noroshi identity{' --store pilot' if pilot else ''}` "
                 "again to resume.")
    for name, n in counts.items():
        print(f"{name}: {n}")


def _no_pending_purge(db: Path, pilot: bool) -> None:
    """Exit while a purge on this store was stopped before it finished: until it is run again, the store and the files
    hold a half-done purge (records gone, rows and the purge log not yet), which nothing may read or add to."""
    from noroshi.optout import pending_purges

    left = pending_purges(db)
    if left:
        sys.exit(f"noroshi: a purge of {', '.join(left)} was stopped before it finished; run `noroshi purge {left[0]}"
                 f"{' --store pilot' if pilot else ''}` again first (nothing else runs on this store until it has)")


def _undecided(derived) -> int:
    """Measured domains (counted once) with no identity decision for the homepage derive read."""
    if "identity" not in derived:
        return int(derived[derived.status == "measured"].domain.nunique())
    left = derived[(derived.status == "measured") & (derived.identity.fillna("").astype(str) == "")]
    return int(left.domain.nunique())


def _registries_fetch() -> None:
    import json

    from noroshi import registries

    regs = config.paths().registries
    registries.fetch_snapshots(regs)
    meta = json.loads((regs / "registries.meta.json").read_text())
    for name, entry in meta["files"].items():
        print(f"{name}: {entry['entries']} entries, commit {entry['commit'][:10]}, stored in {entry['stored_in']}")


def _wayback(first_seen_csv: str | None, redirects_csv: str | None, which: str = "main") -> None:
    import csv
    import json

    from noroshi import wayback

    from noroshi.optout import matches

    if not (first_seen_csv or redirects_csv):
        sys.exit("noroshi: give --first-seen and/or --redirects")
    optout = _read_optout()
    prefix = "pilot_" if which == "pilot" else ""  # a pilot never writes the main result files
    get = wayback.throttled(wayback.httpx.get, interval=config.WAYBACK_INTERVAL_SECONDS)  # every request to the archive
    in_a_row = [0]  # failed lookups in a row, across both files

    class Stop(Exception):
        """The archive refused a connection, or failed WAYBACK_MAX_FAILED_IN_A_ROW lookups in a row."""

    def run(csv_path: str, out: Path, key_cols: tuple[str, ...], lookup) -> None:
        done = set()
        if out.exists():
            for line in out.read_text(encoding="utf-8", errors="replace").splitlines():
                try:  # a line cut off by a killed run is ignored, so that row is looked up again
                    row = json.loads(line)
                    done.add(tuple(row[c] for c in key_cols))
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
            if out.stat().st_size and not out.read_bytes().endswith(b"\n"):
                with out.open("a", encoding="utf-8") as fh:
                    fh.write("\n")  # finish the cut-off line, so the next row starts on its own line
        out.parent.mkdir(parents=True, exist_ok=True)
        failed = skipped = 0
        with open(csv_path, newline="", encoding="utf-8") as src, out.open("a", encoding="utf-8") as dst:
            for row in csv.DictReader(src):
                if tuple(row[c] for c in key_cols) in done:
                    continue
                if matches(row["domain"], optout) or matches(row["host"], optout):
                    skipped += 1  # opted out after derive wrote this file: not looked up
                    continue
                try:
                    result = lookup(row)
                except (wayback.WaybackError, wayback.httpx.HTTPError) as exc:
                    # nothing is written for this row, so a re-run retries it
                    in_a_row[0] += 1
                    if wayback.refused(exc):
                        raise Stop(f"the Internet Archive refused the connection ({row['host']}: {exc})") from None
                    if in_a_row[0] >= config.WAYBACK_MAX_FAILED_IN_A_ROW:
                        raise Stop(f"the Internet Archive failed {in_a_row[0]} lookups in a row (the last, "
                                   f"{row['host']}: {exc})") from None
                    print(f"noroshi: {row['host']}: {exc}")
                    failed += 1
                    continue
                in_a_row[0] = 0
                dst.write(json.dumps({**{c: row[c] for c in key_cols}, **result}) + "\n")
                dst.flush()
        print(f"{out.name} failed: {failed}, opted out: {skipped}")

    def first(row):
        d = wayback.first_seen(row["host"], row["path"], get=get)
        return {"first_seen": d.isoformat() if d else None}
    try:
        if first_seen_csv:
            run(first_seen_csv, config.paths().raw / f"{prefix}wayback.jsonl", ("domain", "host", "path"), first)
        if redirects_csv:
            run(redirects_csv, config.paths().raw / f"{prefix}wayback_redirects.jsonl", ("domain", "host"),
                lambda row: {"redirect_target": wayback.last_redirect_target(row["host"], get=get)})
    except Stop as stop:
        sys.exit(f"noroshi: {stop}; nothing was written for that lookup and every lookup stored so far is kept. Run the "
                 "same command again later to go on.")


def _derive(which: str) -> None:
    from datetime import datetime, timezone

    import pandas as pd

    from noroshi import outcomes, registries
    from noroshi.store import Store

    p = config.paths()
    pilot = which == "pilot"
    db = p.raw / ("pilot.sqlite" if pilot else "crawl.sqlite")
    if not db.exists():
        sys.exit(f"noroshi: {db} does not exist; run `noroshi crawl` first")
    _no_pending_purge(db, pilot)
    domains = _domain_tables()
    judgements = p.raw / ("pilot_judgements.jsonl" if pilot else "judgements.jsonl")
    if not judgements.exists():
        print(f"noroshi: {judgements} does not exist; domains with stored pages get policy_page unmeasured")
    try:
        regs = registries.load(p.registries)
    except FileNotFoundError as exc:
        sys.exit(f"noroshi: {exc}")
    optout = _read_optout()
    out = p.derived / "pilot" if pilot else p.derived
    out.mkdir(parents=True, exist_ok=True)
    with Store(db, p.raw / "pilot_blobs" if pilot else None) as store:
        derived = outcomes.derive_domains(store, domains, judgements, regs,
                                          p.raw / ("pilot_wayback.jsonl" if pilot else "wayback.jsonl"),
                                          datetime.now(timezone.utc), optout=optout,
                                          redirects=p.raw / ("pilot_wayback_redirects.jsonl" if pilot
                                                             else "wayback_redirects.jsonl"),
                                          identity=p.raw / ("pilot_identity.jsonl" if pilot else "identity.jsonl"))
    derived.to_csv(out / "domains.csv", index=False)
    measured = derived[derived.status == "measured"]
    unjudged = int((measured.policy_pages_missing_judgement.fillna(0).astype(int) > 0).sum())
    if unjudged:
        print(f"noroshi: {unjudged} measured domains have stored pages with no judgement row (their policy page is "
              f"unmeasured); run `noroshi judge{' --store pilot' if pilot else ''}` and then derive again")
    undecided = _undecided(derived)
    once = derived.drop_duplicates("domain")
    decided = {s: int((once.identity == s).sum()) for s in ("own", "other", "unclear")}
    print(f"noroshi: identity check: {sum(decided.values())} domains decided ({decided['own']} own, "
          f"{decided['other']} other, {decided['unclear']} unclear), {int((once.identity == 'exempt').sum())} exempt, "
          f"{undecided} measured domains without a decision yet")
    if undecided:
        print(f"noroshi: {undecided} measured domains have no identity decision yet; run `noroshi identity"
              f"{' --store pilot' if pilot else ''}` and then derive again")
    hospitals = pd.read_csv(p.frames / "hospitals.csv", dtype=str, keep_default_na=False)
    outcomes.derive_hospitals(hospitals, derived).to_csv(out / "hospitals.csv", index=False)
    first, redirects = outcomes.wayback_inputs(derived)
    first.to_csv(out / "wayback_first_seen.csv", index=False)
    redirects.to_csv(out / "wayback_redirects.csv", index=False)
    print(f"{len(derived)} domains, {len(first)} to date, {len(redirects)} unreachable hospital domains -> {out}")


def _purge(domain: str, which: str) -> None:
    from noroshi import optout as oo
    from noroshi.crawl import normalize_domain
    from noroshi.store import Store

    p = config.paths()
    pilot = which == "pilot"
    db = p.raw / ("pilot.sqlite" if pilot else "crawl.sqlite")
    if not db.exists():
        sys.exit(f"noroshi: {db} does not exist; nothing to purge")
    name = oo.site_name(normalize_domain(domain))  # the purge works on the site's registrable domain
    if not oo.matches(name, _read_optout()):
        sys.exit(f"noroshi: {name} is not in info/optout.txt; add it there first, so it is never requested again, "
                 "then purge it; nothing was removed")
    prefix = "pilot_" if pilot else ""
    other = p.raw / ("crawl.sqlite" if pilot else "pilot.sqlite")
    # both stores' locks: the model-call log the purge rewrites is one file for the main and the pilot store, so no
    # crawl, judge or identity run on either store may run beside a purge of either
    with (_run_lock(db, "nothing was removed", "noroshi purge"), _run_lock(other, "nothing was removed", "noroshi purge"),
          Store(db, p.raw / "pilot_blobs" if pilot else None) as store):
        removed = oo.purge(store, name, p.raw / f"{prefix}judgements.jsonl",
                           [p.raw / f"{prefix}wayback.jsonl", p.raw / f"{prefix}wayback_redirects.jsonl"], p.llm_log,
                           identity=p.raw / f"{prefix}identity.jsonl", purge_log=p.raw / f"{prefix}purge.log",
                           store_name=db.name)
    # the records are gone, so the purge log's line is what the paper reports: an opt-out honored, and what had been sent
    counts = ", ".join(f"{n} {what}" for what, n in removed.items())
    print(f"purged {name} from {db}: {counts}")
    print(f"run `noroshi derive{' --store pilot' if pilot else ''}` again, so the derived tables drop it")


def _requests_sent_purged(purge_log: Path) -> int:
    """The sum of "requests sent" over the lines `_purge` wrote to a purge log; 0 when there is no log."""
    if not purge_log.exists():
        return 0
    return sum(int(m.group(1)) for line in purge_log.read_text(encoding="utf-8").splitlines()
               if (m := re.search(r"\b(\d+) requests sent\b", line)))


def _jsonl(path: Path):
    """The JSON objects of a result file; a line cut off by a killed run is skipped."""
    import json

    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            yield row


def _analyze(which: str, allow_incomplete: bool = False) -> None:
    import pandas as pd

    from noroshi.analysis import figures, tables

    p = config.paths()
    pilot = which == "pilot"
    src = p.derived / "pilot" if pilot else p.derived
    dest = p.results / "pilot" if pilot else p.results
    _no_pending_purge(p.raw / ("pilot.sqlite" if pilot else "crawl.sqlite"), pilot)
    if not (src / "domains.csv").exists() or not (src / "hospitals.csv").exists():
        sys.exit(f"noroshi: no derived tables in {src}; run `noroshi derive` first")
    purge_log = p.raw / ("pilot_purge.log" if pilot else "purge.log")
    if purge_log.exists() and purge_log.stat().st_mtime_ns > (src / "domains.csv").stat().st_mtime_ns:
        sys.exit(f"noroshi: {purge_log} is newer than {src / 'domains.csv'}: a purge ran after derive, so the purged "
                 f"requests would be counted twice; run `noroshi derive{' --store pilot' if pilot else ''}` again first")
    from noroshi.optout import unpurged

    left = unpurged(p.raw / ("pilot.sqlite" if pilot else "crawl.sqlite"), _read_optout())
    if left:
        sys.exit(f"noroshi: {', '.join(left)} opted out but the store still holds what was collected from "
                 f"{'it' if len(left) == 1 else 'them'}; run `noroshi purge {left[0]}"
                 f"{' --store pilot' if pilot else ''}` (for each) and `noroshi derive` first")
    domains = pd.read_csv(src / "domains.csv", dtype=str, keep_default_na=False)
    hospitals = pd.read_csv(src / "hospitals.csv", dtype=str, keep_default_na=False)
    missing = int((domains.status == "not_crawled").sum())
    undecided = _undecided(domains)
    if not pilot and not allow_incomplete and (missing or undecided):
        problems = []
        if missing:
            problems.append(f"{missing} domains are not_crawled (the main run is incomplete; they would leave every "
                            "share silently); finish the crawl")
        if undecided:
            problems.append(f"{undecided} measured domains have no identity decision (they would count as the "
                            "organizations' own sites unchecked); run `noroshi identity` and derive again")
        sys.exit(f"noroshi: {'; and '.join(problems)}; or give --allow-incomplete to analyze anyway")
    if undecided and pilot:
        print(f"noroshi: {undecided} measured domains have no identity decision yet (the pilot is analyzed anyway)")
    if allow_incomplete and not pilot and (missing or undecided):
        print(f"noroshi: --allow-incomplete: analyzing with {missing} domains not crawled and {undecided} measured "
              "domains without an identity decision (counted as the organizations' own sites)")
    purged = _requests_sent_purged(purge_log)
    prefix = "pilot_" if pilot else ""
    numbers = tables.build_all(domains, hospitals, dest / "tables", requests_sent_purged=purged,
                               judgements=list(_jsonl(p.raw / f"{prefix}judgements.jsonl")),
                               identity=list(_jsonl(p.raw / f"{prefix}identity.jsonl")))
    tables.write_numbers(numbers, dest / "numbers.json")
    tables.write_parser_disagreements(domains, dest / "parser_disagreements.csv")
    figures.shares_by_sector(numbers, dest / "figures")
    figures.first_seen_curves(domains, dest / "figures")
    print(f"{len(numbers)} numbers -> {dest}")


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "crawl":
        _crawl(args.sector, args.pass_no, args.limit, args.seed, args.exclude)
        return
    if args.command == "derive":
        _derive(args.store)
        return
    if args.command == "analyze":
        _analyze(args.store, args.allow_incomplete)
        return
    if args.command == "judge":
        _judge(args.store)
        return
    if args.command == "identity":
        _identity(args.store, args.workers)
        return
    if args.command == "purge":
        _purge(args.domain, args.store)
        return
    if args.command == "registries":
        _registries_fetch()
        return
    if args.command == "wayback":
        _wayback(args.first_seen, args.redirects, args.store)
        return
    if args.command == "frames":
        _frames_guard(args.frame, getattr(args, "sector", None), args.force)
        config.paths().frames.mkdir(parents=True, exist_ok=True)
        if args.frame == "resolve":
            _frames_resolve(args.sector, args.workers)
        else:
            {"hospitals": _frames_hospitals, "federal": _frames_federal, "companies": _frames_companies, "audit": _frames_audit}[args.frame]()
        return
    parser.print_help()
