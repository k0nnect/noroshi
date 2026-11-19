"""B1: an opt-out that arrives after measurement is honored, and `noroshi purge` deletes what was collected."""
import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

import synth
from noroshi import cli, config
from noroshi.judge import run_judging
from noroshi.outcomes import derive_domains, wayback_inputs
from noroshi.store import FetchRecord, Store


def _derive(synthetic, optout):
    store, frame, judg, reg, wb = synthetic
    return derive_domains(store, frame, judg, reg, wb, synth.NOW, optout=optout).set_index("domain")


@pytest.mark.parametrize("line", ["good.org", "https://www.good.org/", "www.good.org"])
def test_a_measured_domain_opted_out_later_is_optout_with_empty_outcomes(synthetic, tmp_path, line):
    (tmp_path / "optout.txt").write_text(f"# arrived after pass 1\n{line}\n")
    d = _derive(synthetic, cli.load_optout(tmp_path / "optout.txt"))
    r = d.loc["good.org"]
    assert r.status == "optout" and r.main is None and r.strict is None and r.broad is None and r.listing is None
    assert r.sectxt_class == "unmeasured" and r.contact_types == "" and r.policy_page == "unmeasured"
    assert r.ua_kind == "" and r.diodb is None and r.cna is None and r.first_seen == ""
    assert d.loc["root.org", "status"] == "measured" and d.loc["root.org", "main"] is True
    first, _ = wayback_inputs(d.reset_index())
    assert "good.org" not in set(first.domain) and "root.org" in set(first.domain)


def test_a_domain_whose_final_host_is_opted_out_is_optout(synthetic):
    store = synthetic[0]
    synth.rec(store, "root.org", "homepage", synth.HOME, ctype="text/html", url="https://root.org/",
              final="https://www.parent-system.com/", pass_no=2)
    store.mark_done("root.org", 2, "measured")
    d = _derive(synthetic, frozenset({"parent-system.com"}))  # the registrable domain of its final host
    assert d.loc["root.org", "status"] == "optout" and d.loc["root.org", "main"] is None


def test_pages_of_an_opted_out_domain_are_not_judged(tmp_path):
    page = (b"<p>To report a security vulnerability email security@x.org</p>"
            + b"<p>We thank every researcher who writes to us in good faith about our systems.</p>" * 3)
    reply = json.dumps({"invites_reports": True, "contact_method": True, "safe_harbor": False})
    with Store(tmp_path / "s.sqlite") as store:
        for d in ("a.org", "b.org", "c.org"):
            synth.rec(store, d, "homepage", synth.HOME, ctype="text/html", url=f"https://{d}/",
                      final="https://www.optedparent.com/" if d == "c.org" else None)
            synth.rec(store, d, "candidate", page + d.encode(), ctype="text/html", url=f"https://{d}/vdp")
        out = tmp_path / "j.jsonl"
        counts = run_judging(store, out, ask=lambda *a, **k: SimpleNamespace(text=reply),
                             optout=frozenset({"a.org", "optedparent.com"}))
    assert {json.loads(x)["domain"] for x in out.read_text().splitlines()} == {"b.org"}
    assert counts["opted_out"] == 2 and counts["judged"] == 1


def test_purging_a_site_keeps_a_listed_domain_that_landed_there_opted_out(tmp_path):
    """Closing fixes, C3: purging site S deletes the homepage record of a listed domain that redirected to S; that
    domain (opted out through S) must still be reported as opted out, and its other pages are not judged."""
    from noroshi.optout import purge

    page = (b"<p>To report a security vulnerability email security@x.org</p>"
            + b"<p>We thank every researcher who writes to us in good faith about our systems.</p>" * 3)
    frame = pd.DataFrame([{"sector": "hospital", "domain": "alias.org", "start_url": "https://alias.org/",
                           "n_units": "1", "beds_sum": "", "name_tokens": ""}])
    reg = synth.Registries(platforms={}, diodb=set(), cna={"alias.org"})
    optout = frozenset({"hub.org"})
    with Store(tmp_path / "s.sqlite") as store:
        synth.rec(store, "alias.org", "homepage", synth.HOME, ctype="text/html", url="https://alias.org/",
                  final="https://www.hub.org/")
        synth.rec(store, "alias.org", "sectxt_wellknown", None, status=404,
                  url="https://www.hub.org/.well-known/security.txt")
        synth.rec(store, "alias.org", "candidate", page, ctype="text/html", url="https://other.org/vdp")
        store.mark_done("alias.org", 1, "measured")

        def status():
            d = derive_domains(store, frame, tmp_path / "none.jsonl", reg, tmp_path / "w.jsonl", synth.NOW,
                               optout=optout)
            return d.loc[0, "status"], d.loc[0, "broad"]
        assert status() == ("optout", None)
        purge(store, "hub.org", tmp_path / "j.jsonl", [], tmp_path / "llm.jsonl")
        assert [r.url for r in store.requests_for("alias.org", 1)] == ["https://other.org/vdp"]
        assert status() == ("optout", None)
        counts = run_judging(store, tmp_path / "j.jsonl", ask=lambda *a, **k: 1 / 0, optout=optout)
    assert counts["opted_out"] == 1 and counts["judged"] == 0


# ---- the commands

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NOROSHI_ROOT", str(tmp_path))
    p = config.paths()
    for d in (p.raw, p.frames, p.info, p.registries):
        d.mkdir(parents=True, exist_ok=True)
    store, frame, judg, reg = synth.build(tmp_path)
    shared = b"<p>shared body</p>"
    store.record(FetchRecord("good.org", "hospital", "good.org", "https://good.org/same", "probe", 1, "identified",
                             "2026-11-20T00:00:00Z", 200, "https://good.org/same", [], "text/html", None, 0, False,
                             None, False), shared)
    store.record(FetchRecord("root.org", "hospital", "root.org", "https://root.org/same", "probe", 1, "identified",
                             "2026-11-20T00:00:00Z", 200, "https://root.org/same", [], "text/html", None, 0, False,
                             None, False), shared)
    store.close()
    (tmp_path / "s.sqlite").rename(p.raw / "crawl.sqlite")
    (tmp_path / "blobs").rename(p.raw / "blobs")
    for f in ("s.sqlite-wal", "s.sqlite-shm"):
        if (tmp_path / f).exists():
            (tmp_path / f).unlink()
    rows = [json.loads(x) for x in judg.read_text().splitlines()]
    rows.append({**rows[0], "domain": "good.org", "url": "https://good.org/x", "body_sha256": "9"})
    (p.raw / "judgements.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (p.raw / "wayback.jsonl").write_text(
        json.dumps({"domain": "good.org", "host": "good.org", "path": "/.well-known/security.txt",
                    "first_seen": "2020-01-01"}) + "\n"
        + json.dumps({"domain": "root.org", "host": "root.org", "path": "/security.txt", "first_seen": None}) + "\n")
    (p.raw / "wayback_redirects.jsonl").write_text(
        json.dumps({"domain": "good.org", "host": "good.org", "redirect_target": None}) + "\n")
    frame.to_csv(p.frames / "hospital_domains.csv", index=False)
    pd.DataFrame([{"hifld_id": "1", "name": "A", "state": "TX", "type": "GENERAL ACUTE CARE", "owner": "NON-PROFIT",
                   "beds": "100", "rural": "True", "domain": "good.org", "exclusion": ""}]).to_csv(
        p.frames / "hospitals.csv", index=False)
    (p.registries / "program-list.json").write_text("[]")
    (p.registries / "CNAsList.json").write_text("[]")
    for name in ("hackerone", "bugcrowd", "intigriti", "yeswehack"):
        (p.registries / f"{name}_data.json").write_text("[]")
    return p


