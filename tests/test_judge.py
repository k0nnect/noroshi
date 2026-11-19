import json
import subprocess
from types import SimpleNamespace

import pytest

from noroshi.discover import PAGE_TEXT_PATTERN
from noroshi.judge import (JudgeError, extract_text, adjudicate, excerpt, judge_page, prefilter, run_judging,
                           visible_text)
from noroshi.store import FetchRecord, Store


def reply(inv, con, safe=False):
    return json.dumps({"invites_reports": inv, "contact_method": con, "safe_harbor": safe})


class FakeAsk:
    """Replies by model name; each value is a list of replies consumed in order."""

    def __init__(self, replies):
        self.replies = {k: list(v) for k, v in replies.items()}
        self.calls = []

    def __call__(self, prompt, model, system, purpose, **kw):
        self.calls.append((prompt, model, system, purpose))
        item = self.replies[model].pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(text=item)


A, B, T = "claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-2"
# Post-pilot Y2: a page with fewer than 200 visible characters is never judged, so test pages carry this filler
FILLER = b"<p>" + b"We thank every researcher who writes to us in good faith about our systems. " * 3 + b"</p>"


def test_prefilter():
    assert prefilter("To report a security vulnerability email security@x.org")
    assert prefilter("We publish a security.txt file")
    assert not prefilter("Visiting hours and parking. Campus security office is on level 1.")


def test_visible_text_drops_scripts_and_collapses_space():
    html = b"<html><head><style>p{}</style><script>var x=1;</script></head><body><p>Report   a\n vulnerability</p><noscript>enable js</noscript></body></html>"
    assert visible_text(html) == "Report a vulnerability"


def test_visible_text_plain_text_passthrough():
    assert visible_text(b"Contact: mailto:a@b.org\nExpires: 2030") == "Contact: mailto:a@b.org Expires: 2030"


def test_excerpt_short_text_unchanged():
    assert excerpt("short text") == "short text"


def test_excerpt_keeps_matching_windows():
    text = "x" * 40_000 + " responsible disclosure program " + "y" * 10_000
    out = excerpt(text)
    assert "responsible disclosure" in out and len(out) <= 40_000


def _federal_template_policy() -> str:
    """The reviewer's probe 8: a 7,988-character policy whose reporting section comes after the last phrase
    the pattern matches (the layout of the common federal template)."""
    pad = lambda n: ("We ask that you follow these guidelines and act in good faith at all times. " * 40)[:n]
    sections = [
        ("Vulnerability Disclosure Policy", "This policy gives security researchers clear guidelines for conducting "
         "vulnerability discovery activities. " + pad(700)),
        ("Authorization", "If you make a good faith effort to comply with this vulnerability disclosure policy we will "
         "consider your research authorized. " + pad(900)),
        ("Guidelines", "Security researchers must notify us as soon as possible. Coordinated disclosure is expected. "
         + pad(1500)),
        ("Test methods", "The following are not authorized. " + pad(1200)),
        ("Scope", "This responsible disclosure policy applies to the following systems. Product security teams are "
         "listed. " + pad(1500)),
        ("Reporting a vulnerability", "We accept reports of weaknesses at secret-contact@agency.test or through our "
         "web form. " + pad(600)),
        ("What you can expect from us", pad(900)),
    ]
    return " ".join(f"{h} {b}" for h, b in sections)


def test_the_judge_sees_the_contact_of_a_long_policy():
    text = _federal_template_policy()
    assert len(text) == 7988 and text.index("secret-contact@") > max(m.end() for m in PAGE_TEXT_PATTERN.finditer(text)) + 500
    ask = FakeAsk({A: [reply(True, True)]})
    judge_page("https://agency.test/vdp", text, A, ask=ask)
    assert "secret-contact@agency.test" in ask.calls[0][0]
    assert text in ask.calls[0][0]  # 40,000 characters or less: the whole visible text


