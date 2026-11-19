"""Is a measured homepage the listed organization's working website?

The crawl no longer decides this by name words (post-pilot X8). After the crawl, two models read the stored homepage
of the latest pass that measured a domain, each without seeing the other; when they differ a third decides. A domain
whose own site is not the organization's (another business, a parked domain, an error or bot-check page) or cannot
be told from the page is reported `mismatch` by derive. Domains only in the federal table are exempt: the .gov
registry controls those names. Every model call is logged by `llm.ask_claude`, with the domain in `meta`.
"""
import hashlib
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import pandas as pd

from noroshi.blocking import VisibleText, visible_text
from noroshi.config import IDENTITY_MODELS, IDENTITY_TIEBREAK
from noroshi.crawl import normalize_domain
from noroshi.llm import CallsStopped, ask_claude, check_stop, extract_json, stop_requested
from noroshi.optout import is_opted_out
from noroshi.store import FetchRecord, Store, measured_homepage

IDENTITY_PURPOSE = "identity_check"  # how every rater and tie-break call is labelled in the model-call log
SYSTEM = "You check web pages for a research study. Answer only with a JSON object."
SITES = ("own", "other", "unclear")
NOT_OWN = ("other", "unclear")  # a decision that makes a measured domain a mismatch
EXEMPT = "exempt"  # the identity value of a domain listed only in the federal table
HEAD_CHARS = 2_000  # the models see the first 2,000 visible characters ...
TAIL_CHARS = 800  # ... and the last 800 (those not already in the head)
META_CHARS = 300  # a title, site name or description is cut to this length
MAX_NAMES = 5  # hospitals named per domain, largest first
MAX_FAILED_IN_A_ROW = 5  # domains in a row, in completion order, whose decision failed: a usage limit or an outage
DEFAULT_WORKERS, MAX_WORKERS = 4, 8
UNNAMED = "(no name on the list)"

PROMPT = """You are checking one web page for a research study. The study's list says the website of the organization below is at the listed domain. Websites change hands, so the page that loaded may belong to someone else.

Organization on the list: {names}
{place_line}
Listed domain: {domain}
Host the homepage ended on: {final_host}

Page title: {title}
Site name and description from the page's meta tags: {meta}
Start of the page text:
<<<
{head}
>>>
End of the page text:
<<<
{tail}
>>>

Whose website is this page? Choose one.
- "own": the listed organization's own working website, or the working website of the organization that now owns or operates it or answers for its products (a parent company, an acquirer, a successor, a health system). A site in another language still counts when it is the same organization. A page that shows little text of its own, for example one that needs scripts to display or a sign-in page, still counts when its title or meta tags name that organization or its product.
- "other": anything else: a different or unrelated organization or business, a parked or for-sale domain, a placeholder or hosting default page, an error page, a bot check or access-denied page, or a site reusing the name for unrelated content.
- "unclear": nothing on the page (its title, its meta tags, its text) shows whose website it is.

Reply with one JSON object and nothing else. It has exactly two keys: "site", whose value is one of the three words above, and "reason", one short sentence naming what on the page decided it."""
_SLOT = re.compile(r"\{(names|place_line|domain|final_host|title|meta|head|tail)\}")


class IdentityError(RuntimeError):
    pass


class IdentityStopped(RuntimeError):
    """The run stopped after MAX_FAILED_IN_A_ROW domains in a row failed; `counts` says what was done. Every decision
    written so far stays, and a failed domain is not written, so a re-run resumes."""

    def __init__(self, counts: dict[str, int]):
        super().__init__(f"the identity check stopped after {MAX_FAILED_IN_A_ROW} domains in a row failed (a usage "
                         "limit or an outage?)")
        self.counts = counts


@dataclass(frozen=True)
class Listing:
    """What the study's list says about a domain: the organization names, and for hospitals where they are."""
    names: str
    place_line: str = ""


@dataclass(frozen=True)
class PageView:
    """What the models see of a measured homepage, and the key of a decision about it."""
    pass_no: int
    url: str
    final_url: str
    final_host: str
    title: str
    meta: str
    head: str
    tail: str

    @property
    def key(self) -> str:
        """One decision per listed domain and homepage text: a page that shows the models the same thing is not
        asked about again."""
        seen = [self.final_host, self.title, self.meta, self.head, self.tail]
        return hashlib.sha256(json.dumps(seen, ensure_ascii=False).encode("utf-8")).hexdigest()