def test_derive_and_judge_commands_read_the_optout_file(env, monkeypatch):
    from noroshi import judge

    (env.info / "optout.txt").write_text("https://www.good.org/\n")
    cli.main(["derive"])
    d = pd.read_csv(env.derived / "domains.csv", dtype=str, keep_default_na=False).set_index("domain")
    assert d.loc["good.org", "status"] == "optout" and d.loc["good.org", "main"] == ""
    first = pd.read_csv(env.derived / "wayback_first_seen.csv")
    assert "good.org" not in set(first.domain)
    seen = {}
    orig = judge.run_judging
    monkeypatch.setattr(judge, "run_judging", lambda store, out, **kw: seen.update(kw) or orig(
        store, out, ask=lambda *a, **k: SimpleNamespace(text="{}"), **kw))
    (env.raw / "judgements.jsonl").unlink()
    cli.main(["judge"])
    assert "good.org" in seen["optout"]
    assert "good.org" not in {json.loads(x)["domain"] for x in (env.raw / "judgements.jsonl").read_text().splitlines()}


def test_wayback_command_skips_opted_out_rows(env, monkeypatch, capsys):
    from noroshi import wayback

    (env.info / "optout.txt").write_text("www.x.org\n")
    monkeypatch.setattr(wayback, "throttled", lambda get, **kw: get)
    asked = []
    monkeypatch.setattr(wayback.httpx, "get", lambda url, **kw: asked.append(kw["params"]["url"]) or
                        SimpleNamespace(status_code=200, json=lambda: [], text="[]"))
    src = env.root / "t.csv"
    src.write_text("domain,host,path\nx.org,www.x.org,/a\ny.org,www.y.org,/b\n")
    cli.main(["wayback", "--first-seen", str(src)])
    assert asked == ["www.y.org/b"] and "opted out: 1" in capsys.readouterr().out


def test_purge_removes_records_blobs_judgements_and_archive_rows(env, capsys):
    (env.info / "optout.txt").write_text("good.org\n")
    with Store(env.raw / "crawl.sqlite") as s:
        n_records = s.db.execute("SELECT COUNT(*) FROM requests WHERE domain = 'good.org'").fetchone()[0]
        own = {r[0] for r in s.db.execute("SELECT body_sha256 FROM requests WHERE domain = 'good.org' AND body_sha256 "
                                          "NOT IN (SELECT body_sha256 FROM requests WHERE domain != 'good.org' AND "
                                          "body_sha256 IS NOT NULL)")}
        kept = {r[0] for r in s.db.execute("SELECT body_sha256 FROM requests WHERE domain = 'good.org' AND "
                                           "body_sha256 IS NOT NULL")} - own
        others = s.db.execute("SELECT COUNT(*) FROM requests WHERE domain != 'good.org'").fetchone()[0]
    assert n_records == 5 and own and kept  # e.g. its security.txt is its own; the shared probe body is not
    cli.main(["purge", "good.org"])
    out = capsys.readouterr().out
    with Store(env.raw / "crawl.sqlite") as s:
        assert s.db.execute("SELECT COUNT(*) FROM requests WHERE domain = 'good.org'").fetchone()[0] == 0
        assert s.db.execute("SELECT COUNT(*) FROM requests WHERE domain != 'good.org'").fetchone()[0] == others
        assert s.done_status("good.org", 1) is None
    assert not any((env.raw / "blobs" / sha).exists() for sha in own)
    assert all((env.raw / "blobs" / sha).exists() for sha in kept)  # another domain's record still holds them
    judged = (env.raw / "judgements.jsonl").read_text()
    assert "good.org" not in judged and "policyonly.org" in judged
    assert "good.org" not in (env.raw / "wayback.jsonl").read_text() and "root.org" in (env.raw / "wayback.jsonl").read_text()
    assert (env.raw / "wayback_redirects.jsonl").read_text() == ""
    assert f"{n_records} records" in out and f"{len(own)} blobs" in out
    assert "1 judgement rows" in out and "2 internet archive rows" in out and "derive" in out


def test_purge_refuses_a_domain_that_is_not_opted_out(env):
    with pytest.raises(SystemExit) as e:
        cli.main(["purge", "good.org"])
    assert "optout.txt" in str(e.value)
    with Store(env.raw / "crawl.sqlite") as s:
        assert s.requests_for("good.org", 1)


def test_purge_of_the_pilot_store_touches_only_pilot_files(env):
    (env.info / "optout.txt").write_text("good.org\n")
    (env.raw / "crawl.sqlite").rename(env.raw / "pilot.sqlite")
    (env.raw / "blobs").rename(env.raw / "pilot_blobs")
    for name in ("judgements.jsonl", "wayback.jsonl", "wayback_redirects.jsonl"):
        (env.raw / name).rename(env.raw / f"pilot_{name}")
    (env.raw / "judgements.jsonl").write_text(json.dumps({"domain": "good.org", "url": "u", "body_sha256": "s"}) + "\n")
    cli.main(["purge", "good.org", "--store", "pilot"])
    assert "good.org" not in (env.raw / "pilot_judgements.jsonl").read_text()
    assert "good.org" in (env.raw / "judgements.jsonl").read_text()  # the main files are not the pilot's
    assert not (env.raw / "crawl.sqlite").exists()
    assert "purged good.org from pilot.sqlite" in (env.raw / "pilot_purge.log").read_text()
    assert not (env.raw / "purge.log").exists()


# ---- rulings round: purge also clears the model-call log, and writes a purge log

def _log_calls(calls):
    """Write real model-call log rows through judge_page and llm.ask_claude, with a fake runner (no model call)."""
    import subprocess

    from noroshi import llm
    from noroshi.config import JUDGE_MODELS, JUDGE_TIEBREAK
    from noroshi.judge import judge_page

    verdict = json.dumps({"invites_reports": True, "contact_method": True, "safe_harbor": False})

    def runner(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, json.dumps({"result": verdict, "is_error": False}), "")

    def ask(prompt, model, system, purpose, **kw):
        return llm.ask_claude(prompt, model, system, purpose, runner=runner, meta=kw.get("meta"))

    for kind, arg in calls:
        if kind == "resolve":  # a resolution-pass row: a company name and a website, not page content
            ask(f"Which website does the company {arg} use?", JUDGE_MODELS[0], "s", "resolve_memory")
        elif kind == "old":  # a judge row written before post-pilot Y1: the page's address opened the prompt
            row = {"purpose": "judge_policy", "model": JUDGE_MODELS[1], "system": "s",
                   "prompt": f"URL: {arg}\nPage text (excerpt):\n<<<\nReport a vulnerability\n>>>",
                   "started_at": "2026-11-16T12:00:00+00:00", "response": verdict, "status": "ok", "duration_ms": 1}
            with config.paths().llm_log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        else:  # the page's address travels as meta: the requested URL, and the final URL after a redirect
            url, final = arg if isinstance(arg, tuple) else (arg, arg)
            judge_page(url, "To report a security vulnerability, café, write to sec@x.org",
                       JUDGE_TIEBREAK if kind == "tiebreak" else JUDGE_MODELS[0], ask=ask,
                       meta={"url": url, "final_url": final, "domain": "collector.org"})