def test_excerpt_of_a_very_long_page_keeps_the_opening_and_windows_in_page_order():
    filler = lambda c, n: (c * 9 + " ") * (n // 10)
    text = (filler("a", 30_000) + "write to psirt@corp.example today " + filler("b", 30_000)
            + "see hackerone.com/corp for rewards " + filler("c", 30_000) + "mailto:x " + filler("d", 30_000)
            + "a bug bounty exists " + filler("e", 30_000))
    out = excerpt(text)
    assert len(out) <= 40_000 and out.startswith(text[:20_000])
    for marker in ("psirt@corp.example", "hackerone.com", "mailto:", "bug bounty"):  # the matched text of each hit
        i = text.index(marker)
        assert text[i - 1000:i + len(marker) + 1000] in out, marker  # 1,000 characters on each side
    assert out.index("psirt@") < out.index("hackerone.com") < out.index("mailto:x") < out.index("bug bounty")


def test_excerpt_stops_at_forty_thousand_characters():
    text = " ".join(f"{'z' * 2500} contact{i}@corp.example" for i in range(40))  # 40 hits, each 2,000+ wide
    out = excerpt(text)
    assert len(out) == 40_000 and out.startswith(text[:20_000])
    assert "contact39@" not in out


def test_judge_page_parses_and_builds_prompt():
    ask = FakeAsk({A: [reply(True, True, True)]})
    j = judge_page("https://x.org/p", "some text", A, ask=ask)
    assert (j.invites_reports, j.contact_method, j.safe_harbor) == (True, True, True)
    prompt, model, system, purpose = ask.calls[0]
    assert prompt == PROMPT_AS_WRITTEN.replace("{excerpt}", "some text")
    assert "x.org" not in prompt  # post-pilot Y1: the address is not in the prompt
    assert system == "You classify web pages for a research study. Answer only with a JSON object."


PROMPT_AS_WRITTEN = """You are classifying one web page for a research study. The page was fetched from an organization's website.

Page text:
<<<
{excerpt}
>>>

Answer three questions about what this page itself says.

1. invites_reports: Does this page tell people outside the organization how to report a security vulnerability in the organization's own websites, systems or products? A page that points reporters to a reporting address or to a disclosure platform counts. Answer false for a page whose only relevant content is a navigation or footer link to a policy that lives elsewhere, for a general home, navigation or error page, and for pages about patient privacy complaints, compliance or fraud hotlines, phishing emails someone received, or physical security.
2. contact_method: Does this page give a way to send such a report: an email address, a web form, a phone number, or a link to a disclosure platform? Answer false when your answer to question 1 is false.
3. safe_harbor: Does this page commit not to pursue legal action against people who report in good faith? Answer false when your answer to question 1 is false.

Reply with one JSON object and nothing else. It has exactly three keys, "invites_reports", "contact_method" and "safe_harbor", and each value is true or false according to your answers."""


def test_the_prompt_carries_no_answer_and_no_address():
    """Post-pilot Y1 (audit C1): the old prompt ended with a filled-in reply that the models copied."""
    import re

    from noroshi.judge import PROMPT

    assert PROMPT == PROMPT_AS_WRITTEN
    assert not re.search(r'"\w+"\s*:\s*(?:true|false)', PROMPT, re.IGNORECASE)  # no JSON example with a value
    assert not re.search(r"[{\[][^{}\[\]]*\b(?:true|false)\b", PROMPT.replace("{excerpt}", ""))
    assert "{url}" not in PROMPT and "URL" not in PROMPT and "http" not in PROMPT
    assert re.findall(r"{\w+}", PROMPT) == ["{excerpt}"]  # the only substitution


def test_the_judge_logs_the_page_address_as_meta(tmp_path):
    """Post-pilot Y1: the requested URL, the final URL and the listed domain go to the log beside the call."""
    page = POLICY
    with Store(tmp_path / "crawl.sqlite") as store:
        rec = _rec("a.org", "https://a.org/vdp", "candidate", None)
        rec.final_url = "https://www.a-parent.org/security/vdp"
        store.record(rec, page)
        ask = FakeAsk({A: [reply(True, True)], B: [reply(True, True)]})
        seen = []
        orig = ask.__call__

        def spy(prompt, model, system, purpose, **kw):
            seen.append(kw.get("meta"))
            return orig(prompt, model, system, purpose, **kw)
        run_judging(store, tmp_path / "j.jsonl", ask=spy)
    assert seen == [{"url": "https://a.org/vdp", "final_url": "https://www.a-parent.org/security/vdp",
                     "domain": "a.org"}] * 2
    assert all("a.org" not in prompt for prompt, *_ in ask.calls)


def test_adjudicate_agreement_no_tiebreak():
    ask = FakeAsk({A: [reply(True, True)], B: [reply(True, True)]})
    out = adjudicate("https://x.org", "t", ask=ask)
    assert len(ask.calls) == 2 and out["agreed"] is True and out["tiebreak"] is None
    assert out["final"] == {"invites_reports": True, "contact_method": True, "safe_harbor": False}


def test_adjudicate_disagreement_uses_tiebreak():
    ask = FakeAsk({A: [reply(True, False)], B: [reply(False, False)], T: [reply(True, False)]})
    out = adjudicate("https://x.org", "t", ask=ask)
    assert len(ask.calls) == 3 and out["agreed"] is False
    assert out["final"]["invites_reports"] is True and out["final"]["contact_method"] is False
    assert out["tiebreak"]["invites_reports"] is True


def test_malformed_model_reply_raises_clear_error():
    ask = FakeAsk({A: ["not json at all", "{\"invites_reports\": true}"]})
    with pytest.raises(JudgeError, match=r"https://x\.org.*claude-opus-5-5"):
        judge_page("https://x.org", "t", A, ask=ask)
    assert len(ask.calls) == 2  # one retry


def test_failed_call_retried_once_then_succeeds():
    ask = FakeAsk({A: [RuntimeError("boom"), reply(False, False)]})
    assert judge_page("https://x.org", "t", A, ask=ask).invites_reports is False
    ask = FakeAsk({A: [RuntimeError("boom"), RuntimeError("boom")]})
    with pytest.raises(JudgeError, match="claude-opus-5-5"):
        judge_page("https://x.org", "t", A, ask=ask)


def _rec(domain, url, purpose, sha, status=200, blocked=False):
    return FetchRecord(domain, "hospital", domain, url, purpose, 1, "identified", "t", status, url, [],
                       "text/html", sha, 0, False, None, blocked)


def test_run_judging_skips_done_and_counts_failures(tmp_path):
    pol = b"<p>To report a security vulnerability email security@x.org</p>" + FILLER
    other = b"<p>Visiting hours and parking</p>" + FILLER
    bad = b"<p>Report a vulnerability here</p>" + FILLER
    with Store(tmp_path / "crawl.sqlite") as store:
        store.record(_rec("a.org", "https://a.org/p", "candidate", None), pol)
        store.record(_rec("b.org", "https://b.org/", "probe", None), other)
        store.record(_rec("c.org", "https://c.org/p", "policy_field", None), bad)
        store.record(_rec("d.org", "https://d.org/x", "robots", None), pol + b"x")  # wrong purpose
        store.record(_rec("e.org", "https://e.org/x", "probe", None, status=404), b"nope vulnerability disclosure")
        store.record(_rec("f.org", "https://f.org/x", "probe", None, blocked=True), b"blocked vulnerability disclosure")
        out = tmp_path / "judgements.jsonl"
        ask = FakeAsk({A: [reply(True, True), "junk", "junk"], B: [reply(True, True)]})
        counts = run_judging(store, out, ask=ask)
        assert counts == {"judged": 1, "prefiltered_out": 1, "failed": 1, "already_done": 0, "pdf_unreadable": 0,
                          "non_text": 0, "unreadable": 0, "catch_all": 0, "meta_refresh": 0, "opted_out": 0,
                          "image": 0, "same_as_homepage": 0}
        rows = [json.loads(line) for line in out.read_text().splitlines()]
        assert {r["domain"] for r in rows} == {"a.org", "b.org"}
        neg = next(r for r in rows if r["domain"] == "b.org")
        assert neg["safe_harbor_agreed"] is None and neg["prefilter"] is False and neg["a"] is None and neg["final"] is None and neg["agreed"] is None
        pos = next(r for r in rows if r["domain"] == "a.org")
        assert pos["safe_harbor_agreed"] is True and pos["prefilter"] is True and pos["final"]["invites_reports"] is True and pos["sector"] == "hospital"
        assert len(pos["body_sha256"]) == 64
        # rerun: the two written pages are skipped, the failed one is retried
        ask2 = FakeAsk({A: [reply(False, False)], B: [reply(False, False)]})
        counts = run_judging(store, out, ask=ask2)
        assert counts == {"judged": 1, "prefiltered_out": 0, "failed": 0, "already_done": 2, "pdf_unreadable": 0,
                          "non_text": 0, "unreadable": 0, "catch_all": 0, "meta_refresh": 0, "opted_out": 0,
                          "image": 0, "same_as_homepage": 0}


def test_safe_harbor_split_on_policy_page_is_tiebroken():
    ask = FakeAsk({A: [reply(True, True, True)], B: [reply(True, True, False)], T: [reply(True, True, True)]})
    out = adjudicate("https://x.org", "t", ask=ask)
    assert len(ask.calls) == 3 and out["agreed"] is True and out["safe_harbor_agreed"] is False
    assert out["final"]["safe_harbor"] is True and out["tiebreak"]["safe_harbor"] is True
    ask = FakeAsk({A: [reply(True, True, True)], B: [reply(True, True, False)], T: [reply(True, True, False)]})
    assert adjudicate("https://x.org", "t", ask=ask)["final"]["safe_harbor"] is False


def test_tiebreak_answer_reused_for_safe_harbor():
    ask = FakeAsk({A: [reply(True, True, True)], B: [reply(False, True, False)], T: [reply(True, True, True)]})
    out = adjudicate("https://x.org", "t", ask=ask)
    assert len(ask.calls) == 3 and out["agreed"] is False and out["safe_harbor_agreed"] is False
    assert out["final"] == {"invites_reports": True, "contact_method": True, "safe_harbor": True}


def test_safe_harbor_split_on_non_policy_page_makes_no_extra_call():
    ask = FakeAsk({A: [reply(False, True, True)], B: [reply(False, True, False)]})
    out = adjudicate("https://x.org", "t", ask=ask)
    assert len(ask.calls) == 2 and out["tiebreak"] is None
    assert out["final"]["safe_harbor"] is False and out["safe_harbor_agreed"] is False


def test_safe_harbor_agreed_true_when_models_match():
    ask = FakeAsk({A: [reply(True, True, True)], B: [reply(True, True, True)]})
    out = adjudicate("https://x.org", "t", ask=ask)
    assert len(ask.calls) == 2 and out["safe_harbor_agreed"] is True and out["final"]["safe_harbor"] is True


SHORT_POLICY = b"<p>To report a security vulnerability email security@x.org</p>"  # 54 visible characters
POLICY = SHORT_POLICY + FILLER


def _rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_identical_page_on_two_domains_gets_two_rows_one_set_of_calls(tmp_path):
    with Store(tmp_path / "crawl.sqlite") as store:
        store.record(_rec("a.org", "https://a.org/p", "candidate", None), POLICY)
        store.record(_rec("b.org", "https://b.org/p", "candidate", None), POLICY)
        out = tmp_path / "j.jsonl"
        ask = FakeAsk({A: [reply(True, True)], B: [reply(True, True)]})
        counts = run_judging(store, out, ask=ask)
        assert len(ask.calls) == 2 and counts["judged"] == 2
        rows = _rows(out)
        assert {(r["domain"], r["url"]) for r in rows} == {("a.org", "https://a.org/p"), ("b.org", "https://b.org/p")}
        assert all(r["final"]["invites_reports"] and r["skipped"] is None for r in rows)
        # a re-run, and a third domain with the same body, need no model calls
        store.record(_rec("c.org", "https://c.org/p", "probe", None), POLICY)
        counts = run_judging(store, out, ask=FakeAsk({}))
        assert counts["already_done"] == 2 and counts["judged"] == 1 and len(_rows(out)) == 3


def test_cached_verdict_comes_from_file_on_rerun(tmp_path):
    out = tmp_path / "j.jsonl"
    with Store(tmp_path / "crawl.sqlite") as store:
        store.record(_rec("a.org", "https://a.org/p", "candidate", None), POLICY)
        run_judging(store, out, ask=FakeAsk({A: [reply(True, True)], B: [reply(True, True)]}))
        store.record(_rec("b.org", "https://b.org/p", "candidate", None), POLICY)
        ask = FakeAsk({})
        run_judging(store, out, ask=ask)
        assert ask.calls == [] and len(_rows(out)) == 2


def test_extract_text_pdf_uses_pdftotext():
    calls = []

    def runner(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, stdout=b"Report a  vulnerability\nto psirt@x.org", stderr=b"")

    text, skipped = extract_text(b"%PDF-1.7 ...", "application/pdf", False, runner=runner)
    assert text == "Report a vulnerability to psirt@x.org" and skipped is None
    assert calls[0][0][0] == "pdftotext" and calls[0][1]["input"] == b"%PDF-1.7 ..."


def test_extract_text_pdf_unreadable_cases():
    ok = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=b"text", stderr=b"")
    assert extract_text(b"%PDF-1.7", "application/pdf", True, runner=ok) == (None, "pdf_unreadable")  # truncated

    def missing(argv, **kw):
        raise FileNotFoundError("pdftotext")

    assert extract_text(b"%PDF-1.7", None, False, runner=missing) == (None, "pdf_unreadable")
    bad = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"err")
    assert extract_text(b"%PDF-1.7", None, False, runner=bad) == (None, "pdf_unreadable")
    empty = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=b"  \n", stderr=b"")
    assert extract_text(b"%PDF-1.7", None, False, runner=empty) == (None, "pdf_unreadable")