class _Page(VisibleText):
    """Visible text (`blocking.VisibleText`), the document's title (the first `<title>` outside an inline `<svg>` or
    `<math>`: icon titles are not the page's), and the first `og:site_name` and `description` meta tags."""

    def __init__(self):
        super().__init__()
        self.tags: dict[str, str] = {}
        self.doc_title: list[str] = []
        self._graphics = 0  # depth of inline svg or math elements
        self._title_state = "before"  # before, in, after the document's title

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        if tag in ("svg", "math"):
            self._graphics += 1
        elif tag == "title" and not self._graphics and self._title_state == "before":
            self._title_state = "in"
        elif tag == "meta":
            a = {k.lower(): (v or "") for k, v in attrs}
            key = (a.get("property") or a.get("name") or "").strip().lower()
            if key in ("og:site_name", "description") and key not in self.tags:
                self.tags[key] = a.get("content", "")

    def handle_endtag(self, tag):
        super().handle_endtag(tag)
        if tag in ("svg", "math") and self._graphics:
            self._graphics -= 1
        elif tag == "title" and self._title_state == "in":
            self._title_state = "after"

    def handle_data(self, data):
        super().handle_data(data)
        if self._title_state == "in":
            self.doc_title.append(data)


def _short(text: str) -> str:
    return " ".join(text.split())[:META_CHARS]


def page_view(store: Store, home: FetchRecord) -> PageView:
    """The view of a homepage record (`store.measured_homepage`): its final host, title, site name and description,
    and the first HEAD_CHARS and last TAIL_CHARS characters of its visible text (`blocking.visible_text`: outside
    title, script and style). The tail holds only characters not already in the head."""
    body = store.get_blob(home.body_sha256) if home.body_sha256 else b""
    page = _Page()
    page.feed(body.decode("utf-8", errors="replace"))
    page.close()
    text = visible_text(body)
    final_url = home.final_url or home.url
    meta = f"{_short(page.tags.get('og:site_name', '')) or '(none)'} | {_short(page.tags.get('description', '')) or '(none)'}"
    return PageView(pass_no=home.pass_no, url=home.url, final_url=final_url,
                    final_host=(urlsplit(final_url).hostname or "").lower(),
                    title=_short(" ".join(page.doc_title)) or "(none)", meta=meta, head=text[:HEAD_CHARS],
                    tail=text[max(HEAD_CHARS, len(text) - TAIL_CHARS):])


def build_prompt(listing: Listing, domain: str, view: PageView) -> str:
    values = {"names": listing.names or UNNAMED, "place_line": listing.place_line, "domain": domain,
              "final_host": view.final_host, "title": view.title, "meta": view.meta, "head": view.head,
              "tail": view.tail}
    return _SLOT.sub(lambda m: values[m.group(1)], PROMPT)  # one pass: page text is never read as a slot


def _read(reply: str) -> dict:
    data = extract_json(reply)
    if not isinstance(data, dict) or data.get("site") not in SITES or not isinstance(data.get("reason"), str):
        raise ValueError(f"reply lacks a site of {SITES} and a reason: {str(data)[:200]!r}")
    return data


def _ask(prompt: str, model: str, ask, meta: dict, should_stop=check_stop) -> dict:
    """One rater's answer, asked once more when the call fails or the reply cannot be read. A rater that replied twice
    and neither reply could be read as one of the three words is recorded `unclear` with `reply_unreadable` (a model
    that will not answer about a page must not hold its domain undecided for ever); a failed call (an error, a
    timeout, a usage limit) is a failure, asked again on a later run."""
    last: Exception | None = None
    unreadable = 0
    for _ in range(2):
        should_stop()  # no retry once the run was asked to stop
        try:
            reply = ask(prompt, model=model, system=SYSTEM, purpose=IDENTITY_PURPOSE, meta=meta).text
        except Exception as exc:  # the call failed
            last = exc
            continue
        try:
            data = _read(reply)
        except (ValueError, TypeError) as exc:  # a reply, but not one of the three words
            last = exc
            unreadable += 1
            continue
        return {"model": model, "site": data["site"], "reason": data["reason"]}
    if unreadable == 2:
        return {"model": model, "site": "unclear", "reason": "", "reply_unreadable": True}
    raise IdentityError(f"asking {model} about {meta.get('domain')} failed twice: {last}") from last