def test_purge_removes_that_domains_judge_rows_from_the_model_log(env, capsys):
    (env.info / "optout.txt").write_text("good.org\n")
    with Store(env.raw / "crawl.sqlite") as s:  # its final host's site, seen in pass 2, which measured it
        synth.rec(s, "good.org", "homepage", synth.HOME, ctype="text/html", pass_no=2, url="https://good.org/",
                  final="https://www.good-parent.com/")
        s.mark_done("good.org", 2, "measured")
    _log_calls([("judge", "https://good.org/vdp"), ("judge", "https://root.org/vdp"),
                ("tiebreak", "https://www.good.org/a"), ("resolve", "Good Org (good.org)"),
                ("judge", "https://careers.good-parent.com/vdp"), ("judge", "https://thirdparty.org/vdp"),
                ("old", "https://good.org/legacy"), ("old", "https://root.org/legacy")])
    log = env.llm_log
    with log.open("ab") as fh:  # a judge row for good.org cut off by a killed run, written before meta existed
        fh.write(b'{"purpose": "judge_policy", "model": "m", "system": "s", "prompt": "URL: https://good.org/cut\\nPage')
    lines = log.read_bytes().splitlines(keepends=True)
    assert len(lines) == 9
    # fix round 2, H8: the call about a page on good-parent.com, where good.org's homepage landed, is not about good.org's
    # own site, so it stays (its verdict may serve other listed domains)
    kept = [ln for ln in lines if b"root.org" in ln or b"thirdparty.org" in ln or b"resolve_memory" in ln
            or b"good-parent.com" in ln]
    assert len(kept) == 5
    cli.main(["purge", "good.org"])
    assert log.read_bytes() == b"".join(kept)  # the other rows byte-identical and in order
    assert "4 model-log rows" in capsys.readouterr().out


def test_a_judge_row_cut_off_after_its_meta_is_still_found(env, capsys):
    """Post-pilot Y1: `meta` is written before the prompt, so a row cut off mid-prompt by a killed run still names
    its page."""
    (env.info / "optout.txt").write_text("good.org\n")
    _log_calls([("judge", "https://root.org/vdp"), ("judge", "https://good.org/vdp")])
    log = env.llm_log
    first, second = log.read_bytes().splitlines(keepends=True)
    assert second.index(b'"meta"') < second.index(b'"prompt"')
    log.write_bytes(first + second[:second.index(b'"prompt"') + 30])
    cli.main(["purge", "good.org"])
    assert log.read_bytes() == first and "1 model-log rows" in capsys.readouterr().out


def test_purge_takes_model_log_sites_only_from_passes_that_measured_the_domain(env, capsys):
    """Closing fixes, C1: a pass that ended on a third-party platform adds no site whose judge calls are dropped."""
    (env.info / "optout.txt").write_text("good.org\n")
    with Store(env.raw / "crawl.sqlite") as s:
        synth.rec(s, "good.org", "homepage", synth.HOME, ctype="text/html", pass_no=2, url="https://good.org/",
                  final="https://www.facebook.com/goodorg")
        s.mark_done("good.org", 2, "mismatch")
    _log_calls([("judge", "https://good.org/vdp"), ("judge", "https://www.facebook.com/someone-else/vdp")])
    cli.main(["purge", "good.org"])
    rows = [json.loads(x) for x in env.llm_log.read_text().splitlines()]
    assert [r["meta"]["url"] for r in rows] == ["https://www.facebook.com/someone-else/vdp"]
    assert "1 model-log rows" in capsys.readouterr().out


def test_the_purge_log_line_holds_the_counts(env, capsys):
    (env.info / "optout.txt").write_text("good.org\n")
    _log_calls([("judge", "https://good.org/vdp")])
    cli.main(["purge", "good.org"])
    printed = capsys.readouterr().out
    line, = (env.raw / "purge.log").read_text().splitlines()
    stamp, rest = line.split(" ", 1)
    assert datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
    counts = rest.split(": ", 1)[1]
    assert counts == ("5 records, 5 requests sent, 1 done markers, 0 browser-site rows, "
                      "1 blobs, 1 judgement rows, 0 identity rows, 2 internet archive rows, 1 model-log rows")
    assert rest.startswith("purged good.org from crawl.sqlite: ") and counts in printed


def test_purging_a_domain_with_no_data_is_a_clean_no_op_that_still_logs(env, capsys):
    (env.info / "optout.txt").write_text("nodata.org\n")
    _log_calls([("judge", "https://good.org/vdp")])
    files = [env.raw / n for n in ("judgements.jsonl", "wayback.jsonl", "wayback_redirects.jsonl", "llm_log.jsonl")]
    before = [f.read_bytes() for f in files]
    blobs = sorted(p.name for p in (env.raw / "blobs").iterdir())
    with Store(env.raw / "crawl.sqlite") as s:
        rows = s.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    cli.main(["purge", "nodata.org"])
    assert [f.read_bytes() for f in files] == before and sorted(p.name for p in (env.raw / "blobs").iterdir()) == blobs
    with Store(env.raw / "crawl.sqlite") as s:
        assert s.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == rows
    line, = (env.raw / "purge.log").read_text().splitlines()
    assert line.endswith("purged nodata.org from crawl.sqlite: 0 records, 0 requests sent, 0 done markers, "
                         "0 browser-site rows, 0 blobs, 0 judgement rows, 0 identity rows, "
                         "0 internet archive rows, 0 model-log rows")


# ---- post-pilot Y6: the purge path (closing re-review C3 and its minors, P1 to P5)

from noroshi.discover import PROBE_PATHS  # noqa: E402
from noroshi.optout import purge  # noqa: E402

POLICY_PAGE = (b"<p>To report a security vulnerability email security@s-parent.org</p>"
               + b"<p>We thank every researcher who writes to us in good faith about our systems.</p>" * 3)


def _linker(s, d="b.org"):
    """The re-reviewer's probe: listed domain b.org is measured; its /security redirects to site S (s-parent.org),
    whose page is judged a policy page."""
    synth.rec(s, d, "homepage", synth.HOME, ctype="text/html", url=f"https://{d}/", sector="device")
    synth.rec(s, d, "robots", b"User-agent: *\nDisallow: /admin\n", url=f"https://{d}/robots.txt", sector="device")
    for purpose, path in (("sectxt_wellknown", "/.well-known/security.txt"), ("sectxt_root", "/security.txt")):
        synth.rec(s, d, purpose, b"<html>nf</html>", status=404, ctype="text/html", url=f"https://{d}{path}",
                  sector="device")
    for p in PROBE_PATHS:
        if p == "/security":
            synth.rec(s, d, "probe", POLICY_PAGE, ctype="text/html", url=f"https://{d}{p}",
                      final="https://www.s-parent.org/security", sector="device")
        else:
            synth.rec(s, d, "probe", b"<html>nf</html>", status=404, ctype="text/html", url=f"https://{d}{p}",
                      sector="device")
    s.mark_done(d, 1, "measured")