def test_extract_text_non_text_and_octet_stream_text():
    assert extract_text(b"\x89PNG\r\n\x1a\n\x00\x00", "image/png", False) == (None, "image")  # fix round 3, K7
    assert extract_text(b"abc\x00def", "text/html", False) == (None, "non_text")
    assert extract_text(b"plain", "audio/mpeg", False) == (None, "non_text")
    assert extract_text(bytes(range(128, 256)) * 4, "application/zip", False) == (None, "non_text")
    text, skipped = extract_text(b"Report a vulnerability: psirt@x.org", "application/octet-stream", False)
    assert text == "Report a vulnerability: psirt@x.org" and skipped is None
    assert extract_text(b"<html>hi</html>", "application/xhtml+xml", False)[0] == "hi"
    assert extract_text(b'{"a": 1}', "application/json", False)[1] is None


def test_run_judging_pdf_and_png_rows(tmp_path):
    pdf = b"%PDF-1.4 fake"
    png = b"\x89PNG\x00\x01"
    with Store(tmp_path / "crawl.sqlite") as store:
        r1 = _rec("a.org", "https://a.org/p.pdf", "candidate", None)
        r1.content_type = "application/pdf"
        store.record(r1, pdf)
        r2 = _rec("b.org", "https://b.org/x.png", "probe", None)
        r2.content_type = "image/png"
        store.record(r2, png)
        r3 = _rec("c.org", "https://c.org/t.pdf", "probe", None)
        r3.content_type = "application/pdf"
        r3.truncated = True
        store.record(r3, pdf + b" truncated")
        out = tmp_path / "j.jsonl"
        runner = lambda argv, **kw: subprocess.CompletedProcess(
            argv, 0, stdout=b"To report a security vulnerability email security@x.org. " + b"Thank you. " * 20, stderr=b"")
        ask = FakeAsk({A: [reply(True, True)], B: [reply(True, True)]})
        counts = run_judging(store, out, ask=ask, pdf_runner=runner)
        rows = {r["domain"]: r for r in _rows(out)}
        assert rows["a.org"]["skipped"] is None and rows["a.org"]["final"]["contact_method"] is True
        assert rows["b.org"]["skipped"] == "image" and rows["b.org"]["prefilter"] is None and rows["b.org"]["final"] is None
        assert rows["c.org"]["skipped"] == "pdf_unreadable" and rows["c.org"]["a"] is None
        assert counts["image"] == 1 and counts["pdf_unreadable"] == 1 and counts["judged"] == 1