def decide(prompt: str, ask, meta: dict, should_stop=check_stop) -> dict:
    """The two raters, one after the other, each without the other's answer; when they differ the tie-break model
    decides. `agreed` says whether the raters agreed. `should_stop()` raises CallsStopped before any call once the run
    was asked to stop: a domain in progress is then left undecided."""
    a, b = (_ask(prompt, model, ask, meta, should_stop) for model in IDENTITY_MODELS)
    tie = None if a["site"] == b["site"] else _ask(prompt, IDENTITY_TIEBREAK, ask, meta, should_stop)
    return {"a": a, "b": b, "tiebreak": tie, "site": (tie or a)["site"], "agreed": tie is None}


# ---- the list's names


def _beds(value) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return -1.0
    return n if n > 0 else -1.0  # unknown (HIFLD writes -999) sorts last


def hospital_listing(hospitals: list[dict]) -> Listing:
    """Up to MAX_NAMES hospital names on a domain, largest first (by beds; unknown last), and how many more there
    are; their cities and states, in the same order, without repeats, on the place line."""
    ranked = sorted(hospitals, key=lambda h: (-_beds(h.get("beds")), str(h.get("name"))))
    shown = ranked[:MAX_NAMES]
    names = "; ".join(str(h["name"]) for h in shown)
    if len(ranked) > MAX_NAMES:
        names += f"; and {len(ranked) - MAX_NAMES} more hospitals at this domain"
    places = list(dict.fromkeys(", ".join(x for x in (str(h.get("city") or ""), str(h.get("state") or "")) if x)
                                for h in shown))
    places = [p for p in places if p]
    place_line = f"{'Location' if len(places) == 1 else 'Locations'}: {'; '.join(places)}" if places else ""
    return Listing(names, place_line)


def listings(frames: Path) -> dict[str, Listing]:
    """Listings for every domain of the frames: kept hospitals from `hospitals.csv` with their city from the HIFLD
    download (`hifld_raw.json`), and company names from the resolution passes (`resolve.reconcile`) that resolved to
    the domain, from every company table. A domain in several tables gets the names of all of them. A missing file
    leaves its names out."""
    from noroshi.resolve import reconcile

    frames = Path(frames)
    hospital: dict[str, list[dict]] = {}
    if (frames / "hospitals.csv").exists():
        rows = pd.read_csv(frames / "hospitals.csv", dtype=str, keep_default_na=False)
        cities = {}
        if (frames / "hifld_raw.json").exists():
            raw = json.loads((frames / "hifld_raw.json").read_text(encoding="utf-8"))
            cities = {str(f["attributes"].get("ID")): f["attributes"].get("CITY") or "" for f in raw.get("features", [])}
        for row in rows[rows.exclusion.str.strip() == ""].to_dict("records"):
            hospital.setdefault(normalize_domain(row["domain"]), []).append({**row, "city": cities.get(row["hifld_id"], "")})
    company: dict[str, list[str]] = {}
    passes = frames / "resolution_passes"
    for sector in ("device", "icsma", "ehr"):
        a, b, s = (passes / f"{sector}_{x}.jsonl" for x in ("memory_a", "memory_b", "search"))
        if not (a.exists() and b.exists()):
            continue
        resolved = reconcile(a, b, s if s.exists() else None)
        for name, domain in zip(resolved.name, resolved.domain):
            if domain:
                company.setdefault(normalize_domain(domain), []).append(name)
    out = {}
    for domain in hospital.keys() | company.keys():
        h = hospital_listing(hospital[domain]) if domain in hospital else Listing("")
        names = [h.names] if h.names else []
        names += sorted(dict.fromkeys(company.get(domain, [])))
        out[domain] = Listing("; ".join(names), h.place_line)
    return out


# ---- the run