def test_a_purged_conventional_path_is_candidate_skipped_not_a_budget_cut(tmp_path):
    """P1, probe_purged_probe.py: before this change the purged path was read as cut, and policy_page became no."""
    with Store(tmp_path / "s.sqlite") as s:
        _linker(s)
        page = next(r for r in s.requests_for("b.org", 1) if r.url == "https://b.org/security")
        judg = tmp_path / "j.jsonl"
        v = {"invites_reports": True, "contact_method": True, "safe_harbor": False}
        judg.write_text(json.dumps({"domain": "b.org", "sector": "device", "url": page.url,
                                    "body_sha256": page.body_sha256, "final": v, "a": v, "b": v, "agreed": True,
                                    "skipped": None}) + "\n")
        frame = pd.DataFrame([{"sector": "device", "domain": "b.org", "start_url": "https://b.org/", "n_units": "1",
                               "beds_sum": "", "name_tokens": "b"}])
        reg = synth.Registries(platforms={}, diodb=set(), cna=set())

        def row(optout):
            r = derive_domains(s, frame, judg, reg, tmp_path / "w.jsonl", synth.NOW, optout=optout).iloc[0]
            return r.policy_page, r.policy_look, r.probes_cut, r.broad
        assert row(frozenset()) == ("yes", "", 0, True)
        opt = frozenset({"s-parent.org"})
        assert row(opt) == ("unmeasured", "candidate_skipped", 0, None)
        purge(s, "s-parent.org", judg, [], tmp_path / "llm.jsonl")
        assert row(opt) == ("unmeasured", "candidate_skipped", 0, None)
        traces = s.db.execute("SELECT domain, pass_no, purpose, url_sha256 FROM purged").fetchall()
        assert [t[:3] for t in traces] == [("b.org", 1, "probe")]
        assert "s-parent" not in repr(traces) and "b.org/security" not in repr(traces)  # an address is kept hashed


def test_the_optout_markers_are_written_with_the_deletions_or_not_at_all(tmp_path):
    """P2: a kill between the deletions and the markers left an alias with no homepage and no optout marker."""
    import sqlite3

    with Store(tmp_path / "s.sqlite") as s:
        synth.rec(s, "alias.org", "homepage", synth.HOME, ctype="text/html", url="https://alias.org/",
                  final="https://www.hub.org/")
        s.mark_done("alias.org", 1, "measured")
        synth.rec(s, "hub.org", "homepage", synth.HOME, ctype="text/html", url="https://hub.org/")
        s.db.execute("CREATE TRIGGER killed BEFORE UPDATE ON done WHEN NEW.status = 'optout' "
                     "BEGIN SELECT RAISE(ABORT, 'killed'); END")
        with pytest.raises(sqlite3.IntegrityError, match="killed"):
            purge(s, "hub.org", tmp_path / "j.jsonl", [], tmp_path / "llm.jsonl")
        assert len(s.requests_for("alias.org", 1)) == 1 and len(s.requests_for("hub.org", 1)) == 1  # rolled back
        s.db.execute("DROP TRIGGER killed")
        purge(s, "hub.org", tmp_path / "j.jsonl", [], tmp_path / "llm.jsonl")
        assert s.requests_for("alias.org", 1) == [] and s.done_status("alias.org", 1) == "optout"


def test_purge_deletes_every_record_naming_the_site_and_keeps_another_sites_429_as_state(tmp_path):
    """P3, probe_purge_429.py, as ruled in fix round 1 (G1): b.org's candidate on S (s-old.org) redirected to T
    (t-system.org), which answered 429. Every record naming S goes; T's 429 and b.org's count stay as state."""
    from noroshi.fetch import PoliteFetcher

    with Store(tmp_path / "s.sqlite") as s:
        synth.rec(s, "b.org", "homepage", synth.HOME, ctype="text/html", url="https://b.org/")
        synth.rec(s, "b.org", "candidate", None, status=429, blocked=True, url="https://www.s-old.org/vdp",
                  final="https://www.t-system.org/vdp")
        synth.rec(s, "b.org", "candidate", b"no", status=403, blocked=True, url="https://www.s-old.org/psirt",
                  final="https://www.t-system.org/psirt")  # refused by T
        synth.rec(s, "b.org", "candidate", b"denied", status=403, blocked=True, url="https://www.s-old.org/x")  # by S
        synth.rec(s, "b.org", "probe", POLICY_PAGE, ctype="text/html", url="https://b.org/security",
                  final="https://www.s-old.org/security")
        live = tmp_path / "LIVE"
        live.write_text("x\n")
        assert PoliteFetcher(s, frozenset(), live).rate_limited("t-system.org", 1)
        sent, at_t = s.count_requests("b.org", 1), s.count_site_requests("t-system.org", 1)
        removed = purge(s, "s-old.org", tmp_path / "j.jsonl", [], tmp_path / "llm.jsonl")
        assert PoliteFetcher(s, frozenset({"s-old.org"}), live).rate_limited("t-system.org", 1)
        assert [(r.url, r.status) for r in s.requests_for("b.org", 1)] == [("https://b.org/", 200)]
        assert removed["records"] == 4 and s.count_requests("b.org", 1) == sent
        assert s.count_site_requests("t-system.org", 1) == at_t  # counted by requested site: S's, so 0 both times
        assert "s-old" not in repr(s.db.execute("SELECT * FROM purged_counts").fetchall())


def test_purge_deletes_the_sites_browser_row_and_every_record_whose_error_names_it(tmp_path):
    with Store(tmp_path / "s.sqlite") as s:
        s.mark_browser("gone.org", 1)
        s.mark_browser("kept.org", 1)
        for i, (url, detail) in enumerate([("https://www.gone.org/vdp", "ConnectError: www.gone.org refused"),
                                           ("https://kept.org/x", "ConnectError: [Errno 8] gone.org not known"),
                                           ("https://kept.org/y", "ReadTimeout: kept.org"),
                                           ("https://boss.org/z", "ConnectError: boss.org")]):
            synth.rec(s, "b.org", "candidate", None, status=None, error="connect", url=f"https://b.org/p{i}",
                      error_url=url, error_detail=detail)
        sent = s.count_requests("b.org", 1)
        removed = purge(s, "gone.org", tmp_path / "j.jsonl", [], tmp_path / "llm.jsonl")
        assert s.browser_sites(1) == {"kept.org"}
        got = [(r.url, r.error_url, r.error_detail) for r in s.requests_for("b.org", 1)]
        assert got == [("https://b.org/p2", "https://kept.org/y", "ReadTimeout: kept.org"),
                       ("https://b.org/p3", "https://boss.org/z", "ConnectError: boss.org")]
        assert (removed["records"], removed["browser-site rows"]) == (2, 1) and s.count_requests("b.org", 1) == sent