def test_resume_ignores_truncated_last_line(tmp_path):
    out = tmp_path / "j.jsonl"
    with Store(tmp_path / "crawl.sqlite") as store:
        store.record(_rec("a.org", "https://a.org/p", "candidate", None), POLICY)
        store.record(_rec("b.org", "https://b.org/p", "candidate", None), b"<p>Visiting hours</p>" + FILLER)
        run_judging(store, out, ask=FakeAsk({A: [reply(True, True)], B: [reply(True, True)]}))
        lines = out.read_text().splitlines()
        out.write_text(lines[0] + "\n" + lines[1][:20])  # killed mid-write
        counts = run_judging(store, out, ask=FakeAsk({}))
        assert counts["already_done"] == 1 and counts["prefiltered_out"] == 1
        good = [json.loads(x) for x in out.read_text().splitlines() if x.endswith("}")]
        assert {r["domain"] for r in good} == {"a.org", "b.org"}  # the cut line stays, ignored


# ---- B8: a long judging run stops on a usage limit or an outage

def _pages(store, n, start=0):
    for i in range(start, start + n):
        store.record(_rec(f"d{i}.org", f"https://d{i}.org/p", "candidate", None),
                     POLICY + f"<p>page {i}</p>".encode())


def test_judging_stops_after_five_failed_pages_in_a_row_and_resumes(tmp_path):
    from noroshi.judge import JudgingStopped

    out = tmp_path / "j.jsonl"
    with Store(tmp_path / "crawl.sqlite") as store:
        _pages(store, 1)
        _pages(store, 7, start=1)
        down = RuntimeError("usage limit reached")
        ask = FakeAsk({A: [reply(True, True)] + [down] * 20, B: [reply(True, True)]})
        with pytest.raises(JudgingStopped, match="5 pages in a row") as stop:
            run_judging(store, out, ask=ask)
        assert stop.value.counts["judged"] == 1 and stop.value.counts["failed"] == 5
        assert len(ask.calls) == 2 + 5 * 2  # one good page, then five pages that each failed twice; nothing more
        assert [r["domain"] for r in _rows(out)] == ["d0.org"]
        counts = run_judging(store, out, ask=FakeAsk({A: [reply(False, False)] * 7, B: [reply(False, False)] * 7}))
        assert counts["already_done"] == 1 and counts["judged"] == 7 and len(_rows(out)) == 8


