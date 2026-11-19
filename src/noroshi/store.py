"""SQLite request log plus content-addressed body blobs.

Every attempt is written as an `in_flight` row before it is sent and completed in place afterwards, so a
hard kill never leaves a sent request without a row. Store failures are never swallowed.
"""
import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import astuple, dataclass, fields, replace
from pathlib import Path

from noroshi.domains import normalize_url, url_key


@dataclass
class FetchRecord:
    domain: str
    sector: str
    host: str
    url: str
    purpose: str
    pass_no: int
    ua_kind: str  # "identified" or "browser"
    started_at: str
    status: int | None
    final_url: str | None
    redirect_chain: list[str]
    content_type: str | None
    body_sha256: str | None
    body_len: int
    truncated: bool
    error: str | None
    blocked: bool
    site: str = ""  # registrable domain of the requested URL's host
    reused: bool = False  # a copy of a measurement made for another listed domain in the same pass: nothing was sent
    error_detail: str | None = None  # an attempt that ended in an error: the exception's class name and message, cut
    # to 300 characters
    error_url: str | None = None  # an attempt that ended in an error: the address of the hop it ended at (its start URL
    # or a redirect target)


IN_FLIGHT = "in_flight"
# errors of attempts that were sent but are never an outcome: counted toward every cap, never reused
NOT_OUTCOMES = ("cancelled", IN_FLIGHT, "outage")
BUSY_TIMEOUT_SECONDS = 30.0


def is_answer(status: int | None, blocked: bool, error: str | None) -> bool:
    """The one definition of a measurement: the request completed, was not refused, and its final status is 200 (the
    thing was read) or a 4xx that is not a refusal (the thing is not there). Any other final status (another 2xx, a
    3xx, a 5xx) means the request could not be measured."""
    return error is None and not blocked and status is not None and (status == 200 or 400 <= status < 500)


def homepage_record(records: list[FetchRecord]) -> FetchRecord | None:
    """The homepage of one listed domain in one pass: its newest homepage record among outcomes (not cancelled, in
    flight or relabelled outage). For a domain that ends measured this is the record crawl_domain measured from;
    look, needs_retry and is_forbidden take the homepage from here."""
    homes = [r for r in records if r.purpose == "homepage" and r.error not in NOT_OUTCOMES]
    return homes[-1] if homes else None


def measured_homepage(records_by_pass: dict[int, list[FetchRecord]], measured_passes) -> FetchRecord | None:
    """The homepage a domain was measured from: `homepage_record` of the latest of its passes that ended measured
    (derive and the identity check read it here). None when no pass measured it."""
    return homepage_record(records_by_pass[max(measured_passes)]) if measured_passes else None


