"""Decide which stored pages are vulnerability-disclosure policy pages.

Pages that cannot be read, a site's catch-all page and pages a followed meta refresh stands in for are set aside
by rule; a text prefilter drops clear negatives; two models judge the rest independently and a third breaks ties.
Every model call is logged by `llm.ask_claude`."""
import hashlib
import json
import re
import subprocess
from collections import defaultdict
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path

from noroshi import blocking
from noroshi.config import JUDGE_MODELS, JUDGE_TIEBREAK
from noroshi.crawl import PAGE_PURPOSES, refresh_target
from noroshi.discover import PAGE_TEXT_PATTERN, PLATFORM_HOSTS
from noroshi.domains import url_key
from noroshi.llm import ask_claude, check_stop, extract_json, stop_requested
from noroshi.optout import is_opted_out, url_matches
from noroshi.store import homepage_record, is_answer

SYSTEM = "You classify web pages for a research study. Answer only with a JSON object."
JUDGED_PURPOSES = ("policy_field", "candidate", "probe")
JUDGE_PURPOSE = "judge_policy"  # how every judge and tie-break call is labelled in the model-call log
_SECURITY_TXT = re.compile(r"security\.txt", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)+|mailto:", re.IGNORECASE)
_PLATFORM = re.compile("|".join(re.escape(h) for h in PLATFORM_HOSTS), re.IGNORECASE)
EXCERPT_CHARS = 40_000  # a page this long or shorter is sent whole
OPENING_CHARS = 20_000  # a longer page: its opening, then windows around every hit
WINDOW_CHARS = 1_000  # on each side of a hit
_GAP = " ... "
_FIELDS = ("invites_reports", "contact_method", "safe_harbor")
# Skip reasons of a page that is never sent to a model. The first three leave the page unread, so the look for a
# policy page is incomplete (look.UNREADABLE_PAGE); the others decide the page is read and is not a policy page.
UNREADABLE = "unreadable"  # fewer than MIN_VISIBLE_CHARS visible characters, or cut at the size cap and screened out
UNREADABLE_SKIPS = ("pdf_unreadable", "non_text", UNREADABLE)
CATCH_ALL = "catch_all"  # one text at two or more conventional paths of a domain in a pass, with different final URLs
META_REFRESH = "meta_refresh"  # a page that answered with a meta refresh that was followed: its target stands in
IMAGE = "image"  # a page answered with an image (image/*): read, and not a policy page (fix round 3, K7)
SAME_AS_HOMEPAGE = "same_as_homepage"  # a candidate or conventional-path page whose final URL is the homepage's own
# final URL (same key): it is the homepage, whatever its size or whether it was cut (fix round 3, K7)
MIN_VISIBLE_CHARS = blocking.MIN_VISIBLE_CHARS  # 200, the crawl's own measure (text outside title, script, style)
PDF_STOPPED = "pdf_stopped"  # pdftotext ended by a signal after a stop was asked: no row; a later run reads the page

PROMPT = """You are classifying one web page for a research study. The page was fetched from an organization's website.

Page text:
<<<
{excerpt}
>>>

Answer three questions about what this page itself says.

1. invites_reports: Does this page tell people outside the organization how to report a security vulnerability in the organization's own websites, systems or products? A page that points reporters to a reporting address or to a disclosure platform counts. Answer false for a page whose only relevant content is a navigation or footer link to a policy that lives elsewhere, for a general home, navigation or error page, and for pages about patient privacy complaints, compliance or fraud hotlines, phishing emails someone received, or physical security.
2. contact_method: Does this page give a way to send such a report: an email address, a web form, a phone number, or a link to a disclosure platform? Answer false when your answer to question 1 is false.
3. safe_harbor: Does this page commit not to pursue legal action against people who report in good faith? Answer false when your answer to question 1 is false.

Reply with one JSON object and nothing else. It has exactly three keys, "invites_reports", "contact_method" and "safe_harbor", and each value is true or false according to your answers."""
# The page's address is not in the prompt (the models judge the text alone); it goes to the model-call log as `meta`.


class JudgeError(RuntimeError):
    pass