def test_a_judged_page_resets_the_count_of_failed_pages(tmp_path):
    out = tmp_path / "j.jsonl"
    with Store(tmp_path / "crawl.sqlite") as store:
        _pages(store, 9)
        down = RuntimeError("outage")
        ask = FakeAsk({A: [down] * 8 + [reply(True, True)] + [down] * 8, B: [reply(True, True)]})
        counts = run_judging(store, out, ask=ask)
        assert counts["failed"] == 8 and counts["judged"] == 1


def test_judge_command_exits_with_a_clear_message_when_judging_stops(tmp_path, monkeypatch):
    from noroshi import cli, judge

    monkeypatch.setenv("NOROSHI_ROOT", str(tmp_path))
    raw = tmp_path / "data" / "raw"
    with Store(raw / "crawl.sqlite") as store:
        _pages(store, 6)
    orig = judge.run_judging
    monkeypatch.setattr(judge, "run_judging", lambda store, out, **kw: orig(
        store, out, ask=FakeAsk({A: [RuntimeError("limit")] * 12}), **kw))
    with pytest.raises(SystemExit) as e:
        cli.main(["judge"])
    assert "5 pages in a row" in str(e.value) and "noroshi judge" in str(e.value)


def test_only_a_page_that_was_read_is_judged(tmp_path):
    """Closing fixes, C2: a 200 record is judged only when it is an answer (it completed and was not refused)."""
    page = b"<p>To report a security vulnerability email security@x.org</p>"
    with Store(tmp_path / "crawl.sqlite") as store:
        store.record(_rec("a.org", "https://a.org/x", "probe", None, status=202), page + b"a")
        failed = _rec("b.org", "https://b.org/x", "probe", None)
        failed.error = "internal"
        store.record(failed, page + b"b")
        ask = FakeAsk({A: [reply(True, True)], B: [reply(True, True)]})
        counts = run_judging(store, tmp_path / "judgements.jsonl", ask=ask)
    assert ask.calls == [] and counts["judged"] == counts["failed"] == 0


