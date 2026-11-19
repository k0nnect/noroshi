"""Post-pilot Y3: is the measured homepage the listed organization's working site? Two models read it; a third breaks a
tie. The models are faked here, as in the judge tests: no model is ever called."""
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import synth
from noroshi import identity
from noroshi.identity import (IDENTITY_PURPOSE, PROMPT, Listing, build_prompt, hospital_listing, page_view,
                              run_identity)
from noroshi.outcomes import derive_domains
from noroshi.store import Store

PILOT = Path(__file__).parent / "fixtures" / "pilot"
S, H, O = "claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5"
HOME = synth.HOME


def said(site, reason="the page names the hospital"):
    return json.dumps({"site": site, "reason": reason})


class FakeAsk:
    """Replies by model name: a function of the prompt, or a fixed reply; records every call."""

    def __init__(self, by_model, sleep=0.0):
        self.by_model, self.sleep = by_model, sleep
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, prompt, model, system, purpose, **kw):
        with self.lock:
            self.calls.append((prompt, model, system, purpose, kw.get("meta")))
        if self.sleep:
            time.sleep(self.sleep)
        reply = self.by_model[model]
        reply = reply(prompt) if callable(reply) else reply
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(text=reply)


def frame(*rows):
    """A domain table: (sector, domain) pairs."""
    return pd.DataFrame([{"sector": sector, "domain": d, "start_url": f"https://{d}/", "n_units": "1", "beds_sum": "",
                          "name_tokens": ""} for sector, d in rows])


def measured(store, d, home=HOME, sector="hospital", pass_no=1, final=None):
    synth.rec(store, d, "homepage", home, ctype="text/html", url=f"https://{d}/", final=final, pass_no=pass_no,
              sector=sector)
    store.mark_done(d, pass_no, "measured")


def rows(path):
    """The decision rows of a result file; a line cut off by a killed run is left out, as the reader does."""
    return [json.loads(x) for x in Path(path).read_text().splitlines() if x.strip().endswith("}")]


# ---- the prompt