MAX_FAILED_IN_A_ROW = 5  # pages whose judging failed one after another: a usage limit or an outage


class JudgingStopped(RuntimeError):
    """Judging stopped after MAX_FAILED_IN_A_ROW pages in a row failed; `counts` says what was done. Every page
    written so far stays, and a failed page is not written, so a re-run resumes."""

    def __init__(self, counts: dict[str, int]):
        super().__init__(f"judging stopped after {MAX_FAILED_IN_A_ROW} pages in a row failed (a usage limit or an "
                         "outage?)")
        self.counts = counts


@dataclass(frozen=True)
class Judgement:
    invites_reports: bool
    contact_method: bool
    safe_harbor: bool


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def visible_text(html: bytes) -> str:
    """Text a reader would see: no script, style or noscript content, whitespace collapsed.
    A body that is not HTML comes back as its decoded text."""
    parser = _Text()
    parser.feed(html.decode("utf-8", errors="replace"))
    parser.close()
    return " ".join(" ".join(parser.parts).split())


def excerpt(text: str, max_chars: int = EXCERPT_CHARS) -> str:
    """The whole text when it is `max_chars` (40,000) characters or less. Otherwise the first 20,000 characters,
    then 1,000-character windows on each side of every hit (a disclosure phrase of `PAGE_TEXT_PATTERN`, an e-mail
    address or `mailto:`, a disclosure-platform host name, or `security.txt`), merged where they touch and in page
    order, until `max_chars` characters in all (the " ... " between pieces included)."""
    if len(text) <= max_chars:
        return text
    hits = sorted(m.span() for pattern in (PAGE_TEXT_PATTERN, _EMAIL, _PLATFORM, _SECURITY_TXT)
                  for m in pattern.finditer(text))
    spans = [(0, OPENING_CHARS)]
    for start, end in hits:
        start, end = max(OPENING_CHARS, start - WINDOW_CHARS), min(len(text), end + WINDOW_CHARS)
        if end <= start:
            continue  # inside the opening
        if start <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(end, spans[-1][1]))
        else:
            spans.append((start, end))
    pieces: list[str] = []
    used = 0
    for start, end in spans:
        room = max_chars - used - (len(_GAP) if pieces else 0)
        if room <= 0:
            break
        piece = text[start:min(end, start + room)]
        used += len(piece) + (len(_GAP) if pieces else 0)
        pieces.append(piece)
    return _GAP.join(pieces)


def prefilter(text: str) -> bool:
    """False means a clear negative that is never sent to a model."""
    return bool(PAGE_TEXT_PATTERN.search(text) or _SECURITY_TXT.search(text))


def _parse(reply: str) -> Judgement:
    data = extract_json(reply)
    if not isinstance(data, dict) or not all(isinstance(data.get(k), bool) for k in _FIELDS):
        raise ValueError(f"reply lacks the three boolean fields: {reply[:200]!r}")
    return Judgement(**{k: data[k] for k in _FIELDS})


def judge_page(url: str, text: str, model: str, ask=ask_claude, meta: dict | None = None) -> Judgement:
    """One model's answers for one page. `url` names the page in an error only; `meta` (the requested URL, the final
    URL and the listed domain) goes to the model-call log, not to the model."""
    prompt = PROMPT.replace("{excerpt}", excerpt(text))
    last: Exception | None = None
    for _ in range(2):  # one retry, for a failed call or an unparseable reply
        check_stop()  # no call, and no retry, once a signal asked the run to stop
        try:
            return _parse(ask(prompt, model=model, system=SYSTEM, purpose=JUDGE_PURPOSE, meta=meta).text)
        except Exception as exc:
            last = exc
    raise JudgeError(f"judging {url} with {model} failed twice: {last}") from last