_COLS = [f.name for f in fields(FetchRecord)]

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    {", ".join(_COLS)}
);
CREATE INDEX IF NOT EXISTS requests_domain_pass ON requests (domain, pass_no);
CREATE INDEX IF NOT EXISTS requests_site_pass ON requests (site, pass_no);
CREATE INDEX IF NOT EXISTS requests_url_pass ON requests (url, pass_no);
CREATE TABLE IF NOT EXISTS done (
    domain TEXT NOT NULL, pass_no INTEGER NOT NULL, status TEXT NOT NULL,
    PRIMARY KEY (domain, pass_no)
);
CREATE TABLE IF NOT EXISTS browser_sites (
    site TEXT NOT NULL, pass_no INTEGER NOT NULL,
    PRIMARY KEY (site, pass_no)
);
CREATE TABLE IF NOT EXISTS purged (
    domain TEXT NOT NULL, pass_no INTEGER NOT NULL, purpose TEXT NOT NULL, url_sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS purged_domain_pass ON purged (domain, pass_no);
CREATE TABLE IF NOT EXISTS purged_counts (
    pass_no INTEGER NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL, n INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS purged_counts_name ON purged_counts (kind, name, pass_no);
CREATE TABLE IF NOT EXISTS purge_pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT NOT NULL, plan TEXT NOT NULL
);
"""


def purged_key(url: str) -> str:
    """How the `purged` table keeps a requested URL: the SHA-256 of its key (`domains.url_key`), so the trace of a
    record purge removed never holds the address of a site that opted out."""
    return hashlib.sha256(url_key(url).encode("utf-8")).hexdigest()


class Store:
    """Blobs live in a `blobs/` directory beside the database file."""

    def __init__(self, path: Path, blob_dir: Path | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.blob_dir = Path(blob_dir) if blob_dir else self.path.parent / "blobs"
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_SECONDS)
        self.db.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_SECONDS * 1000)}")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript(_SCHEMA)
        have = {row[1] for row in self.db.execute("PRAGMA table_info(requests)")}
        for col in _COLS:
            if col not in have:  # a store written before the column existed
                self.db.execute(f"ALTER TABLE requests ADD COLUMN {col}")
        self.db.commit()

    def _values(self, rec: FetchRecord, body: bytes | None) -> list:
        if body is not None:
            sha = hashlib.sha256(body).hexdigest()
            blob = self.blob_dir / sha
            if not blob.exists():
                self._write_blob(blob, body)
            rec.body_sha256 = sha
            rec.body_len = len(body)
        vals = list(astuple(rec))
        vals[_COLS.index("redirect_chain")] = json.dumps(rec.redirect_chain)
        return vals

    def record(self, rec: FetchRecord, body: bytes | None) -> int:
        """Insert a finished record (a reused copy, or a fixture)."""
        cur = self.db.execute(
            f"INSERT INTO requests ({', '.join(_COLS)}) VALUES ({', '.join('?' * len(_COLS))})",
            self._values(rec, body),
        )
        self.db.commit()
        return cur.lastrowid

    def begin(self, rec: FetchRecord) -> int:
        """Write the `in_flight` row of an attempt that is about to be sent; returns its row id."""
        return self.record(replace(rec, error=IN_FLIGHT), None)

    def finish(self, row_id: int, rec: FetchRecord, body: bytes | None) -> None:
        """Complete the row written by `begin` with the attempt's outcome."""
        self.db.execute(f"UPDATE requests SET {', '.join(c + ' = ?' for c in _COLS)} WHERE id = ?",
                        [*self._values(rec, body), row_id])
        self.db.commit()

    def mark_429(self, row_id: int, final_url: str) -> None:
        """Save a 429 on the attempt's row the moment it arrives, with the URL that answered it: a cancel, a failure
        or a kill before the attempt ends still leaves the 429 for `rate_limits` to read back."""
        self.db.execute("UPDATE requests SET status = 429, final_url = ?, blocked = 1 WHERE id = ?",
                        (final_url, row_id))
        self.db.commit()

    def _write_blob(self, blob: Path, body: bytes) -> None:
        # temp file in the same folder, then an atomic rename: a killed crawl never leaves a
        # truncated file under a real hash name
        fd, tmp = tempfile.mkstemp(dir=self.blob_dir, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(body)
            os.replace(tmp, blob)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get_blob(self, sha256: str) -> bytes:
        return (self.blob_dir / sha256).read_bytes()

    @staticmethod
    def _to_record(row) -> FetchRecord:
        d = dict(zip(_COLS, row))
        d["redirect_chain"] = json.loads(d["redirect_chain"])
        d["truncated"] = bool(d["truncated"])
        d["blocked"] = bool(d["blocked"])
        d["reused"] = bool(d["reused"])
        return FetchRecord(**d)

    def requests_for(self, domain: str, pass_no: int) -> list[FetchRecord]:
        rows = self.db.execute(
            f"SELECT {', '.join(_COLS)} FROM requests WHERE domain = ? AND pass_no = ? ORDER BY id",
            (domain, pass_no),
        ).fetchall()
        return [self._to_record(row) for row in rows]

    def sent(self, pass_no: int) -> list[tuple[int, FetchRecord]]:
        """(row id, record) of every attempt sent in this pass, for any listed domain, in the order written; reused
        copies were not sent and are left out."""
        rows = self.db.execute(
            f"SELECT id, {', '.join(_COLS)} FROM requests WHERE pass_no = ? AND NOT reused ORDER BY id", (pass_no,)
        ).fetchall()
        return [(row[0], self._to_record(row[1:])) for row in rows]

    def records_by_purpose(self, purposes: tuple[str, ...]) -> list[FetchRecord]:
        """Every stored record with one of these purposes, in the order it was stored."""
        rows = self.db.execute(
            f"SELECT {', '.join(_COLS)} FROM requests WHERE purpose IN ({', '.join('?' * len(purposes))}) ORDER BY id",
            purposes,
        ).fetchall()
        return [self._to_record(row) for row in rows]

    def _purged_count(self, kind: str, name: str, pass_no: int) -> int:
        """What purges removed and wrote down for a resumed pass (`purged_counts`, no address of a purged site)."""
        return self.db.execute("SELECT COALESCE(SUM(n), 0) FROM purged_counts WHERE kind = ? AND name = ? AND "
                               "pass_no = ?", (kind, name, pass_no)).fetchone()[0]

    def count_requests(self, domain: str, pass_no: int) -> int:
        """Attempts sent for this listed domain in this pass, in-flight ones included; reused copies are not. Attempts
        whose records a purge of another site removed still count."""
        return self.db.execute(
            "SELECT COUNT(*) FROM requests WHERE domain = ? AND pass_no = ? AND NOT reused", (domain, pass_no)
        ).fetchone()[0] + self._purged_count("domain", domain, pass_no)

    def count_site_requests(self, site: str, pass_no: int) -> int:
        """Attempts sent to this site in this pass, for any listed domain, in-flight ones included. Homepage
        attempts are not counted: each listed domain's own homepage (and its one browser retry) counts only
        toward that domain's cap. Attempts whose records a purge of another site removed still count."""
        return self.db.execute(
            "SELECT COUNT(*) FROM requests WHERE site = ? AND pass_no = ? AND NOT reused AND purpose != 'homepage'",
            (site, pass_no),
        ).fetchone()[0] + self._purged_count("site", site, pass_no)

    def mark_browser(self, site: str, pass_no: int) -> None:
        """The site refused the identified signature and the browser retry of that request was not refused: the
        rest of the site's requests in this pass use the browser signature."""
        self.db.execute("INSERT OR IGNORE INTO browser_sites (site, pass_no) VALUES (?, ?)", (site, pass_no))
        self.db.commit()

    def browser_sites(self, pass_no: int) -> set[str]:
        """Sites switched to the browser signature in this pass (`mark_browser`)."""
        return {row[0] for row in self.db.execute("SELECT site FROM browser_sites WHERE pass_no = ?", (pass_no,))}

    def purged(self, domain: str, pass_no: int) -> set[str]:
        """`purged_key`s of the records of this listed domain and pass that a purge of another site removed: the look
        counts such a planned page as skipped (candidate_skipped), never as cut by the request limit."""
        return {row[0] for row in self.db.execute("SELECT url_sha256 FROM purged WHERE domain = ? AND pass_no = ?",
                                                  (domain, pass_no))}

    def rate_limits(self, pass_no: int) -> list[tuple[str, str | None]]:
        """(url, final_url) of every row of this pass that got a 429; and, for each site whose 429 record a purge of
        another site removed, the site's root address with no final URL (the fetcher reads the site from either)."""
        rows = self.db.execute(
            "SELECT url, final_url FROM requests WHERE pass_no = ? AND status = 429", (pass_no,)
        ).fetchall()
        return rows + [(f"https://{name}/", None) for (name,) in self.db.execute(
            "SELECT DISTINCT name FROM purged_counts WHERE kind = 'rate_limited' AND pass_no = ?", (pass_no,))]

    def find_measurement(self, url: str, pass_no: int) -> FetchRecord | None:
        """The latest record of this URL (in its one spelling, `domains.normalize_url`) in this pass that is a
        measurement (`is_answer`)."""
        url = normalize_url(url)
        rows = self.db.execute(
            f"SELECT {', '.join(_COLS)} FROM requests WHERE url = ? AND pass_no = ? ORDER BY id DESC", (url, pass_no),
        ).fetchall()
        return next((r for r in map(self._to_record, rows) if is_answer(r.status, r.blocked, r.error)), None)

    def mark_done(self, domain: str, pass_no: int, status: str) -> None:
        self.db.execute(
            "INSERT INTO done (domain, pass_no, status) VALUES (?, ?, ?) "
            "ON CONFLICT(domain, pass_no) DO UPDATE SET status = excluded.status",
            (domain, pass_no, status),
        )
        self.db.commit()

    def mark_outage(self, domains: list[str], pass_no: int, network_errors: tuple[str, ...]) -> None:
        """A local outage: unmark these domains for the pass and relabel their network-error homepage
        records `outage` (sent, so still counted; never an outcome)."""
        marks = ", ".join("?" * len(domains))
        errs = ", ".join("?" * len(network_errors))
        with self.db:
            self.db.execute(f"UPDATE requests SET error = 'outage' WHERE pass_no = ? AND purpose = 'homepage' "
                            f"AND domain IN ({marks}) AND error IN ({errs})", (pass_no, *domains, *network_errors))
            self.db.execute(f"DELETE FROM done WHERE pass_no = ? AND domain IN ({marks})", (pass_no, *domains))

    def done_status(self, domain: str, pass_no: int) -> str | None:
        row = self.db.execute(
            "SELECT status FROM done WHERE domain = ? AND pass_no = ?", (domain, pass_no)
        ).fetchone()
        return row[0] if row else None