def test_purge_keeps_the_caps_and_the_429_a_resumed_pass_reads(tmp_path):
    """Review B, probe_own_429_and_caps.py: S's own record on T answered 429; listed domain X used its 12 requests,
    3 of them on S; S's 11 other records on T count toward T's site cap."""
    from noroshi.fetch import BudgetExceeded, PoliteFetcher, RateLimited, SiteBudgetExceeded

    with Store(tmp_path / "s.sqlite") as s:
        synth.rec(s, "s-gone.org", "homepage", synth.HOME, ctype="text/html", url="https://s-gone.org/")
        synth.rec(s, "s-gone.org", "candidate", None, status=429, blocked=True, url="https://www.t-host.org/vdp")
        for i in range(11):
            synth.rec(s, "s-gone.org", "candidate", b"<html>nf</html>", status=404, ctype="text/html",
                      url=f"https://www.t-host.org/p{i}")
        s.mark_done("s-gone.org", 1, "measured")
        synth.rec(s, "x.org", "homepage", synth.HOME, ctype="text/html", url="https://x.org/")
        for i in range(8):
            synth.rec(s, "x.org", "probe", b"<html>nf</html>", status=404, ctype="text/html", url=f"https://x.org/q{i}")
        for i in range(3):
            synth.rec(s, "x.org", "candidate", b"<html>nf</html>", status=404, ctype="text/html",
                      url=f"https://s-gone.org/c{i}")
        live = tmp_path / "LIVE"
        live.write_text("x\n")

        def state():
            return (PoliteFetcher(s, frozenset(), live).rate_limited("t-host.org", 1), s.count_requests("x.org", 1),
                    s.count_site_requests("t-host.org", 1))
        before = state()
        assert before == (True, 12, 12)
        purge(s, "s-gone.org", tmp_path / "j.jsonl", [], tmp_path / "llm.jsonl")
        assert state() == before
        rows = s.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        f = PoliteFetcher(s, frozenset({"s-gone.org"}), live)  # the pass resumed after the purge
        with pytest.raises(RateLimited):
            asyncio.run(f.fetch("other.org", "device", "https://www.t-host.org/new", "candidate", 1))
        with pytest.raises(BudgetExceeded):
            asyncio.run(f.fetch("x.org", "device", "https://x.org/more", "probe", 1))
        assert s.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == rows  # nothing was sent
        s.db.execute("DELETE FROM purged_counts WHERE kind = 'rate_limited'")
        with pytest.raises(SiteBudgetExceeded):  # T is at its site cap too
            asyncio.run(PoliteFetcher(s, frozenset(), live).fetch("other.org", "device", "https://www.t-host.org/n",
                                                                  "candidate", 1))


def test_a_record_that_only_passed_through_the_site_is_deleted(tmp_path):
    """Review B Minor 5: a redirect hop on S, with the requested and final URL elsewhere."""
    with Store(tmp_path / "s.sqlite") as s:
        synth.rec(s, "x.org", "candidate", b"<p>page</p>", ctype="text/html", url="https://x.org/a",
                  final="https://t.org/c", chain=["https://x.org/a", "https://www.s-gone.org/b"])
        synth.rec(s, "x.org", "candidate", b"<p>kept</p>", ctype="text/html", url="https://x.org/k",
                  final="https://t.org/k", chain=["https://x.org/k"])
        removed = purge(s, "s-gone.org", tmp_path / "j.jsonl", [], tmp_path / "llm.jsonl")
        assert [r.url for r in s.requests_for("x.org", 1)] == ["https://x.org/k"] and removed["records"] == 1
        assert len(s.purged("x.org", 1)) == 1


def test_a_kept_model_log_row_no_longer_names_the_purged_domain(env, capsys):
    """Review A Minor 6, as ruled (G1): the judge call about another site's page stays, the purged domain's name goes."""
    (env.info / "optout.txt").write_text("good.org\n")
    _log_calls([("judge", "https://hhs.gov/vdp")])
    log = env.llm_log
    row = json.loads(log.read_text())
    row["meta"]["domain"] = "good.org"  # good.org collected hhs.gov's page first
    log.write_text(json.dumps(row, ensure_ascii=False) + "\n")
    cli.main(["purge", "good.org"])
    after = json.loads(log.read_text())
    assert after["meta"] == {**row["meta"], "domain": ""} and after["prompt"] == row["prompt"]
    assert after["response"] == row["response"] and "good.org" not in log.read_text()


def test_purge_removes_the_judge_call_of_a_page_that_ended_on_the_site(env, capsys):
    """P5: a page requested elsewhere that redirected into the purged site is found by its final URL in meta."""
    (env.info / "optout.txt").write_text("good.org\n")
    _log_calls([("judge", ("https://other.org/vdp", "https://www.good.org/vdp")), ("judge", "https://other.org/x")])
    cli.main(["purge", "good.org"])
    assert [json.loads(x)["meta"]["url"] for x in env.llm_log.read_text().splitlines()] == ["https://other.org/x"]
    assert "1 model-log rows" in capsys.readouterr().out


def test_analyze_refuses_a_derived_table_older_than_the_purge_log(env):
    """P4: the purged requests would be counted twice (in the old table and in requests_sent_purged)."""
    import os

    (env.info / "optout.txt").write_text("good.org\n")
    cli.main(["derive"])
    cli.main(["purge", "good.org"])
    table, log = env.derived / "domains.csv", env.raw / "purge.log"
    os.utime(log, ns=(table.stat().st_mtime_ns + 1_000_000, table.stat().st_mtime_ns + 1_000_000))
    with pytest.raises(SystemExit) as e:
        cli.main(["analyze", "--allow-incomplete"])
    assert "purge" in str(e.value) and "noroshi derive" in str(e.value)
    assert not (env.results / "numbers.json").exists()
    cli.main(["derive"])
    cli.main(["analyze", "--allow-incomplete"])
    assert (env.results / "numbers.json").exists()