# ---- post-pilot Y2: what reaches the judge

from pathlib import Path  # noqa: E402

PILOT = Path(__file__).parent / "fixtures" / "pilot"


def _page(store, domain, url, body, purpose="probe", final=None, pass_no=1, truncated=False, ctype="text/html"):
    rec = _rec(domain, url, purpose, None)
    rec.final_url, rec.pass_no, rec.truncated, rec.content_type = final or url, pass_no, truncated, ctype
    store.record(rec, body)


def test_the_prefilter_takes_the_two_policy_wordings_the_pilot_pages_use():
    assert prefilter("Vulnerability report") and prefilter("Submit a vulnerabilities report here")
    assert prefilter("Coordinated Vulnerability Reporting") and prefilter("coordinated  vulnerability handling")
    assert not prefilter("Security") and not prefilter("Trust Center") and not prefilter("Cybersecurity")


def test_a_page_whose_only_wording_is_one_of_the_new_phrases_reaches_the_judge(tmp_path):
    pages = {"a.org": b"<h1>Vulnerability report</h1>", "b.org": b"<h1>Coordinated Vulnerability Reporting</h1>",
             "c.org": b"<h1>Security</h1>"}
    with Store(tmp_path / "crawl.sqlite") as store:
        for d, head in pages.items():
            _page(store, d, f"https://{d}/x", head + FILLER, purpose="candidate")
        ask = FakeAsk({A: [reply(True, True)] * 2, B: [reply(True, True)] * 2})
        counts = run_judging(store, tmp_path / "j.jsonl", ask=ask)
    rows = {r["domain"]: r for r in _rows(tmp_path / "j.jsonl")}
    assert rows["a.org"]["prefilter"] is True and rows["b.org"]["prefilter"] is True
    assert rows["c.org"]["prefilter"] is False and rows["c.org"]["final"] is None
    assert counts["judged"] == 2 and counts["prefiltered_out"] == 1 and len(ask.calls) == 4


def test_the_six_grants_gov_paths_are_a_catch_all_page_and_none_is_judged(tmp_path):
    """Audit C1: grants.gov answered 200 at six conventional paths with one 999-character shell whose footer links the
    HHS policy; the old judges called five of them policy pages."""
    shell = (PILOT / "grants_probe_shell.html").read_bytes()
    assert len(visible_text(shell)) == 999 and prefilter(visible_text(shell))
    with Store(tmp_path / "crawl.sqlite") as store:
        for path in ("/vulnerability-disclosure-policy", "/responsible-disclosure", "/security",
                     "/vulnerability-disclosure", "/product-security", "/psirt"):
            # the stored bodies differ in bytes (each names its own path), not in visible text
            _page(store, "grants.gov", f"https://grants.gov{path}", shell + f"<!-- {path} -->".encode())
        counts = run_judging(store, tmp_path / "j.jsonl", ask=FakeAsk({}))
    rows = _rows(tmp_path / "j.jsonl")
    assert len(rows) == 6 and {r["skipped"] for r in rows} == {"catch_all"}
    assert all(r["final"] is None and r["a"] is None for r in rows)
    assert counts["catch_all"] == 6 and counts["judged"] == 0