def adjudicate(url: str, text: str, ask=ask_claude, meta: dict | None = None) -> dict:
    """Both judge models; on disagreement over invites_reports or contact_method the tie-break model
    votes and the majority wins each field. `agreed` is true when no tie-break was needed for those two.
    For a final policy page (both true) a split on safe_harbor is also tie-broken, reusing the
    tie-break answer if it was already asked. A page that is not a policy page has safe_harbor false."""
    a, b = (judge_page(url, text, m, ask=ask, meta=meta) for m in JUDGE_MODELS)
    tie = None
    agreed = a.invites_reports == b.invites_reports and a.contact_method == b.contact_method
    if agreed:
        invites, contact = a.invites_reports, a.contact_method
    else:
        tie = judge_page(url, text, JUDGE_TIEBREAK, ask=ask, meta=meta)
        invites, contact = (sum(getattr(j, f) for j in (a, b, tie)) >= 2 for f in _FIELDS[:2])
    safe_agreed = a.safe_harbor == b.safe_harbor
    safe = False
    if invites and contact:
        if safe_agreed:
            safe = a.safe_harbor
        else:
            tie = tie or judge_page(url, text, JUDGE_TIEBREAK, ask=ask, meta=meta)
            safe = sum(j.safe_harbor for j in (a, b, tie)) >= 2
    return {"a": asdict(a), "b": asdict(b), "tiebreak": asdict(tie) if tie else None,
            "final": asdict(Judgement(invites, contact, safe)), "agreed": agreed,
            "safe_harbor_agreed": safe_agreed}


_TEXT_APP = re.compile(r"application/(?:json|xml|xhtml\+xml|[\w.+-]+\+(?:json|xml))", re.IGNORECASE)


def _mostly_text(body: bytes) -> bool:
    decoded = body.decode("utf-8", errors="replace")
    if not decoded:
        return True
    printable = sum(c.isprintable() or c in "\n\r\t" for c in decoded)
    return printable / len(decoded) >= 0.95 and decoded.count("\ufffd") / len(decoded) < 0.05


def extract_text(body: bytes, content_type: str | None, truncated: bool, runner=subprocess.run,
                 ) -> tuple[str | None, str | None]:
    """(text, None) for a page to examine, or (None, reason) for one that cannot be: "pdf_unreadable"
    (a PDF that was cut at the body cap, or pdftotext is missing, failed or found no text) or "non_text"; or
    (None, PDF_STOPPED) when a signal ended pdftotext after a stop was asked (`llm.stop_requested`); one that died by a
    signal on its own (a malformed PDF) is pdf_unreadable. pdftotext runs in its own session, so a terminal's Ctrl-C to
    the run does not reach it."""
    if body.startswith(b"%PDF"):
        if truncated:
            return None, "pdf_unreadable"
        try:
            proc = runner(["pdftotext", "-", "-"], input=body, capture_output=True, timeout=60, start_new_session=True)
        except (OSError, subprocess.SubprocessError):
            return None, "pdf_unreadable"
        if proc.returncode < 0 and stop_requested():  # fix round 4, N3: a crash on its own is pdf_unreadable
            return None, PDF_STOPPED
        text = " ".join(proc.stdout.decode("utf-8", errors="replace").split()) if proc.returncode == 0 else ""
        return (text, None) if text else (None, "pdf_unreadable")
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype.startswith("image/"):
        return None, IMAGE
    if b"\x00" in body or ctype.startswith(("audio/", "video/")):
        return None, "non_text"
    if ctype.startswith("application/") and not _TEXT_APP.fullmatch(ctype) and not _mostly_text(body):
        return None, "non_text"
    return visible_text(body), None


def _load_rows(out: Path) -> list[dict]:
    """Rows already written; a truncated or malformed line (a run killed mid-write) is ignored."""
    rows = []
    if out.exists():
        for line in out.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and "body_sha256" in row:
                rows.append(row)
    return rows


_VERDICT = ("a", "b", "tiebreak", "final", "agreed", "safe_harbor_agreed")


def page_meta(rec) -> dict:
    """What the model-call log keeps about the page a judge call is about: its requested URL, its final URL and the
    listed domain that collected it (purge finds a site's calls by the two URLs)."""
    return {"url": rec.url, "final_url": rec.final_url or rec.url, "domain": rec.domain}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def visible_chars(body: bytes, text: str) -> int:
    """How many visible characters a stored page has: for a PDF, the text pdftotext gave; otherwise the crawl's measure
    (`blocking.visible_text`: text outside title, script and style, whitespace collapsed)."""
    return len(text) if body.startswith(b"%PDF") else len(blocking.visible_text(body))