def sectors_by_domain(domains: pd.DataFrame) -> dict[str, list[str]]:
    """Each listed domain (in its normalized form, in frame order) and the sector tables it is in."""
    out: dict[str, set[str]] = {}
    for sector, name in zip(domains["sector"], domains["domain"]):
        out.setdefault(normalize_domain(name), set()).add(sector)
    return {d: sorted(s) for d, s in out.items()}


def is_exempt(sectors) -> bool:
    return set(sectors) == {"federal"}


def index_decisions(rows) -> dict[tuple[str, str], dict]:
    """(domain, page key) -> the decision row for that page view (the last one written). The one definition of which
    decision counts: derive looks a domain's measured homepage up here, and the agreement table reads the row derive
    recorded (`identity_page_sha256`)."""
    out: dict[tuple[str, str], dict] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("domain") and row.get("page_sha256") and row.get("site") in SITES:
            out[(row["domain"], row["page_sha256"])] = row
    return out


def unnamed(store: Store, domains: pd.DataFrame, names: dict[str, Listing],
            optout: frozenset[str] = frozenset()) -> list[tuple[str, list[str]]]:
    """(domain, sectors) of every domain `run_identity` would ask about that has no organization name in `names`: a
    prompt without one cannot be answered fairly, so the command refuses to start."""
    out = []
    for domain, sectors in sectors_by_domain(domains).items():
        if (not (names.get(domain) and names[domain].names) and not is_exempt(sectors)
                and any(store.done_status(domain, p) == "measured" for p in (1, 2))
                and not is_opted_out(store, domain, optout)):
            out.append((domain, sectors))
    return out


def name_sources(frames: Path, sector: str) -> list[Path]:
    """The files `listings` reads the names of a sector from."""
    if sector == "hospital":
        return [Path(frames) / "hospitals.csv"]
    return [Path(frames) / "resolution_passes" / f"{sector}_{x}.jsonl" for x in ("memory_a", "memory_b")]


def load_decisions(path: Path | None) -> dict[tuple[str, str], dict]:
    """`index_decisions` of a result file. A line cut off by a killed run is ignored."""
    if path is None or not Path(path).exists():
        return {}
    rows = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return index_decisions(rows)