def test_purge_refuses_to_run_while_a_crawl_holds_the_store(env):
    import fcntl

    (env.info / "optout.txt").write_text("good.org\n")
    with open(env.raw / "crawl.sqlite.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SystemExit) as e:
            cli.main(["purge", "good.org"])
    assert "in use by another noroshi command" in str(e.value) and "nothing was removed" in str(e.value)
    with Store(env.raw / "crawl.sqlite") as s:
        assert s.requests_for("good.org", 1)


def _touch_every_way(store, raw):
    """A store and files in which site S (listed domain s-gone.org, host www.s-gone.org) is touched in every way purge
    handles. Returns the sha of the one page body of another site that names S (x.org's homepage links to S)."""
    S = "https://www.s-gone.org"
    policy = POLICY_PAGE.replace(b"s-parent.org", b"s-gone.org")
    # S's own records, one requested at a third site T that answered 429, and a page on S
    synth.rec(store, "s-gone.org", "homepage", synth.HOME + b"<!-- s-gone home -->", ctype="text/html",
              url="https://s-gone.org/", final=f"{S}/")
    synth.rec(store, "s-gone.org", "candidate", None, status=429, blocked=True, url="https://www.t-host.org/vdp")
    synth.rec(store, "s-gone.org", "probe", policy, ctype="text/html", url=f"{S}/security")
    store.mark_done("s-gone.org", 1, "measured")
    # x.org: a page that ended on S; one that passed through S to T, which answered 429; one T refused; one whose
    # chain only passes through S; one whose error names S
    home = synth.HOME.replace(b"</body>", b'<a href="https://www.s-gone.org/">our partner</a></body>')
    synth.rec(store, "x.org", "homepage", home, ctype="text/html", url="https://x.org/")
    synth.rec(store, "x.org", "probe", policy + b"x", ctype="text/html", url="https://x.org/security",
              final=f"{S}/security-x")
    synth.rec(store, "x.org", "candidate", None, status=429, blocked=True, url="https://x.org/a",
              final="https://www.t-host.org/a", chain=["https://x.org/a", f"{S}/a"])
    synth.rec(store, "x.org", "candidate", b"no", status=403, blocked=True, url="https://x.org/b",
              final="https://www.t-host.org/b", chain=["https://x.org/b", f"{S}/b"])
    synth.rec(store, "x.org", "candidate", b"<p>elsewhere</p>", ctype="text/html", url="https://x.org/c",
              final="https://www.t-host.org/c", chain=["https://x.org/c", f"{S}/c"])
    synth.rec(store, "x.org", "candidate", None, status=None, error="timeout", url="https://x.org/d",
              error_url=f"{S}/d", error_detail="ReadTimeout: www.s-gone.org")
    synth.rec(store, "x.org", "probe", b"<html>nf</html>", status=404, ctype="text/html", url="https://x.org/psirt")
    store.mark_done("x.org", 1, "measured")
    # a.org: its homepage landed on S
    synth.rec(store, "a.org", "homepage", synth.HOME, ctype="text/html", url="https://a.org/", final=f"{S}/a-home")
    store.mark_done("a.org", 1, "measured")
    store.mark_browser("s-gone.org", 1)
    store.mark_browser("t-host.org", 1)
    rows = {(r.domain, r.url): r for d in ("s-gone.org", "x.org") for r in store.requests_for(d, 1)}
    v = {"invites_reports": True, "contact_method": True, "safe_harbor": False}

    def jrow(domain, url):
        r = rows[(domain, url)]
        return {"domain": domain, "url": url, "body_sha256": r.body_sha256, "final": v, "a": v, "b": v, "skipped": None}
    (raw / "judgements.jsonl").write_text("".join(json.dumps(r) + "\n" for r in (
        jrow("s-gone.org", f"{S}/security"), jrow("x.org", "https://x.org/security"),
        jrow("x.org", "https://x.org/psirt"))))
    rater = {"model": "m", "site": "own", "reason": "r"}
    (raw / "identity.jsonl").write_text("".join(json.dumps({"domain": d, "page_sha256": d, "final_url": f, "a": rater,
                                                            "b": rater, "site": "own", "agreed": True}) + "\n"
                                                for d, f in (("s-gone.org", f"{S}/"), ("a.org", f"{S}/a-home"),
                                                             ("kept.org", "https://kept.org/"))))
    calls = [("judge_policy", {"url": f"{S}/security", "final_url": f"{S}/security", "domain": "s-gone.org"}),
             ("judge_policy", {"url": "https://x.org/security", "final_url": f"{S}/security-x", "domain": "x.org"}),
             ("judge_policy", {"url": "https://hhs.gov/vdp", "final_url": "https://hhs.gov/vdp", "domain": "s-gone.org"}),
             ("identity_check", {"domain": "s-gone.org", "url": "https://s-gone.org/", "final_url": f"{S}/"}),
             ("identity_check", {"domain": "a.org", "url": "https://a.org/", "final_url": f"{S}/a-home"}),
             ("identity_check", {"domain": "kept.org", "url": "https://kept.org/", "final_url": "https://kept.org/"})]
    with (raw / "llm_log.jsonl").open("w") as fh:
        for purpose, meta in calls:
            fh.write(json.dumps({"purpose": purpose, "model": "m", "meta": meta, "system": "s",
                                 "prompt": f"page text of {meta['final_url']}", "response": "{}"}) + "\n")
    (raw / "wayback.jsonl").write_text("".join(json.dumps(r) + "\n" for r in (
        {"domain": "s-gone.org", "host": "www.s-gone.org", "path": "/.well-known/security.txt", "first_seen": None},
        {"domain": "x.org", "host": "www.s-gone.org", "path": "/security.txt", "first_seen": "2020-01-01"},
        {"domain": "x.org", "host": "x.org", "path": "/.well-known/security.txt", "first_seen": None})))
    (raw / "wayback_redirects.jsonl").write_text("".join(json.dumps(r) + "\n" for r in (
        {"domain": "s-gone.org", "host": "www.s-gone.org", "redirect_target": None},
        {"domain": "old-hospital.org", "host": "old-hospital.org", "redirect_target": "s-gone.org"})))
    return rows[("x.org", "https://x.org/")].body_sha256


def test_after_a_purge_nothing_collected_from_the_site_is_left_in_the_store_or_its_files(env):
    """Fix round 1, G1: the invariant. Allowed to name s-gone.org afterwards: the opt-out list; the done markers; the
    frame tables; the purge log's line naming the purged domain; another listed domain's Internet Archive row naming
    the site as a redirect target (a fact about that other domain, from a third party); and stored page bodies of
    other sites (here x.org's homepage, which links to S)."""
    (env.info / "optout.txt").write_text("s-gone.org\n")
    with Store(env.raw / "crawl.sqlite") as s:
        other_body = _touch_every_way(s, env.raw)
    cli.main(["purge", "s-gone.org"])
    names = ("s-gone", )
    with Store(env.raw / "crawl.sqlite") as s:
        tables = [t for (t,) in s.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        for table in tables:
            rows = s.db.execute(f"SELECT * FROM {table}").fetchall()
            if table == "done":
                rows = [r for r in rows if r[0] != "s-gone.org"]  # allowed: the purged domain's own markers
            assert not any(n in repr(rows) for n in names), table
        left = {(r.url, r.status) for r in s.requests_for("x.org", 1)}
        assert left == {("https://x.org/", 200), ("https://x.org/psirt", 404)}
        assert s.done_status("a.org", 1) == "optout"
    for f in ("judgements.jsonl", "identity.jsonl", "llm_log.jsonl", "wayback.jsonl"):
        assert "s-gone" not in (env.raw / f).read_text(), f
    redirects = (env.raw / "wayback_redirects.jsonl").read_text().splitlines()
    assert [json.loads(x)["domain"] for x in redirects] == ["old-hospital.org"]  # allowed: its redirect target
    assert [json.loads(x)["domain"] for x in (env.raw / "identity.jsonl").read_text().splitlines()] == ["kept.org"]
    kept_calls = [json.loads(x) for x in (env.raw / "llm_log.jsonl").read_text().splitlines()]
    assert [c["meta"]["url"] for c in kept_calls] == ["https://hhs.gov/vdp", "https://kept.org/"]
    assert kept_calls[0]["meta"]["domain"] == "" and kept_calls[0]["prompt"] == "page text of https://hhs.gov/vdp"
    with_name = {b.name for b in (env.raw / "blobs").iterdir() if b"s-gone" in b.read_bytes()}
    assert with_name == {other_body}  # allowed: a page body of another site
    log_lines = (env.raw / "purge.log").read_text().splitlines()
    assert len(log_lines) == 1 and " purged s-gone.org from crawl.sqlite: " in log_lines[0]
    for raw_file in (env.raw / "crawl.sqlite", env.raw / "crawl.sqlite-wal"):  # deleted rows leave no bytes behind
        assert not raw_file.exists() or b"s-gone" not in raw_file.read_bytes(), raw_file.name


# ---- fix round 1, G5: a purge stopped anywhere is finished by running it again

STOPS = ["planned", "committed", *["blob"] * 6, "judgements", "identity", "archive", "archive", "model log",
         "log line"]


def _purge_files(raw):
    return dict(judgements=raw / "judgements.jsonl", archive_files=[raw / "wayback.jsonl",
                raw / "wayback_redirects.jsonl"], model_log=raw / "llm_log.jsonl", identity=raw / "identity.jsonl",
                purge_log=raw / "purge.log", store_name="crawl.sqlite")


def _scene(root):
    raw = root / "data" / "raw"
    raw.mkdir(parents=True)
    store = Store(raw / "crawl.sqlite", raw / "blobs")
    _touch_every_way(store, raw)
    return store, raw


def _end_state(store, raw):
    tables = {t: sorted(map(repr, store.db.execute(f"SELECT * FROM {t}").fetchall()))
              for (t,) in store.db.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")}
    files = {f.name: f.read_text() for f in sorted(raw.glob("*.jsonl"))}
    log = [line.split(" ", 1)[1] for line in (raw / "purge.log").read_text().splitlines()]  # without its time stamp
    return tables, files, sorted(b.name for b in (raw / "blobs").iterdir()), log


def test_the_purge_steps_are_the_ones_the_stop_test_covers(tmp_path, monkeypatch):
    from noroshi import optout

    seen = []
    monkeypatch.setattr(optout, "_step", seen.append)
    store, raw = _scene(tmp_path)
    optout.purge(store, "s-gone.org", **_purge_files(raw))
    store.close()
    assert seen == STOPS


@pytest.mark.parametrize("stop", range(len(STOPS)), ids=[f"{i}-{s}" for i, s in enumerate(STOPS)])
def test_a_purge_stopped_at_any_step_is_finished_by_running_it_again(tmp_path, monkeypatch, stop):
    """Review B Important 2, probe_purge_rerun.py: the end state equals an uninterrupted purge of an identical store."""
    from noroshi import optout

    store, raw = _scene(tmp_path / "whole")
    whole_counts = optout.purge(store, "s-gone.org", **_purge_files(raw))
    whole = _end_state(store, raw)
    store.close()
    store, raw = _scene(tmp_path / "stopped")
    calls = []

    def stop_here(label):
        calls.append(label)
        if len(calls) == stop + 1:
            raise KeyboardInterrupt(f"killed at {label}")
    monkeypatch.setattr(optout, "_step", stop_here)
    with pytest.raises(KeyboardInterrupt):
        optout.purge(store, "s-gone.org", **_purge_files(raw))
    monkeypatch.setattr(optout, "_step", lambda label: None)
    store.close()  # the purge is run again by a new process: a new connection, with SQLite's default settings
    store = Store(raw / "crawl.sqlite", raw / "blobs")
    again = optout.purge(store, "s-gone.org", **_purge_files(raw))
    assert again == whole_counts and whole_counts["requests sent"] > 0 and whole_counts["blobs"] == 6
    assert _end_state(store, raw) == whole
    for raw_file in (raw / "crawl.sqlite", raw / "crawl.sqlite-wal"):  # fix round 2, H5: no bytes of the plan left
        assert not raw_file.exists() or b"s-gone" not in raw_file.read_bytes(), raw_file.name
    store.close()


def test_derive_counts_a_judgement_row_only_when_a_stored_record_stands_behind_it(synthetic):
    """The second guard (G5): a judgement row left behind with no record changes no derived value."""
    store, frame, judg, reg, wb = synthetic
    before = derive_domains(store, frame, judg, reg, wb, synth.NOW)
    v = {"invites_reports": True, "contact_method": True, "safe_harbor": True}
    with judg.open("a") as fh:
        for d in ("none.org", "blocked.org", "vendor.com"):
            fh.write(json.dumps({"domain": d, "url": f"https://www.s-gone.org/{d}", "body_sha256": "f" * 64,
                                 "final": v, "a": v, "b": v, "skipped": None}) + "\n")
            fh.write(json.dumps({"domain": d, "url": f"https://{d}/x.pdf", "body_sha256": "e" * 64, "final": None,
                                 "skipped": "pdf_unreadable"}) + "\n")
    after = derive_domains(store, frame, judg, reg, wb, synth.NOW)
    pd.testing.assert_frame_equal(before, after)


def test_analyze_refuses_while_an_opted_out_domain_still_has_records(env):
    """Fix round 1, M1 (review B Minor 1): the purge has not been run; a domain opted out before it was ever crawled
    has no records and does not block."""
    (env.info / "optout.txt").write_text("notcrawled.org\n")
    cli.main(["derive"])
    cli.main(["analyze", "--allow-incomplete"])  # nothing of notcrawled.org is stored
    (env.info / "optout.txt").write_text("notcrawled.org\nhttps://www.good.org/\n")
    cli.main(["derive"])
    (env.results / "numbers.json").unlink()
    with pytest.raises(SystemExit) as e:
        cli.main(["analyze", "--allow-incomplete"])
    assert "good.org" in str(e.value) and "noroshi purge good.org" in str(e.value)
    assert not (env.results / "numbers.json").exists()
    cli.main(["purge", "good.org"])
    cli.main(["derive"])
    cli.main(["analyze", "--allow-incomplete"])
    assert (env.results / "numbers.json").exists()


# ---- fix round 2

def _stop_purge_at(monkeypatch, label):
    from noroshi import optout

    def stop(at):
        if at == label:
            raise KeyboardInterrupt(f"killed at {at}")
    monkeypatch.setattr(optout, "_step", stop)


def test_a_pending_purge_blocks_every_other_command_until_it_is_run_again(env, monkeypatch, tmp_path):
    """H3, rrY1/probe_pending_between.py turned around: nothing reads or adds to the store while a purge is half done."""
    from frozen import freeze
    from noroshi import optout

    (env.info / "optout.txt").write_text("good.org\n")
    (env.info / "LIVE").write_text("https://info.test/study\n")
    freeze(env.root)
    _stop_purge_at(monkeypatch, "committed")
    with pytest.raises(KeyboardInterrupt):
        cli.main(["purge", "good.org"])
    monkeypatch.setattr(optout, "_step", lambda label: None)
    for command in (["derive"], ["analyze", "--allow-incomplete"], ["judge"], ["identity"],
                    ["crawl", "--sector", "hospital", "--pass", "1"]):
        with pytest.raises(SystemExit) as e:
            cli.main(command)
        assert "run `noroshi purge good.org` again" in str(e.value), command
    assert not (env.derived / "domains.csv").exists() and not (env.raw / "purge.log").exists()
    cli.main(["purge", "good.org"])
    assert len((env.raw / "purge.log").read_text().splitlines()) == 1
    cli.main(["derive"])
    assert (env.derived / "domains.csv").exists()


def test_finishing_a_purge_never_deletes_a_blob_a_record_still_uses(tmp_path, monkeypatch):
    """H4: a record written between the stop and the re-run uses one of the plan's blobs; it keeps it."""
    from noroshi import optout

    store, raw = _scene(tmp_path)
    page = next(r for r in store.requests_for("s-gone.org", 1) if r.purpose == "probe")
    body = store.get_blob(page.body_sha256)
    _stop_purge_at(monkeypatch, "committed")
    with pytest.raises(KeyboardInterrupt):
        optout.purge(store, "s-gone.org", **_purge_files(raw))
    monkeypatch.setattr(optout, "_step", lambda label: None)
    synth.rec(store, "y.org", "candidate", body, ctype="text/html", url="https://y.org/mirror")
    optout.purge(store, "s-gone.org", **_purge_files(raw))
    assert store.get_blob(store.requests_for("y.org", 1)[0].body_sha256) == body
    store.close()


@pytest.mark.parametrize("spelling", ["www.s-gone.org", "S-GONE.ORG", "s-gone.org.", " Www.S-Gone.Org. "])
def test_the_purge_does_the_same_whichever_spelling_of_the_site_it_is_given(tmp_path, spelling):
    """H6, rrY1/probe_host_form.py: the end state, the counts, the traces and the model-log rows equal those of the
    listed-domain form."""
    from noroshi import optout

    store, raw = _scene(tmp_path / "listed")
    counts = optout.purge(store, "s-gone.org", **_purge_files(raw))
    listed = _end_state(store, raw)
    store.close()
    store, raw = _scene(tmp_path / "spelled")
    assert optout.purge(store, spelling, **_purge_files(raw)) == counts
    assert _end_state(store, raw) == listed
    assert not any("s-gone" in repr(row) for table in ("purged", "purged_counts", "requests")
                   for row in store.db.execute(f"SELECT * FROM {table}").fetchall())
    store.close()


def test_the_purge_command_accepts_the_host_form_and_logs_the_site(env, capsys):
    (env.info / "optout.txt").write_text("www.good.org\n")
    cli.main(["purge", "WWW.Good.org."])
    assert " purged good.org from crawl.sqlite: 5 records" in (env.raw / "purge.log").read_text()
    with Store(env.raw / "crawl.sqlite") as s:
        assert s.requests_for("good.org", 1) == []


def test_a_purge_and_the_model_runs_exclude_each_other_across_both_stores(env, monkeypatch):
    """H7: the model-call log is one file for the main and the pilot store, so a purge of either store refuses while
    a crawl, judge or identity run holds the lock of either store, and the other way round."""
    import fcntl

    from noroshi import optout as oo

    (env.info / "optout.txt").write_text("good.org\n")
    Store(env.raw / "pilot.sqlite").close()  # a pilot store beside the main one
    for holder in ("noroshi identity", "noroshi judge", "noroshi crawl"):
        with open(env.raw / "pilot.sqlite.lock", "w") as held:  # a run on the pilot store
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.write(f"pid 4242 {holder}\n")
            held.flush()
            with pytest.raises(SystemExit) as e:
                cli.main(["purge", "good.org"])  # a purge of the main store
            assert f"`{holder}` (pid 4242)" in str(e.value) and "nothing was removed" in str(e.value)
    with Store(env.raw / "crawl.sqlite") as s:
        assert s.requests_for("good.org", 1)
    refused = []
    real = oo.purge

    def purge_while_trying_the_pilot_store(*args, **kw):
        for command in (["identity", "--store", "pilot"], ["judge", "--store", "pilot"]):
            with pytest.raises(SystemExit) as e:
                cli.main(command)
            refused.append(str(e.value))
        return real(*args, **kw)
    monkeypatch.setattr(oo, "purge", purge_while_trying_the_pilot_store)
    cli.main(["purge", "good.org"])
    assert len(refused) == 2 and all("in use by `noroshi purge`" in r for r in refused)


def test_a_purge_keeps_the_judge_calls_of_other_domains_on_the_third_party_site_its_homepage_landed_on(tmp_path):
    """H8: S's homepage landed on T; other listed domains' judge calls about T's pages stay (their verdicts are still
    in use); a call about a page on S's own site goes, whoever asked; S's call about T's page stays without S's name."""
    from noroshi import optout

    with Store(tmp_path / "s.sqlite") as s:
        synth.rec(s, "s-gone.org", "homepage", synth.HOME, ctype="text/html", url="https://s-gone.org/",
                  final="https://www.t-host.org/")
        s.mark_done("s-gone.org", 1, "measured")
        log = tmp_path / "llm.jsonl"
        calls = [("z.org", "https://www.t-host.org/security", "https://www.t-host.org/security"),
                 ("z.org", "https://z.org/vdp", "https://www.t-host.org/vdp"),
                 ("s-gone.org", "https://www.t-host.org/psirt", "https://www.t-host.org/psirt"),
                 ("x.org", "https://x.org/security", "https://www.s-gone.org/security")]
        log.write_text("".join(json.dumps({"purpose": "judge_policy", "model": "m",
                                           "meta": {"url": u, "final_url": f, "domain": d}, "prompt": f"text of {f}",
                                           "response": "{}"}) + "\n" for d, u, f in calls))
        before = log.read_text().splitlines()
        removed = optout.purge(s, "s-gone.org", tmp_path / "j.jsonl", [], log)
    after = log.read_text().splitlines()
    assert after[:2] == before[:2]  # z.org's calls about T's pages, byte for byte
    kept = json.loads(after[2])
    assert kept["meta"] == {"url": "https://www.t-host.org/psirt", "final_url": "https://www.t-host.org/psirt",
                            "domain": ""}
    assert len(after) == 3 and removed["model-log rows"] == 1 and "s-gone" not in log.read_text()


@pytest.mark.parametrize("folder", ["a?b", "c#d", "e%41f", "g h > i"])
def test_unpurged_opens_a_store_whose_path_holds_uri_characters(tmp_path, folder):
    """H9: the read-only URI is built from the path, so `?`, `#` and `%` open the right file."""
    from noroshi.optout import pending_purges, unpurged

    db = tmp_path / folder / "crawl.sqlite"
    db.parent.mkdir()
    with Store(db) as s:
        synth.rec(s, "s-gone.org", "homepage", synth.HOME, ctype="text/html", url="https://s-gone.org/")
    assert unpurged(db, frozenset({"s-gone.org"})) == ["s-gone.org"] and pending_purges(db) == []