def is_unreadable(body: bytes, text: str, truncated: bool) -> bool:
    """A page nobody can judge from what was stored: fewer than 200 visible characters (a script shell, a challenge,
    an empty page), or cut at the size cap with nothing in the part that was kept that passes the prefilter."""
    return visible_chars(body, text) < MIN_VISIBLE_CHARS or (truncated and not prefilter(text))


def run_judging(store, out: Path, ask=ask_claude, pdf_runner=subprocess.run,
                optout: frozenset[str] = frozenset()) -> dict[str, int]:
    """Write one line to `out` for every stored page, keyed by (domain, url, body_sha256). Before any prefilter or
    model, a page is set aside, in this order, as `same_as_homepage` when it is a candidate or conventional-path page
    whose final URL is the final URL of its domain's homepage in that pass (same key); as `pdf_unreadable`, `non_text`
    or `image` (`extract_text`); as `meta_refresh` when it answered with a meta refresh (`crawl.refresh_target`) whose
    target was requested for the same listed domain in the same pass, or is the final URL of one of its answered pages
    (the record of that address stands in for it); as `unreadable` (`is_unreadable`); or as
    `catch_all` when it is a conventional-path page and another conventional-path page of the same listed domain and
    pass, with a different final URL, has the same visible text (the site answers 200 for any path). Every other page
    goes through the prefilter, then to the models.

    Each distinct visible text is judged once and its verdict is copied to every page with that text (or that body).
    A page already in `out` is skipped, and a text or body whose verdict is in `out` is not sent to the models again,
    so a re-run retries only pages that failed. After MAX_FAILED_IN_A_ROW pages in a row whose judging failed (a page
    that needs no model call neither counts nor resets the run), JudgingStopped is raised. Pages of a listed domain in
    `optout` (the set `cli.load_optout` reads), and pages whose final URL is on an opted-out host or registrable
    domain (whichever listed domain collected them), are never judged."""

    rows = _load_rows(out)
    done = {(r["domain"], r["url"], r["body_sha256"]) for r in rows}
    verdicts = {r["body_sha256"]: {k: r[k] for k in _VERDICT} for r in rows if r.get("final")}
    by_text = {r["text_sha256"]: {k: r[k] for k in _VERDICT} for r in rows if r.get("final") and r.get("text_sha256")}
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() and out.stat().st_size and not out.read_bytes().endswith(b"\n"):
        with out.open("a", encoding="utf-8") as fh:
            fh.write("\n")  # finish a line cut off by a killed run
    counts = {"judged": 0, "prefiltered_out": 0, "failed": 0, "already_done": 0, "pdf_unreadable": 0, "non_text": 0,
              UNREADABLE: 0, CATCH_ALL: 0, META_REFRESH: 0, "opted_out": 0, IMAGE: 0, SAME_AS_HOMEPAGE: 0}
    opted: dict[str, bool] = {}
    texts: dict[str, tuple[str | None, str | None]] = {}  # body sha -> extract_text's answer, once per run
    requested: dict[tuple[str, int], set[str]] = {}  # (domain, pass) -> keys of every URL requested or answered at
    homes: dict[tuple[str, int], str | None] = {}  # (domain, pass) -> the key of its homepage's final URL

    def is_homepage(rec) -> bool:
        if rec.purpose not in ("candidate", "probe"):
            return False
        if (rec.domain, rec.pass_no) not in homes:
            home = homepage_record(store.requests_for(rec.domain, rec.pass_no))
            homes[(rec.domain, rec.pass_no)] = url_key(home.final_url or home.url) if home is not None else None
        return homes[(rec.domain, rec.pass_no)] == url_key(rec.final_url or rec.url)

    def text_of(rec) -> tuple[str | None, str | None]:
        if rec.body_sha256 not in texts:
            texts[rec.body_sha256] = extract_text(store.get_blob(rec.body_sha256), rec.content_type, rec.truncated,
                                                  runner=pdf_runner)
        return texts[rec.body_sha256]

    def followed_refresh(rec) -> bool:
        target = refresh_target(rec, store)
        if target is None:
            return False
        if (rec.domain, rec.pass_no) not in requested:  # as the crawl's already-requested set (fix round 4, N2)
            recs = store.requests_for(rec.domain, rec.pass_no)
            requested[(rec.domain, rec.pass_no)] = {url_key(r.url) for r in recs} | {
                url_key(r.final_url) for r in recs
                if r.purpose in PAGE_PURPOSES and r.final_url and is_answer(r.status, r.blocked, r.error)}
        return url_key(target) in requested[(rec.domain, rec.pass_no)]

    pages = []
    for rec in store.records_by_purpose(JUDGED_PURPOSES):
        if rec.status != 200 or not is_answer(rec.status, rec.blocked, rec.error) or not rec.body_sha256:
            continue  # only a page that was read
        if rec.domain not in opted:
            opted[rec.domain] = is_opted_out(store, rec.domain, optout)
        if opted[rec.domain] or url_matches(rec.final_url or rec.url, optout):  # a page on an opted-out site too
            counts["opted_out"] += 1
            continue
        pages.append(rec)

    finals: dict[tuple[str, int, str], set[str]] = defaultdict(set)  # (domain, pass, text sha) -> final URL keys
    for rec in pages:
        if rec.purpose == "probe" and (found := text_of(rec))[0] is not None:
            finals[(rec.domain, rec.pass_no, _sha(found[0]))].add(url_key(rec.final_url or rec.url))
    catch_all = {key for key, urls in finals.items() if len(urls) >= 2}

    failed: set[str] = set()  # text shas whose judging failed in this run
    failed_in_a_row = 0
    for rec in pages:
        check_stop()  # a signal stopped the run: the page in progress was left unwritten, and nothing more starts
        key = (rec.domain, rec.url, rec.body_sha256)
        if key in done:
            counts["already_done"] += 1
            continue
        done.add(key)
        row = {"domain": rec.domain, "sector": rec.sector, "url": rec.url, "body_sha256": rec.body_sha256,
               "text_sha256": None, "prefilter": None, "a": None, "b": None, "tiebreak": None, "final": None,
               "agreed": None, "safe_harbor_agreed": None, "skipped": None}
        text, skipped = (None, SAME_AS_HOMEPAGE) if is_homepage(rec) else text_of(rec)
        if skipped == PDF_STOPPED:  # no verdict on a page nobody read: asked again on a later run
            counts["failed"] += 1
            continue
        if not skipped:
            row["text_sha256"] = text_sha = _sha(text)
            if followed_refresh(rec):
                skipped = META_REFRESH
            elif is_unreadable(store.get_blob(rec.body_sha256), text, rec.truncated):
                skipped = UNREADABLE
            elif rec.purpose == "probe" and (rec.domain, rec.pass_no, text_sha) in catch_all:
                skipped = CATCH_ALL
        if skipped:
            row["skipped"] = skipped
            counts[skipped] += 1
        else:
            row["prefilter"] = prefilter(text)
            known = verdicts.get(rec.body_sha256) or by_text.get(text_sha)
            if not row["prefilter"]:
                counts["prefiltered_out"] += 1
            elif known:
                row.update(known)
                counts["judged"] += 1
            elif text_sha in failed:
                counts["failed"] += 1
                continue
            else:
                try:
                    row.update(adjudicate(rec.url, text, ask=ask, meta=page_meta(rec)))
                except JudgeError as exc:
                    print(f"noroshi: {exc}")
                    failed.add(text_sha)
                    counts["failed"] += 1
                    failed_in_a_row += 1
                    if failed_in_a_row >= MAX_FAILED_IN_A_ROW:
                        raise JudgingStopped(counts) from exc
                    continue
                failed_in_a_row = 0
                verdicts[rec.body_sha256] = by_text[text_sha] = {k: row[k] for k in _VERDICT}
                counts["judged"] += 1
        check_stop()  # once a signal asked the run to stop, no row is written: the page is asked again on a later run
        with out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    check_stop()  # a signal during the last page still ends the run as stopped
    return counts
