"""A12: every path comes from one root, resolved when a command runs; NOROSHI_ROOT overrides it."""
import json
import subprocess
from pathlib import Path

import pandas as pd
import pytest
from pytest_httpserver import HTTPServer

import synth
from noroshi import cli, config, judge, llm

REPO = Path(__file__).resolve().parents[1]
LIVE_TEXT = "https://info.test/study 2026-11-16T00:00:00Z\n"  # the info page URL (never requested here)


def test_paths_follow_noroshi_root(monkeypatch, tmp_path):
    monkeypatch.setenv("NOROSHI_ROOT", str(tmp_path))
    p = config.paths()
    assert p.root == tmp_path
    assert p.frames == tmp_path / "data" / "frames"
    assert p.passes == tmp_path / "data" / "frames" / "resolution_passes"
    assert p.info == tmp_path / "info" and p.protocol == tmp_path / "protocol"
    assert p.frozen == tmp_path / "protocol" / "FROZEN.txt"
    assert p.raw == tmp_path / "data" / "raw" and p.derived == tmp_path / "data" / "derived"
    assert p.results == tmp_path / "results"
    assert p.registries == tmp_path / "data" / "reference" / "registries"
    assert p.rucc == tmp_path / "data" / "reference" / "rucc2023.csv"
    assert p.llm_log == tmp_path / "data" / "raw" / "llm_log.jsonl"
    monkeypatch.delenv("NOROSHI_ROOT")
    assert config.paths().root == config.REPO  # the default, read at call time (the tests move it out of the checkout)
    import conftest
    assert conftest.CHECKOUT_ROOT == REPO  # outside the tests the default is the repository


def test_model_call_log_lands_under_the_root(monkeypatch, tmp_path):
    monkeypatch.setenv("NOROSHI_ROOT", str(tmp_path))
    out = json.dumps({"result": "ok", "is_error": False})
    llm.ask_claude("hi", "m", "s", "p", runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, out, ""))
    assert len((tmp_path / "data" / "raw" / "llm_log.jsonl").read_text().splitlines()) == 1


def _snapshot(root: Path) -> dict:
    found = {}
    for top in ("data", "info", "protocol", "results", "logs"):
        for p in (root / top).rglob("*") if (root / top).exists() else ():
            if p.is_file():
                st = p.stat()
                found[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return found


def test_rehearsal_under_noroshi_root_writes_nothing_outside_it(monkeypatch, tmp_path, httpserver: HTTPServer):
    before = _snapshot(REPO)
    root = tmp_path / "rehearsal"
    monkeypatch.setenv("NOROSHI_ROOT", str(root))
    frames, info, regs = root / "data" / "frames", root / "info", root / "data" / "reference" / "registries"
    for d in (frames, info, regs):
        d.mkdir(parents=True)
    httpserver.expect_request("/").respond_with_data("down", status=503)  # one request, no 2 s gaps
    head = "sector,domain,start_url,n_units,beds_sum,name_tokens\n"
    (frames / "federal_domains.csv").write_text(
        head + "".join(f"federal,{d},{httpserver.url_for('/')},1,,\n" for d in ("a.org", "b.org")))
    pd.DataFrame([{"hifld_id": "1", "name": "A", "state": "TX", "type": "GENERAL ACUTE CARE", "owner": "NON-PROFIT",
                   "beds": "100", "rural": "True", "domain": "good.org", "exclusion": ""}]).to_csv(
        frames / "hospitals.csv", index=False)
    (regs / "program-list.json").write_text("[]")
    (regs / "CNAsList.json").write_text("[]")
    for p in ("hackerone", "bugcrowd", "intigriti", "yeswehack"):
        (regs / f"{p}_data.json").write_text("[]")
    (info / "LIVE").write_text(LIVE_TEXT)
    cli.main(["crawl", "--sector", "federal", "--pass", "1", "--limit", "1", "--seed", "1"])
    reply = json.dumps({"invites_reports": False, "contact_method": False, "safe_harbor": False})
    orig = judge.run_judging
    monkeypatch.setattr(judge, "run_judging",  # the command also passes the opt-out set
                        lambda store, out, **kw: orig(store, out, ask=lambda *a, **k: type("R", (), {"text": reply})(),
                                                      **kw))
    cli.main(["judge", "--store", "pilot"])
    cli.main(["derive", "--store", "pilot"])
    cli.main(["analyze", "--store", "pilot"])
    assert (root / "data" / "raw" / "pilot.sqlite").exists()
    assert (root / "data" / "derived" / "pilot" / "domains.csv").exists()
    assert (root / "results" / "pilot" / "numbers.json").exists()
    assert _snapshot(REPO) == before  # nothing written in the repository's data, info, protocol, results or logs


def test_the_dependencies_name_what_the_code_and_tests_import():
    """Post-pilot Y8: statsmodels is unused since the regression model was removed; the tests import cryptography
    (tests/crawled.py), pinned to the version already locked."""
    import tomllib

    project = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert not any(dep.startswith("statsmodels") for dep in project["project"]["dependencies"])
    lock = tomllib.loads((REPO / "uv.lock").read_text())
    locked = next(p["version"] for p in lock["package"] if p["name"] == "cryptography")
    assert f"cryptography=={locked}" in project["dependency-groups"]["dev"]