def test_a_catch_all_needs_two_final_urls_in_one_domain_and_pass(tmp_path):
    shell = (PILOT / "grants_probe_shell.html").read_bytes()
    with Store(tmp_path / "crawl.sqlite") as store:
        # one domain: two paths redirected to one address are one page, not a catch-all
        _page(store, "a.org", "https://a.org/security", shell, final="https://a.org/home")
        _page(store, "a.org", "https://a.org/psirt", shell, final="https://a.org/home")
        # the same text at one path of each of two domains, and at one path in each of two passes
        _page(store, "b.org", "https://b.org/security", shell)
        _page(store, "c.org", "https://c.org/security", shell)
        _page(store, "d.org", "https://d.org/security", shell, pass_no=1)
        _page(store, "d.org", "https://d.org/psirt", shell, pass_no=2)
        # a candidate and a conventional path with the same text: only conventional paths make a catch-all
        _page(store, "e.org", "https://e.org/security", shell)
        _page(store, "e.org", "https://e.org/legal/vdp", shell, purpose="candidate")
        ask = FakeAsk({A: [reply(False, False)], B: [reply(False, False)]})
        counts = run_judging(store, tmp_path / "j.jsonl", ask=ask)
    assert counts["catch_all"] == 0 and counts["judged"] == 8 and len(ask.calls) == 2


def test_pages_with_identical_text_share_one_verdict_even_when_their_bytes_differ(tmp_path):
    """Audit M7: two HHS policy bodies with the same extracted text got different answers from one model."""
    text = POLICY
    one, two = b"<html><head><script>var v=1;</script></head>" + text, b"<html><!-- build 2 -->" + text
    with Store(tmp_path / "crawl.sqlite") as store:
        _page(store, "hhs.gov", "https://hhs.gov/vulnerability-disclosure-policy", one)
        _page(store, "hhs.gov", "https://hhs.gov/vulnerability-disclosure-policy/index.html", two, purpose="candidate")
        ask = FakeAsk({A: [reply(True, True, True)], B: [reply(True, True, True)]})
        counts = run_judging(store, tmp_path / "j.jsonl", ask=ask)
        assert len(ask.calls) == 2 and counts["judged"] == 2
        rows = _rows(tmp_path / "j.jsonl")
        assert rows[0]["body_sha256"] != rows[1]["body_sha256"]
        assert rows[0]["text_sha256"] == rows[1]["text_sha256"] and rows[0]["final"] == rows[1]["final"]
        # a later run: a third body with the same text takes the verdict from the file
        _page(store, "x.gov", "https://x.gov/vdp", b"<div>" + text + b"</div>", purpose="candidate")
        ask = FakeAsk({})
        assert run_judging(store, tmp_path / "j.jsonl", ask=ask)["judged"] == 1 and ask.calls == []


def test_the_va_gov_page_with_eight_visible_characters_is_unreadable(tmp_path):
    """Audit I5: the stored va.gov Policy: page is a meta refresh with 8 visible characters. In the pilot store its
    target was never requested, so nothing stands in for it: unreadable, not a clear negative."""
    body = (PILOT / "va_policy_refresh.html").read_bytes()
    with Store(tmp_path / "crawl.sqlite") as store:
        _page(store, "va.gov", "https://www.va.gov/vulnerability-disclosure-policy/", body, purpose="policy_field")
        counts = run_judging(store, tmp_path / "j.jsonl", ask=FakeAsk({}))
    row, = _rows(tmp_path / "j.jsonl")
    assert row["skipped"] == "unreadable" and row["prefilter"] is None and counts["unreadable"] == 1


def test_a_followed_meta_refresh_page_is_neither_judged_nor_unreadable(tmp_path):
    """Its target's record stands in for it (part X follows the refresh)."""
    body = (PILOT / "va_policy_refresh.html").read_bytes()
    with Store(tmp_path / "crawl.sqlite") as store:
        _page(store, "va.gov", "https://www.va.gov/vulnerability-disclosure-policy/", body, purpose="policy_field")
        _page(store, "va.gov", "https://digital.va.gov/vulnerability-disclosure-policy/", POLICY,
              purpose="policy_field")
        ask = FakeAsk({A: [reply(True, True)], B: [reply(True, True)]})
        counts = run_judging(store, tmp_path / "j.jsonl", ask=ask)
    rows = {r["url"]: r for r in _rows(tmp_path / "j.jsonl")}
    assert rows["https://www.va.gov/vulnerability-disclosure-policy/"]["skipped"] == "meta_refresh"
    assert rows["https://digital.va.gov/vulnerability-disclosure-policy/"]["final"]["invites_reports"] is True
    assert counts["meta_refresh"] == 1 and counts["unreadable"] == 0 and counts["judged"] == 1