def run_identity(store: Store, domains: pd.DataFrame, names: dict[str, Listing], out: Path, ask=ask_claude,
                 optout: frozenset[str] = frozenset(), workers: int = DEFAULT_WORKERS) -> dict[str, int]:
    """Decide, for every listed domain of `domains` (the sector tables) that a pass measured and that is not exempt or
    opted out, whether the homepage of its latest measured pass (`store.measured_homepage`) is the organization's own
    working site. A domain in several tables is asked about once, with the names of all of them (`names`, from
    `listings`). One line per decision is appended to `out`; a domain with a decision for the same page view is not
    asked again, so a re-run resumes. Domains are decided by a pool of `workers` threads (1 to MAX_WORKERS), the two
    raters of a domain one after the other in its worker; a row is written, whole and under a lock, when its decision
    is complete. After MAX_FAILED_IN_A_ROW failed domains in a row, in the order domains finish, no new domain is
    started, calls already running finish (and their decisions are written), and IdentityStopped is raised. When the
    run is asked to stop (a signal under `llm.stop_on_signals`, or a KeyboardInterrupt in this thread), no new call
    starts, not even the next rater, a retry or a tie-break of a domain in progress (left undecided); the calls in
    flight finish, are logged and complete decisions are written; once every worker has ended, CallsStopped (or the
    KeyboardInterrupt) is raised. The workers are waited for without `Thread.join`, which an interrupt can cut short.
    A row that cannot be written stops the run and its error is raised."""
    if not 1 <= workers <= MAX_WORKERS:
        raise ValueError(f"--workers must be 1 to {MAX_WORKERS}, not {workers}")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() and out.stat().st_size and not out.read_bytes().endswith(b"\n"):
        with out.open("a", encoding="utf-8") as fh:
            fh.write("\n")  # finish a line cut off by a killed run
    done = set(load_decisions(out))
    counts = {"decided": 0, "failed": 0, "already_done": 0, "exempt": 0, "opted_out": 0, "not_measured": 0,
              "unreadable_replies": 0}
    todo = []
    for domain, sectors in sectors_by_domain(domains).items():
        passes = [p for p in (1, 2) if store.done_status(domain, p) == "measured"]
        if not passes:
            counts["not_measured"] += 1
        elif is_exempt(sectors):
            counts["exempt"] += 1
        elif is_opted_out(store, domain, optout):
            counts["opted_out"] += 1
        else:
            home = measured_homepage({p: store.requests_for(domain, p) for p in (1, 2)}, passes)
            view = page_view(store, home)  # read here: the store is used from this thread only
            if (domain, view.key) in done:
                counts["already_done"] += 1
            else:
                todo.append((domain, sectors, view, build_prompt(names.get(domain) or Listing(""), domain, view)))

    lock = threading.Lock()  # guards the queue, the counts, the file and the count of running workers
    ended = threading.Condition(lock)
    queue = iter(todo)
    state = {"in_a_row": 0, "stopped": False, "interrupted": False, "error": None}

    def should_stop() -> None:
        if state["interrupted"]:
            raise CallsStopped("the run was interrupted: no new model call starts")
        check_stop()

    def worker() -> None:
        try:
            work()
        finally:
            with lock:
                ended.notify_all()

    def work() -> None:
        while True:
            with lock:
                stop = state["stopped"] or state["interrupted"] or stop_requested()
                item = None if stop else next(queue, None)
            if item is None:
                return  # nothing left, or stopped: a domain not yet started is never started
            domain, sectors, view, prompt = item
            try:
                decision = decide(prompt, ask, {"domain": domain, "url": view.url, "final_url": view.final_url},
                                  should_stop)
            except IdentityError as exc:
                with lock:  # failures in a row, in the order the domains finish
                    print(f"noroshi: {exc}")
                    counts["failed"] += 1
                    state["in_a_row"] += 1
                    if state["in_a_row"] >= MAX_FAILED_IN_A_ROW:
                        state["stopped"] = True
                continue
            except CallsStopped:
                return  # asked to stop: this domain is left undecided, and is asked again on a later run
            except BaseException as exc:  # anything else stops the run and is raised in the caller's thread
                with lock:
                    state["stopped"], state["error"] = True, state["error"] or exc
                return
            row = {"domain": domain, "sectors": sectors, "pass_no": view.pass_no, "homepage_url": view.url,
                   "final_url": view.final_url, "page_sha256": view.key, **decision}
            try:
                with lock:  # one whole line per decision, written when the decision is complete
                    with out.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    counts["decided"] += 1
                    counts["unreadable_replies"] += sum(bool(x and x.get("reply_unreadable"))
                                                        for x in (decision["a"], decision["b"], decision["tiebreak"]))
                    state["in_a_row"] = 0
            except BaseException as exc:  # a row that cannot be written stops the run, in the caller's thread
                with lock:
                    state["stopped"], state["error"] = True, state["error"] or exc
                return

    # not daemon threads; this thread waits for them by polling `is_alive` on a condition they signal when they end,
    # never with Thread.join, which an interrupt can cut short and so leave a worker running unseen
    pool = [threading.Thread(target=worker, name=f"identity-{i}") for i in range(min(workers, len(todo)))]
    interrupted: BaseException | None = None
    for t in pool:
        try:
            if state["interrupted"]:
                break
            t.start()
        except KeyboardInterrupt as exc:  # a thread that starts anyway finds the run interrupted and asks nothing
            with lock:
                state["interrupted"], state["stopped"] = True, True
            interrupted = interrupted or exc
    while True:
        try:
            with lock:
                while any(t.is_alive() for t in pool):
                    ended.wait(0.1)
            break
        except KeyboardInterrupt as exc:  # no new call starts; the calls in flight finish
            with lock:
                state["interrupted"], state["stopped"] = True, True
            interrupted = interrupted or exc
    if state["error"] is not None:
        raise state["error"]
    if interrupted is not None:
        raise interrupted
    if stop_requested():
        raise CallsStopped(f"the identity check was stopped by a signal: the calls in flight finished; "
                           f"{counts['decided']} decisions were written in this run")
    if state["stopped"]:
        raise IdentityStopped(counts)
    return counts