PROMPT_AS_WRITTEN = """You are checking one web page for a research study. The study's list says the website of the organization below is at the listed domain. Websites change hands, so the page that loaded may belong to someone else.

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


def test_the_prompt_is_as_written():
    assert PROMPT == PROMPT_AS_WRITTEN


def test_the_models_see_names_place_domain_host_title_meta_head_and_tail(tmp_path):
    body = (PILOT / "getqardio_home.html").read_bytes()
    with Store(tmp_path / "s.sqlite") as store:
        measured(store, "getqardio.com", body, sector="icsma", final="https://www.getqardio.com/")
        view = page_view(store, store.requests_for("getqardio.com", 1)[0])
    prompt = build_prompt(Listing("Qardio"), "getqardio.com", view)
    assert "Organization on the list: Qardio\n\nListed domain: getqardio.com\n" in prompt  # no place line for a company
    assert "Host the homepage ended on: www.getqardio.com\n" in prompt
    assert "Page title: Get Qardio - Portal Kesehatan dan Informasi Olahraga\n" in prompt
    assert ("Site name and description from the page's meta tags: www.getqardio.com | Get Qardio menyajikan artikel "
            "kesehatan") in prompt
    text = identity.visible_text(body)  # 914 characters: all of it in the head, an empty tail
    assert f"<<<\n{text}\n>>>\nEnd of the page text:\n<<<\n\n>>>" in prompt
    assert prompt.endswith(PROMPT.split("{tail}\n>>>", 1)[1])


def test_head_and_tail_are_the_first_2000_and_the_last_800_visible_characters(tmp_path):
    words = " ".join(f"w{i:05d}" for i in range(1000))  # 6,999 characters
    with Store(tmp_path / "s.sqlite") as store:
        measured(store, "a.org", f"<title>T</title><p>{words}</p>".encode())
        view = page_view(store, store.requests_for("a.org", 1)[0])
        assert (view.head, view.tail) == (words[:2000], words[-800:])
        measured(store, "b.org", b"<p>" + b"short page " * 100 + b"</p>")  # 1,099 characters: all in the head
        short = page_view(store, store.requests_for("b.org", 1)[0])
        assert len(short.head) == 1099 and short.tail == ""


def test_a_hospital_domain_shows_its_five_largest_hospitals_with_city_and_state():
    rows_ = [{"name": f"H{i}", "city": f"C{i}", "state": "OH", "beds": str(b)}
             for i, b in enumerate([10, 300, "", 50, 900, 20, 75])]
    listing = hospital_listing(rows_)
    assert listing.names == "H4; H1; H6; H3; H5; and 2 more hospitals at this domain"
    assert listing.place_line == "Locations: C4, OH; C1, OH; C6, OH; C3, OH; C5, OH"
    one = hospital_listing([{"name": "GREENE MEMORIAL HOSPITAL", "city": "XENIA", "state": "OH", "beds": "41"}])
    assert (one.names, one.place_line) == ("GREENE MEMORIAL HOSPITAL", "Location: XENIA, OH")


def test_listings_from_the_frames_join_hospital_names_cities_and_company_names(tmp_path):
    frames = tmp_path / "frames"
    (frames / "resolution_passes").mkdir(parents=True)
    pd.DataFrame([{"hifld_id": "01", "name": "MERCY FAIRFIELD", "state": "OH", "beds": "300", "domain": "e-mercy.com",
                   "exclusion": ""},
                  {"hifld_id": "02", "name": "MERCY ANDERSON", "state": "OH", "beds": "263", "domain": "e-mercy.com",
                   "exclusion": ""},
                  {"hifld_id": "03", "name": "CLOSED ONE", "state": "OH", "beds": "999", "domain": "e-mercy.com",
                   "exclusion": "closed"}]).to_csv(frames / "hospitals.csv", index=False)
    (frames / "hifld_raw.json").write_text(json.dumps({"features": [
        {"attributes": {"ID": "01", "CITY": "FAIRFIELD"}}, {"attributes": {"ID": "02", "CITY": "CINCINNATI"}}]}))
    for sector, names in (("device", [("change", "Change Healthcare", "changehealthcare.com")]),
                          ("icsma", [("change", "Change Healthcare Inc", "changehealthcare.com"),
                                     ("qardio", "Qardio", "getqardio.com")])):
        for which in ("memory_a", "memory_b"):
            (frames / "resolution_passes" / f"{sector}_{which}.jsonl").write_text("".join(
                json.dumps({"key": k, "name": n, "model": "m", "domain": d}) + "\n" for k, n, d in names))
    got = identity.listings(frames)
    assert got["e-mercy.com"] == Listing("MERCY FAIRFIELD; MERCY ANDERSON", "Locations: FAIRFIELD, OH; CINCINNATI, OH")
    assert got["changehealthcare.com"] == Listing("Change Healthcare; Change Healthcare Inc")
    assert got["getqardio.com"] == Listing("Qardio")


# ---- deciding

def test_two_raters_that_agree_decide_and_each_call_is_logged_with_meta(tmp_path):
    out = tmp_path / "identity.jsonl"
    with Store(tmp_path / "s.sqlite") as store:
        measured(store, "a.org", final="https://www.a.org/")
        ask = FakeAsk({S: said("own"), H: said("own")})
        counts = run_identity(store, frame(("hospital", "a.org")), {"a.org": Listing("A HOSPITAL")}, out, ask=ask)
    assert counts["decided"] == 1 and sorted(c[1] for c in ask.calls) == [H, S]
    row, = rows(out)
    assert (row["domain"], row["site"], row["agreed"], row["tiebreak"]) == ("a.org", "own", True, None)
    assert (row["a"]["model"], row["b"]["model"]) == (S, H)
    assert all(c[3] == IDENTITY_PURPOSE and c[4] == {"domain": "a.org", "url": "https://a.org/",
                                                     "final_url": "https://www.a.org/"} for c in ask.calls)
    assert "A HOSPITAL" in ask.calls[0][0]


def test_a_disagreement_goes_to_the_tie_break(tmp_path):
    out = tmp_path / "identity.jsonl"
    with Store(tmp_path / "s.sqlite") as store:
        measured(store, "a.org")
        ask = FakeAsk({S: said("own"), H: said("other"), O: said("unclear", "only a script shell")})
        run_identity(store, frame(("device", "a.org")), {}, out, ask=ask)
    row, = rows(out)
    assert (row["site"], row["agreed"], row["tiebreak"]["model"], row["tiebreak"]["site"]) == ("unclear", False, O,
                                                                                               "unclear")
    assert [c[1] for c in ask.calls][-1] == O and len(ask.calls) == 3


def test_a_reply_that_cannot_be_read_once_is_asked_again_and_the_readable_answer_counts(tmp_path):
    out = tmp_path / "identity.jsonl"
    with Store(tmp_path / "s.sqlite") as store:
        measured(store, "a.org")
        replies = iter(["not json", said("own")])
        ask = FakeAsk({S: lambda p: next(replies), H: said("own")})
        counts = run_identity(store, frame(("device", "a.org")), {}, out, ask=ask)
    row, = rows(out)
    assert (counts["decided"], counts["unreadable_replies"], row["a"]["site"]) == (1, 0, "own")
    assert "reply_unreadable" not in row["a"]


def test_a_rater_that_twice_gives_no_readable_answer_is_recorded_unclear(tmp_path):
    """Fix round 1, M3: otherwise a page a model will not answer about blocks the main analysis for ever."""
    out = tmp_path / "identity.jsonl"
    with Store(tmp_path / "s.sqlite") as store:
        measured(store, "b.org")
        ask = FakeAsk({S: '{"site": "maybe", "reason": "x"}', H: said("own"), O: "I cannot tell."})
        counts = run_identity(store, frame(("device", "b.org")), {}, out, ask=ask)
    row, = rows(out)
    assert (row["a"]["site"], row["a"]["reply_unreadable"], row["b"]["site"]) == ("unclear", True, "own")
    assert (row["tiebreak"]["site"], row["tiebreak"]["reply_unreadable"], row["site"]) == ("unclear", True, "unclear")
    assert counts["unreadable_replies"] == 2 and counts["decided"] == 1 and len(ask.calls) == 2 + 1 + 2


def test_a_failed_call_is_not_recorded_but_asked_again_on_a_later_run(tmp_path):
    out = tmp_path / "identity.jsonl"
    with Store(tmp_path / "s.sqlite") as store:
        measured(store, "c.org")
        replies = iter(["not json", RuntimeError("timed out")])  # an unreadable reply, then a failed call
        counts = run_identity(store, frame(("device", "c.org")), {}, out,
                              ask=FakeAsk({S: lambda p: next(replies), H: said("own")}))
        assert counts["failed"] == 1 and counts["decided"] == 0 and not out.exists()
        counts = run_identity(store, frame(("device", "c.org")), {}, out,
                              ask=FakeAsk({S: RuntimeError("usage limit"), H: said("own")}))
        assert counts["failed"] == 1 and not out.exists()


def test_the_agreement_note_counts_answers_recorded_unclear_for_an_unreadable_reply(tmp_path):
    from noroshi.analysis import tables

    d, h = _derived_rows(tmp_path)
    rater = lambda site, **kw: {"model": "m", "site": site, "reason": "", **kw}
    decided = [{"domain": "a.org", "page_sha256": "k", "a": rater("unclear", reply_unreadable=True),
                "b": rater("own"), "site": "own"}]
    n = tables.build_all(d, h, tmp_path / "t", identity=decided)
    note = next(r for r in __import__("csv").DictReader((tmp_path / "t" / "tableS2_agreement.csv").read_text()
                                                         .splitlines()) if r["check"] == "identity")["note"]
    assert "1 rater answer was recorded as unclear because its reply could not be read twice" in note
    assert n["agreement.identity.site.unreadable_replies"] == 1


def _derived_rows(tmp_path):
    from noroshi.outcomes import COLUMNS, derive_hospitals

    row = dict.fromkeys(COLUMNS)
    row.update(sector="device", domain="a.org", status="measured", identity="own", identity_page_sha256="k",
               sectxt_class="absent", policy_page="no", main=False, strict=False, listing=False, broad=False)
    d = pd.DataFrame([row], columns=COLUMNS)
    h = derive_hospitals(pd.DataFrame(columns=["hifld_id", "name", "state", "type", "owner", "beds", "rural",
                                               "domain", "exclusion"]), d)
    return d, h


def test_a_federal_only_domain_is_exempt_and_never_asked_and_two_tables_are_asked_once(tmp_path):
    out = tmp_path / "identity.jsonl"
    with Store(tmp_path / "s.sqlite") as store:
        for d in ("fed.gov", "both.gov", "dual.com"):
            measured(store, d, sector="federal")
        ask = FakeAsk({S: said("own"), H: said("own")})
        counts = run_identity(store, frame(("federal", "fed.gov"), ("federal", "both.gov"), ("device", "both.gov"),
                                           ("device", "dual.com"), ("icsma", "dual.com")),
                              {"dual.com": Listing("Dual Inc; Dual Medical")}, out, ask=ask)
    assert counts["exempt"] == 1 and counts["decided"] == 2
    assert sorted(r["domain"] for r in rows(out)) == ["both.gov", "dual.com"]
    assert not any("fed.gov" in c[0] for c in ask.calls) and len(ask.calls) == 4
    assert {tuple(r["sectors"]) for r in rows(out)} == {("device", "federal"), ("device", "icsma")}


def test_a_domain_whose_homepage_text_is_unchanged_is_not_asked_again(tmp_path):
    out = tmp_path / "identity.jsonl"
    with Store(tmp_path / "s.sqlite") as store:
        measured(store, "a.org")
        run_identity(store, frame(("device", "a.org")), {}, out, ask=FakeAsk({S: said("own"), H: said("own")}))
        ask = FakeAsk({})
        assert run_identity(store, frame(("device", "a.org")), {}, out, ask=ask)["already_done"] == 1
        measured(store, "a.org", HOME.replace(b"<p>welcome</p>", b"<p>welcome <!-- a new build --></p>"), pass_no=2)
        assert run_identity(store, frame(("device", "a.org")), {}, out, ask=ask)["already_done"] == 1  # same text
        assert ask.calls == []
        measured(store, "a.org", HOME.replace(b"welcome", b"now a gym blog"), pass_no=2)
        ask = FakeAsk({S: said("other"), H: said("other")})
        assert run_identity(store, frame(("device", "a.org")), {}, out, ask=ask)["decided"] == 1
    assert [r["site"] for r in rows(out)] == ["own", "other"]


def test_opted_out_and_unmeasured_domains_are_not_asked(tmp_path):
    out = tmp_path / "identity.jsonl"
    with Store(tmp_path / "s.sqlite") as store:
        measured(store, "a.org")
        measured(store, "b.org", final="https://www.parent.org/")
        synth.rec(store, "c.org", "homepage", HOME, ctype="text/html", url="https://c.org/")
        store.mark_done("c.org", 1, "mismatch")
        ask = FakeAsk({S: said("own"), H: said("own")})
        counts = run_identity(store, frame(*(("device", d) for d in ("a.org", "b.org", "c.org", "d.org"))), {}, out,
                              ask=ask, optout=frozenset({"a.org", "parent.org"}))
    assert (counts["opted_out"], counts["not_measured"], counts["decided"]) == (2, 2, 0) and ask.calls == []


# ---- speed: domains decided concurrently, rows whole

def _many(store, n):
    for i in range(n):
        measured(store, f"d{i:02d}.org", HOME.replace(b"welcome", f"welcome {i}".encode()))
    return frame(*(("device", f"d{i:02d}.org") for i in range(n)))


def test_calls_overlap_up_to_the_number_of_workers_and_every_row_is_whole(tmp_path):
    """Fix round 1, M7: the largest number of calls in flight reaches the worker count (one worker: 1)."""
    for workers in (1, 4):
        flight = {"now": 0, "most": 0, "calls": 0}
        lock = threading.Lock()
        gate = threading.Barrier(workers, timeout=10)  # the first calls wait until every worker has one in flight

        def ask(prompt, model, system, purpose, **kw):
            with lock:
                flight["now"] += 1
                flight["most"] = max(flight["most"], flight["now"])
                first_round = flight["calls"] < workers
                flight["calls"] += 1
            try:
                if first_round:
                    gate.wait()
                time.sleep(0.01)
                return SimpleNamespace(text=said("own"))
            finally:
                with lock:
                    flight["now"] -= 1
        out = tmp_path / f"w{workers}.jsonl"
        with Store(tmp_path / f"s{workers}.sqlite") as store:
            counts = run_identity(store, _many(store, 12), {}, out, ask=ask, workers=workers)
        assert counts["decided"] == 12 and flight["most"] == workers
        lines = out.read_text().splitlines()
        assert len(lines) == 12 and all(json.loads(x)["site"] == "own" for x in lines)


def test_several_workers_through_the_real_logger_write_every_call_once_and_whole(tmp_path):
    """Fix round 1, M7: the model process is faked behind the real `llm.ask_claude` logger."""
    import subprocess

    from noroshi import llm

    log = tmp_path / "llm_log.jsonl"

    def ask(prompt, model, system, purpose, **kw):
        def runner(argv, **k):
            time.sleep(0.005)
            return subprocess.CompletedProcess(argv, 0, json.dumps({"result": said("own", "x" * 2000)}), "")
        return llm.ask_claude(prompt, model, system, purpose, runner=runner, log_path=log, meta=kw.get("meta"))
    with Store(tmp_path / "s.sqlite") as store:
        run_identity(store, _many(store, 20), {}, tmp_path / "identity.jsonl", ask=ask, workers=8)
    calls = [json.loads(line) for line in log.read_text().splitlines()]  # every line whole JSON
    seen = sorted((c["meta"]["domain"], c["model"]) for c in calls)
    assert seen == sorted((f"d{i:02d}.org", m) for i in range(20) for m in (S, H))  # each call once


def test_the_number_of_workers_is_one_to_eight(tmp_path):
    with Store(tmp_path / "s.sqlite") as store:
        for bad in (0, 9):
            with pytest.raises(ValueError, match="1 to 8"):
                run_identity(store, frame(), {}, tmp_path / "i.jsonl", ask=FakeAsk({}), workers=bad)


def _canon(path):
    return sorted((r["domain"], r["site"], r["agreed"], r["page_sha256"]) for r in rows(path))


def test_a_run_stopped_after_five_failures_in_a_row_resumes_to_the_same_rows(tmp_path):
    def flaky(prompt):  # a usage limit that starts after a few domains
        return RuntimeError("usage limit") if any(f"welcome {i}" in prompt for i in range(3, 12)) else said("own")
    with Store(tmp_path / "s.sqlite") as store:
        df = _many(store, 12)
        whole = tmp_path / "whole.jsonl"
        run_identity(store, df, {}, whole, ask=FakeAsk({S: said("own"), H: said("own")}), workers=4)
        out = tmp_path / "i.jsonl"
        with pytest.raises(identity.IdentityStopped, match="5 domains in a row") as stop:
            run_identity(store, df, {}, out, ask=FakeAsk({S: flaky, H: said("own")}), workers=1)
        assert stop.value.counts["failed"] == 5 and stop.value.counts["decided"] == 3
        assert len(rows(out)) == 3
        with out.open("a") as fh:
            fh.write('{"domain": "d11.org", "site": "ow')  # a line cut off by a kill: ignored and done again
        counts = run_identity(store, df, {}, out, ask=FakeAsk({S: said("own"), H: said("own")}), workers=4)
    assert counts["already_done"] == 3 and counts["decided"] == 9
    assert _canon(out) == _canon(whole)


def test_a_stop_lets_running_calls_finish_and_writes_their_rows(tmp_path):
    def slow_or_down(prompt):
        if "welcome 0" in prompt:
            time.sleep(0.4)  # still running when the failures arrive
            return said("own")
        time.sleep(0.03)
        return RuntimeError("outage")
    with Store(tmp_path / "s.sqlite") as store:
        df = _many(store, 12)
        out = tmp_path / "i.jsonl"
        with pytest.raises(identity.IdentityStopped) as stop:
            run_identity(store, df, {}, out, ask=FakeAsk({S: slow_or_down, H: said("own")}), workers=3)
    assert [r["domain"] for r in rows(out)] == ["d00.org"] and stop.value.counts["decided"] == 1
    assert stop.value.counts["failed"] >= 5 and stop.value.counts["failed"] + 1 < 12  # pending domains were not started


# ---- derive: identity, identity_agreed, mismatch_reason

C2 = {  # the audit's five measured non-sites (C2), with their stored homepages cut down
    "e-mercy.com": ("hospital", "emercy_home_browser.html"),
    "greenehealth.org": ("hospital", "greenehealth_home.html"),
    "wvuniversityhealthcare.com": ("hospital", "wvuniversityhealthcare_home.html"),
    "getqardio.com": ("icsma", "getqardio_home.html"),
    "intuitive.com": ("device", "intuitive_home.html"),
}


def _derive(store, df, decisions):
    reg = synth.Registries(platforms={}, diodb=set(), cna=set())
    return derive_domains(store, df, Path("/nonexistent/judgements.jsonl"), reg, Path("/nonexistent/w.jsonl"),
                          synth.NOW, identity=decisions).set_index("domain")


def test_the_five_audit_non_sites_end_mismatch_when_the_raters_say_other(tmp_path):
    out = tmp_path / "identity.jsonl"
    with Store(tmp_path / "s.sqlite") as store:
        for d, (sector, fixture) in C2.items():
            synth.core(store, d, home=(PILOT / fixture).read_bytes(), sector=sector,
                       wk=None, cms=None if sector == "hospital" else False)
            store.mark_done(d, 1, "measured")
        synth.core(store, "real.org", home=HOME)
        store.mark_done("real.org", 1, "measured")
        df = frame(*((s, d) for d, (s, _) in C2.items()), ("hospital", "real.org"))
        ask = FakeAsk({S: lambda p: said("own" if "real.org" in p else "other"),
                       H: lambda p: said("own" if "real.org" in p else "other")})
        assert run_identity(store, df, {}, out, ask=ask)["decided"] == 6
        d = _derive(store, df, out)
    for name in C2:
        r = d.loc[name]
        assert (r.status, r.identity, r.identity_agreed, r.mismatch_reason) == ("mismatch", "other", True,
                                                                                "identity_other"), name
        assert (r.main, r.strict, r.broad, r.listing, r.sectxt_class, r.policy_page, r.final_domain) == \
            (None, None, None, None, "unmeasured", "unmeasured", ""), name
    assert (d.loc["real.org", "status"], d.loc["real.org", "identity"], d.loc["real.org", "mismatch_reason"]) == \
        ("measured", "own", "")


def test_unclear_ends_mismatch_and_a_crawl_mismatch_is_a_crawl_rule(tmp_path):
    out = tmp_path / "identity.jsonl"
    with Store(tmp_path / "s.sqlite") as store:
        synth.core(store, "shell.org", home=b"<html><body><app-root></app-root></body></html>", sector="device",
                   cms=False)
        store.mark_done("shell.org", 1, "measured")
        synth.rec(store, "parked.org", "homepage", HOME, ctype="text/html", url="https://parked.org/",
                  final="https://www.hugedomains.com/", sector="device")
        store.mark_done("parked.org", 1, "mismatch")
        df = frame(("device", "shell.org"), ("device", "parked.org"))
        ask = FakeAsk({S: said("unclear"), H: said("other"), O: said("unclear")})
        run_identity(store, df, {}, out, ask=ask)
        d = _derive(store, df, out)
    assert (d.loc["shell.org", "status"], d.loc["shell.org", "identity"], d.loc["shell.org", "identity_agreed"],
            d.loc["shell.org", "mismatch_reason"]) == ("mismatch", "unclear", False, "identity_unclear")
    assert (d.loc["parked.org", "status"], d.loc["parked.org", "identity"], d.loc["parked.org", "mismatch_reason"]) == \
        ("mismatch", "", "crawl_rule")


def test_derive_leaves_identity_empty_until_checked_and_federal_only_domains_exempt(tmp_path):
    with Store(tmp_path / "s.sqlite") as store:
        synth.core(store, "a.org", home=HOME, sector="device", cms=False)
        store.mark_done("a.org", 1, "measured")
        synth.core(store, "fed.gov", home=HOME, sector="federal", cms=False)
        store.mark_done("fed.gov", 1, "measured")
        d = _derive(store, frame(("device", "a.org"), ("federal", "fed.gov")), tmp_path / "missing.jsonl")
    assert (d.loc["a.org", "status"], d.loc["a.org", "identity"], d.loc["a.org", "identity_agreed"]) == \
        ("measured", "", None)
    assert (d.loc["fed.gov", "status"], d.loc["fed.gov", "identity"]) == ("measured", "exempt")


def test_a_decision_for_an_older_homepage_does_not_count_for_a_changed_one(tmp_path):
    out = tmp_path / "identity.jsonl"
    with Store(tmp_path / "s.sqlite") as store:
        synth.core(store, "a.org", home=HOME, sector="device", cms=False)
        store.mark_done("a.org", 1, "measured")
        run_identity(store, frame(("device", "a.org")), {}, out, ask=FakeAsk({S: said("other"), H: said("other")}))
        synth.core(store, "a.org", home=HOME.replace(b"welcome", b"hello again"), sector="device", cms=False,
                   pass_no=2)
        store.mark_done("a.org", 2, "measured")
        d = _derive(store, frame(("device", "a.org")), out)
    assert (d.loc["a.org", "status"], d.loc["a.org", "identity"]) == ("measured", "")


# ---- the commands

@pytest.fixture
def env(tmp_path, monkeypatch):
    """The synthetic store as the main store, its frame as the domain tables, no identity file yet."""
    from noroshi import config

    monkeypatch.setenv("NOROSHI_ROOT", str(tmp_path))
    p = config.paths()
    for d in (p.raw, p.frames, p.registries, p.info):
        d.mkdir(parents=True, exist_ok=True)
    store, frame_, judg, reg = synth.build(tmp_path)
    store.close()
    (tmp_path / "s.sqlite").rename(p.raw / "crawl.sqlite")
    (tmp_path / "blobs").rename(p.raw / "blobs")
    judg.rename(p.raw / "judgements.jsonl")
    for sector, name in (("hospital", "hospital_domains.csv"), ("federal", "federal_domains.csv"),
                         ("device", "device_domains.csv")):
        frame_[frame_.sector == sector].to_csv(p.frames / name, index=False)
    hospital_domains = list(frame_[frame_.sector == "hospital"].domain)  # every one named, good.org first
    pd.DataFrame([{"hifld_id": str(i + 1), "name": f"{d.split('.')[0].upper()} HOSPITAL", "state": "TX",
                   "type": "GENERAL ACUTE CARE", "owner": "NON-PROFIT", "beds": "100", "rural": "True", "domain": d,
                   "exclusion": ""} for i, d in enumerate(hospital_domains)]).to_csv(p.frames / "hospitals.csv",
                                                                                     index=False)
    (p.passes).mkdir(parents=True, exist_ok=True)
    for which in ("memory_a", "memory_b"):  # vendor.com, the device domain, resolved from its company name
        (p.passes / f"device_{which}.jsonl").write_text(json.dumps({"key": "vendor", "name": "Vendor Medical Inc",
                                                                    "model": "m", "domain": "vendor.com"}) + "\n")
    (p.registries / "program-list.json").write_text("[]")
    (p.registries / "CNAsList.json").write_text("[]")
    for name in ("hackerone", "bugcrowd", "intigriti", "yeswehack"):
        (p.registries / f"{name}_data.json").write_text("[]")
    return p


def _fake_models(monkeypatch, ask):
    orig = identity.run_identity
    seen = {}

    def run(store, domains, names, out, **kw):
        seen.update(kw, names=names, out=out)
        return orig(store, domains, names, out, ask=ask, **kw)
    monkeypatch.setattr(identity, "run_identity", run)
    return seen


def test_the_identity_command_decides_every_measured_domain_and_analyze_waits_for_it(env, monkeypatch, capsys):
    from noroshi import cli

    cli.main(["derive"])
    assert "13 measured domains have no identity decision yet" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        cli.main(["analyze"])
    assert "13 measured domains have no identity decision" in str(e.value) and "noroshi identity" in str(e.value)
    assert not (env.results / "numbers.json").exists()
    seen = _fake_models(monkeypatch, FakeAsk({S: said("own"), H: said("own")}))
    cli.main(["identity", "--workers", "2"])
    assert seen["workers"] == 2 and seen["out"] == env.raw / "identity.jsonl" and "optout" in seen
    assert seen["names"]["good.org"] == Listing("GOOD HOSPITAL", "Location: TX")
    assert "decided: 13" in capsys.readouterr().out
    cli.main(["derive"])
    assert ("identity check: 13 domains decided (13 own, 0 other, 0 unclear), 1 exempt, 0 measured domains without "
            "a decision yet") in capsys.readouterr().out
    cli.main(["analyze", "--allow-incomplete"])  # notcrawled.org is still not crawled
    d = pd.read_csv(env.derived / "domains.csv", dtype=str, keep_default_na=False).set_index("domain")
    assert d.loc["fed.gov", "identity"] == "exempt" and d.loc["good.org", "identity"] == "own"


def test_the_main_analysis_may_run_without_identity_decisions_only_when_allowed(env, capsys):
    from noroshi import cli

    cli.main(["derive"])
    cli.main(["analyze", "--allow-incomplete"])
    assert (env.results / "numbers.json").exists()


def test_the_pilot_identity_check_writes_its_own_file_and_the_pilot_analysis_only_prints_the_count(env, monkeypatch,
                                                                                                   capsys):
    from noroshi import cli

    for name in ("crawl.sqlite", "judgements.jsonl"):
        (env.raw / name).rename(env.raw / ("pilot.sqlite" if name.endswith(".sqlite") else f"pilot_{name}"))
    (env.raw / "blobs").rename(env.raw / "pilot_blobs")
    cli.main(["derive", "--store", "pilot"])
    cli.main(["analyze", "--store", "pilot"])
    assert "13 measured domains have no identity decision yet" in capsys.readouterr().out
    assert (env.results / "pilot" / "numbers.json").exists()
    seen = _fake_models(monkeypatch, FakeAsk({S: said("other"), H: said("other")}))
    cli.main(["identity", "--store", "pilot"])
    assert seen["out"] == env.raw / "pilot_identity.jsonl" and seen["workers"] == 4
    assert not (env.raw / "identity.jsonl").exists()
    cli.main(["derive", "--store", "pilot"])
    d = pd.read_csv(env.derived / "pilot" / "domains.csv", dtype=str, keep_default_na=False).set_index("domain")
    assert (d.loc["good.org", "status"], d.loc["good.org", "mismatch_reason"]) == ("mismatch", "identity_other")


@pytest.mark.parametrize("workers", ["0", "9"])
def test_the_identity_command_takes_one_to_eight_workers(env, workers):
    from noroshi import cli

    with pytest.raises(SystemExit) as e:
        cli.main(["identity", "--workers", workers])
    assert e.value.code != 0


def test_the_identity_command_exits_with_a_clear_message_when_it_stops(env, monkeypatch):
    from noroshi import cli

    _fake_models(monkeypatch, FakeAsk({S: RuntimeError("limit"), H: said("own")}))
    with pytest.raises(SystemExit) as e:
        cli.main(["identity", "--workers", "1"])
    assert "5 domains in a row" in str(e.value) and "noroshi identity" in str(e.value)


# ---- a late opt-out: purge removes the domain's identity rows and their model-log rows

def test_purge_removes_the_identity_rows_and_model_log_rows_of_the_domain_and_of_one_that_landed_on_it(tmp_path):
    from noroshi import llm
    from noroshi.optout import purge

    log = tmp_path / "llm_log.jsonl"

    def logged(prompt, model, system, purpose, **kw):  # the real logger, with a faked command
        import subprocess
        runner = lambda argv, **k: subprocess.CompletedProcess(argv, 0, json.dumps({"result": said("own")}), "")
        return llm.ask_claude(prompt, model, system, purpose, runner=runner, log_path=log, meta=kw.get("meta"))
    out = tmp_path / "identity.jsonl"
    with Store(tmp_path / "s.sqlite") as store:
        measured(store, "gone.org")
        measured(store, "alias.org", final="https://www.gone.org/")  # landed on the site that opts out
        measured(store, "kept.org")
        run_identity(store, frame(*(("device", d) for d in ("gone.org", "alias.org", "kept.org"))), {}, out,
                     ask=logged, workers=1)
        assert len(rows(out)) == 3 and len(log.read_text().splitlines()) == 6
        removed = purge(store, "gone.org", tmp_path / "j.jsonl", [], log, identity=out)
    assert [r["domain"] for r in rows(out)] == ["kept.org"]
    assert {json.loads(x)["meta"]["domain"] for x in log.read_text().splitlines()} == {"kept.org"}
    assert (removed["identity rows"], removed["model-log rows"]) == (2, 4)


# ---- fix round 1, G6: the model runs hold the store's run lock

def test_a_purge_while_the_identity_check_runs_is_refused_and_changes_nothing(env, monkeypatch):
    """Review A, probe_purge_during_identity.py turned around: the identity run holds the lock, so the purge refuses."""
    from noroshi import cli

    (env.info / "optout.txt").write_text("good.org\n")
    refused = []

    def ask(prompt, model, system, purpose, **kw):
        if not refused:
            with pytest.raises(SystemExit) as e:
                cli.main(["purge", "good.org"])
            refused.append(str(e.value))
        return SimpleNamespace(text=said("own"))
    _fake_models(monkeypatch, ask)
    cli.main(["identity", "--workers", "1"])
    assert "noroshi identity" in refused[0] and "nothing was removed" in refused[0]
    with Store(env.raw / "crawl.sqlite") as s:
        assert s.requests_for("good.org", 1)
    assert not (env.raw / "purge.log").exists()


def test_the_model_runs_refuse_while_a_crawl_or_a_purge_holds_the_store(env):
    import fcntl

    from noroshi import cli

    for holder in ("noroshi crawl", "noroshi purge"):
        with open(env.raw / "crawl.sqlite.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.write(f"pid 4242 {holder}\n")
            held.flush()
            for command in (["identity"], ["judge"]):
                with pytest.raises(SystemExit) as e:
                    cli.main(command)
                assert f"in use by `{holder}` (pid 4242)" in str(e.value) and "another crawl" not in str(e.value)
    assert not (env.raw / "identity.jsonl").exists()


def test_a_purge_while_the_judge_runs_is_refused(env, monkeypatch):
    from noroshi import cli, judge

    (env.raw / "judgements.jsonl").unlink()
    (env.info / "optout.txt").write_text("notcrawled.org\n")  # listed, never crawled: a purge would remove nothing
    refused = []

    def ask(prompt, model, system, purpose, **kw):
        if not refused:
            with pytest.raises(SystemExit) as e:
                cli.main(["purge", "notcrawled.org"])
            refused.append(str(e.value))
        return SimpleNamespace(text=json.dumps({"invites_reports": True, "contact_method": True,
                                                "safe_harbor": False}))
    orig = judge.run_judging
    monkeypatch.setattr(judge, "run_judging", lambda store, out, **kw: orig(store, out, ask=ask, **kw))
    cli.main(["judge"])
    assert refused and "noroshi judge" in refused[0] and not (env.raw / "purge.log").exists()


# ---- fix round 1, G3: the agreement table reads the decision derive used

def test_the_agreement_table_reads_the_decision_derive_matched_not_the_last_one_written(tmp_path):
    from noroshi.analysis import tables
    from noroshi.outcomes import derive_hospitals

    out = tmp_path / "identity.jsonl"
    with Store(tmp_path / "s.sqlite") as store:
        synth.core(store, "a.org", home=HOME, sector="device", cms=False)
        store.mark_done("a.org", 1, "measured")
        run_identity(store, frame(("device", "a.org")), {}, out, ask=FakeAsk({S: said("own"), H: said("own")}))
        synth.core(store, "a.org", home=HOME.replace(b"welcome", b"hello again"), sector="device", cms=False,
                   pass_no=2)
        store.mark_done("a.org", 2, "measured")
        run_identity(store, frame(("device", "a.org")), {}, out,
                     ask=FakeAsk({S: said("own"), H: said("unclear"), O: said("own")}))
        first, second = out.read_text().splitlines()
        out.write_text(second + "\n" + first + "\n")  # the decision derive matches is no longer the last line
        d = _derive(store, frame(("device", "a.org")), out)
    assert d.loc["a.org", "identity_page_sha256"] == json.loads(second)["page_sha256"]
    assert (d.loc["a.org", "identity"], d.loc["a.org", "identity_agreed"]) == ("own", False)
    n = tables.build_all(d.reset_index(), derive_hospitals(pd.DataFrame(columns=["hifld_id", "name", "state", "type",
                         "owner", "beds", "rural", "domain", "exclusion"]), d.reset_index()), tmp_path / "t",
                         identity=[json.loads(x) for x in out.read_text().splitlines()])
    assert (n["agreement.identity.site.n"], n["agreement.identity.site.agree"]) == (1, 0)  # own and unclear


# ---- fix round 1, M2: the page title is the document's title

def test_the_page_title_is_the_documents_title_not_the_titles_of_inline_svg_icons(tmp_path):
    icon = '<svg viewBox="0 0 1 1"><title>{}</title><path d="M0 0"/></svg>'
    body = ("<html><head><title>Mercy Health | Fairfield Hospital</title></head><body>"
            + "".join(icon.format(t) for t in ("Search", "Close", "Facebook"))
            + "<p>" + "Care close to home for every family in the valley. " * 5 + "</p></body></html>").encode()
    with Store(tmp_path / "s.sqlite") as store:
        measured(store, "a.org", body)
        view = page_view(store, store.requests_for("a.org", 1)[0])
    assert view.title == "Mercy Health | Fairfield Hospital"
    with Store(tmp_path / "t.sqlite") as store:
        measured(store, "b.org", ("<html><body>" + icon.format("Search") + "<p>text</p></body></html>").encode())
        assert page_view(store, store.requests_for("b.org", 1)[0]).title == "(none)"


# ---- fix round 1, M4: no nameless prompt

def test_the_identity_command_refuses_when_a_measured_domain_has_no_name_on_the_list(env, monkeypatch):
    from noroshi import cli

    seen = _fake_models(monkeypatch, FakeAsk({S: said("own"), H: said("own")}))
    for which in ("memory_a", "memory_b"):
        (env.passes / f"device_{which}.jsonl").unlink()
    hospitals = pd.read_csv(env.frames / "hospitals.csv", dtype=str, keep_default_na=False)
    hospitals[hospitals.domain != "late.org"].to_csv(env.frames / "hospitals.csv", index=False)
    with pytest.raises(SystemExit) as e:
        cli.main(["identity"])
    message = str(e.value)
    assert "late.org (hospital)" in message and "vendor.com (device)" in message and "good.org" not in message
    assert "device_memory_a.jsonl (missing)" in message and "hospitals.csv (no name for it there)" in message
    assert "nothing was asked" in message and "workers" not in seen and not (env.raw / "identity.jsonl").exists()


# ---- fix round 1, M5: crash paths of the threaded run

def test_a_decision_that_cannot_be_written_stops_the_run_with_an_error(tmp_path):
    out = tmp_path / "identity.jsonl"
    out.write_text("")
    out.chmod(0o444)  # the disk refuses the row
    with Store(tmp_path / "s.sqlite") as store:
        df = _many(store, 4)
        with pytest.raises(PermissionError):
            run_identity(store, df, {}, out, ask=FakeAsk({S: said("own"), H: said("own")}), workers=2)
    out.chmod(0o644)


def test_the_identity_command_exits_non_zero_when_a_decision_cannot_be_written(env, monkeypatch):
    from noroshi import cli

    _fake_models(monkeypatch, FakeAsk({S: said("own"), H: said("own")}))
    out = env.raw / "identity.jsonl"
    out.write_text("")
    out.chmod(0o444)
    with pytest.raises(SystemExit) as e:
        cli.main(["identity"])
    out.chmod(0o644)
    assert e.value.code != 0 and "could not be written" in str(e.value)


def test_an_interrupt_in_this_thread_lets_running_calls_finish_and_starts_no_new_call(tmp_path):
    """The direct path (run_identity called with no signal handler): a KeyboardInterrupt. Fix round 2, H2: no call
    starts after it, not even the next rater of a domain in progress, which stays undecided. The real signal path is
    tests/test_stop_signals.py."""
    import _thread

    calls = []

    def ask(prompt, model, system, purpose, **kw):
        calls.append((kw["meta"]["domain"], model))
        if len({d for d, _ in calls}) == 2 and model == S:
            _thread.interrupt_main()  # Ctrl-C while two domains are being decided
        time.sleep(1.0)  # fix round 3, K11: a margin a loaded machine cannot miss (it was 0.3 s)
        return SimpleNamespace(text=said("own"))
    with Store(tmp_path / "s.sqlite") as store:
        df = _many(store, 8)
        out = tmp_path / "identity.jsonl"
        with pytest.raises(KeyboardInterrupt):
            run_identity(store, df, {}, out, ask=ask, workers=2)
    started = {d for d, _ in calls}
    written = {r["domain"] for r in rows(out)} if out.exists() else set()
    assert len(started) == 2 and written <= {d for d in started if (d, H) in calls}
    second = [d for d, m in calls if m == S][1]
    assert (second, H) not in calls and second not in written  # its next rater never started
    assert all(not t.is_alive() for t in threading.enumerate() if t.name.startswith("identity-"))


# ---- fix round 1, M6: --allow-incomplete says what it lets through

def test_allow_incomplete_prints_what_it_lets_through_and_its_help_names_both(env, capsys):
    from noroshi import cli

    cli.main(["derive"])
    out = capsys.readouterr().out
    assert ("identity check: 0 domains decided (0 own, 0 other, 0 unclear), 1 exempt, 13 measured domains without a "
            "decision yet") in out and "run `noroshi identity`" in out
    cli.main(["analyze", "--allow-incomplete"])
    out = capsys.readouterr().out
    assert "--allow-incomplete" in out and "1 domains not crawled" in out
    assert "13 measured domains without an identity decision" in out
    analyze = next(a for a in cli.build_parser()._subparsers._group_actions[0].choices["analyze"]._actions
                   if "--allow-incomplete" in a.option_strings)
    assert "not_crawled" in analyze.help and "identity decision" in analyze.help