def test_a_page_cut_at_the_cap_that_fails_the_prefilter_is_unreadable(tmp_path):
    """Audit I5: modmed.com's Policy: page was cut at 1 MiB with nothing visible but its title."""
    cut = (PILOT / "modmed_security_cut.html").read_bytes()
    with Store(tmp_path / "crawl.sqlite") as store:
        _page(store, "modmed.com", "https://modmed.com/security/", cut, purpose="policy_field", truncated=True)
        _page(store, "long.org", "https://long.org/security", b"<p>About our campus</p>" + FILLER, truncated=True)
        _page(store, "kept.org", "https://kept.org/security", POLICY, truncated=True)
        _page(store, "whole.org", "https://whole.org/security", b"<p>About our campus</p>" + FILLER)
        ask = FakeAsk({A: [reply(True, True)], B: [reply(True, True)]})
        counts = run_judging(store, tmp_path / "j.jsonl", ask=ask)
    rows = {r["domain"]: r for r in _rows(tmp_path / "j.jsonl")}
    assert rows["modmed.com"]["skipped"] == rows["long.org"]["skipped"] == "unreadable"
    assert rows["kept.org"]["skipped"] is None and rows["kept.org"]["final"]["invites_reports"] is True
    assert rows["whole.org"]["skipped"] is None and rows["whole.org"]["prefilter"] is False  # whole: a clear negative
    assert counts["unreadable"] == 2


def test_a_short_page_that_names_a_policy_is_still_unreadable(tmp_path):
    with Store(tmp_path / "crawl.sqlite") as store:
        _page(store, "a.org", "https://a.org/vdp", SHORT_POLICY, purpose="candidate")  # 54 visible characters
        counts = run_judging(store, tmp_path / "j.jsonl", ask=FakeAsk({}))
    assert counts["unreadable"] == 1 and _rows(tmp_path / "j.jsonl")[0]["skipped"] == "unreadable"


# ---- fix round 3, K8: a pdftotext ended by a signal, or a stop, writes no judgement row

PDF_TEXT = b"To report a security vulnerability email security@x.org. " + b"Thank you for your report. " * 10


def test_pdftotext_runs_in_its_own_session_so_a_terminals_ctrl_c_does_not_reach_it():
    seen = {}

    def runner(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0, stdout=PDF_TEXT, stderr=b"")
    assert extract_text(b"%PDF-1.7", "application/pdf", False, runner=runner)[1] is None
    assert seen.get("start_new_session") is True


def _one_pdf(store):
    r = _rec("a.org", "https://a.org/vdp.pdf", "candidate", None)
    r.content_type = "application/pdf"
    store.record(r, b"%PDF-1.4 fake")


def test_a_pdftotext_ended_by_a_signal_after_a_stop_writes_no_row_and_the_page_is_read_on_the_next_run(tmp_path):
    """K8, narrowed in fix round 4 (N3): only with a stop requested."""
    from noroshi import llm

    with Store(tmp_path / "crawl.sqlite") as store:
        _one_pdf(store)
        out = tmp_path / "j.jsonl"

        def stopped(argv, **kw):  # the signal that asked the run to stop also ended the helper
            llm._stop.set()
            return subprocess.CompletedProcess(argv, -2, stdout=b"", stderr=b"")
        try:
            with pytest.raises(llm.CallsStopped):
                run_judging(store, out, ask=FakeAsk({}), pdf_runner=stopped)
        finally:
            llm._stop.clear()
        assert not (out.exists() and _rows(out))
        ok = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=PDF_TEXT, stderr=b"")  # noqa: E731
        counts = run_judging(store, out, ask=FakeAsk({A: [reply(True, True)], B: [reply(True, True)]}), pdf_runner=ok)
        row, = _rows(out)
        assert counts["judged"] == 1 and row["skipped"] is None and row["final"]["invites_reports"] is True


def test_a_pdftotext_that_crashes_on_its_own_is_pdf_unreadable(tmp_path):
    """Fix round 4, N3: a helper that died by a signal with no stop requested (a malformed PDF) is pdf_unreadable."""
    with Store(tmp_path / "crawl.sqlite") as store:
        _one_pdf(store)
        out = tmp_path / "j.jsonl"
        crashed = lambda argv, **kw: subprocess.CompletedProcess(argv, -11, stdout=b"", stderr=b"")  # noqa: E731
        counts = run_judging(store, out, ask=FakeAsk({}), pdf_runner=crashed)
        assert counts["pdf_unreadable"] == 1 and [r["skipped"] for r in _rows(out)] == ["pdf_unreadable"]


def test_no_row_is_written_once_a_stop_was_requested(tmp_path, monkeypatch):
    from noroshi import llm

    with Store(tmp_path / "crawl.sqlite") as store:
        store.record(_rec("a.org", "https://a.org/p", "candidate", None),
                     b"<p>To report a security vulnerability email security@x.org</p>" + FILLER)
        out = tmp_path / "j.jsonl"

        def ask_then_stop(prompt, model, system, purpose, **kw):  # the signal lands while the second call is out
            if model == B:
                llm._stop.set()
            return SimpleNamespace(text=reply(True, True))
        try:
            with pytest.raises(llm.CallsStopped):
                run_judging(store, out, ask=ask_then_stop)
        finally:
            llm._stop.clear()
        assert not (out.exists() and _rows(out))  # the page is asked again on the next run
