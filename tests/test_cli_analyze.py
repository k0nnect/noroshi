import json

import pandas as pd
import pytest

import synth
from noroshi import cli, config
from noroshi.store import Store


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NOROSHI_ROOT", str(tmp_path))
    raw = tmp_path / "data" / "raw"
    raw.mkdir(parents=True)
    store, frame, judg, reg = synth.build(tmp_path)
    store.close()
    # the pilot store is the synthetic store, copied under the pilot names
    (tmp_path / "s.sqlite").rename(raw / "pilot.sqlite")
    (tmp_path / "blobs").rename(raw / "pilot_blobs")
    judg.rename(raw / "pilot_judgements.jsonl")
    frames = tmp_path / "data" / "frames"
    frames.mkdir()
    frame[frame.sector == "hospital"].to_csv(frames / "hospital_domains.csv", index=False)
    frame[frame.sector == "federal"].to_csv(frames / "federal_domains.csv", index=False)
    pd.DataFrame([{"hifld_id": "1", "name": "A", "state": "TX", "type": "GENERAL ACUTE CARE", "owner": "NON-PROFIT",
                   "beds": "100", "rural": "True", "domain": "good.org", "exclusion": ""}]).to_csv(
        frames / "hospitals.csv", index=False)
    regs = tmp_path / "data" / "reference" / "registries"
    regs.mkdir(parents=True)
    (regs / "program-list.json").write_text("[]")
    (regs / "CNAsList.json").write_text(json.dumps([{"contact": [{"email": [{"value": "a@cnaonly.com"}]}]}]))
    for p in ("hackerone", "bugcrowd", "intigriti", "yeswehack"):
        (regs / f"{p}_data.json").write_text("[]")
    return tmp_path


def test_derive_and_analyze_pilot_write_only_pilot_paths(env):
    cli.main(["derive", "--store", "pilot"])
    cli.main(["analyze", "--store", "pilot"])
    assert (env / "data" / "derived" / "pilot" / "domains.csv").exists()
    assert (env / "data" / "derived" / "pilot" / "wayback_first_seen.csv").exists()
    assert (env / "data" / "derived" / "pilot" / "wayback_redirects.csv").exists()
    assert (env / "results" / "pilot" / "numbers.json").exists()
    assert (env / "results" / "pilot" / "tables" / "table2_shares.csv").exists()
    assert (env / "results" / "pilot" / "figures" / "figure1_shares.png").exists()
    assert not (env / "results" / "numbers.json").exists() and not (env / "data" / "derived" / "domains.csv").exists()
    first = pd.read_csv(env / "data" / "derived" / "pilot" / "wayback_first_seen.csv")
    assert set(first.columns) == {"domain", "host", "path"} and "good.org" in set(first.domain)
    assert "secret-contact@" not in (env / "data" / "derived" / "pilot" / "domains.csv").read_text()
    numbers = json.loads((env / "results" / "pilot" / "numbers.json").read_text())
    assert numbers["hospital.domain.main.k"] == 6


def test_main_store_is_the_default_and_missing_store_exits(env):
    with pytest.raises(SystemExit) as e:
        cli.main(["derive"])
    assert "crawl.sqlite" in str(e.value)
    with pytest.raises(SystemExit):
        cli.main(["analyze"])


def test_judge_pilot_writes_pilot_file_only(env, monkeypatch):
    from types import SimpleNamespace

    from noroshi import judge

    raw = config.paths().raw
    (raw / "pilot_judgements.jsonl").unlink()
    reply = json.dumps({"invites_reports": True, "contact_method": True, "safe_harbor": False})
    orig = judge.run_judging
    monkeypatch.setattr(judge, "run_judging",  # the command also passes the opt-out set
                        lambda store, out, **kw: orig(store, out, ask=lambda *a, **k: SimpleNamespace(text=reply), **kw))
    cli.main(["judge", "--store", "pilot"])
    rows = [json.loads(x) for x in (raw / "pilot_judgements.jsonl").read_text().splitlines()]
    assert {r["domain"] for r in rows} == {"policyonly.org"} and rows[0]["final"]["invites_reports"]
    assert not (raw / "judgements.jsonl").exists()


def test_pilot_derive_reads_pilot_wayback_only(env):
    (config.paths().raw / "wayback.jsonl").write_text(json.dumps(
        {"domain": "good.org", "host": "good.org", "path": "/.well-known/security.txt", "first_seen": "2001-01-01"}) + "\n")
    (config.paths().raw / "pilot_wayback.jsonl").write_text(json.dumps(
        {"domain": "good.org", "host": "good.org", "path": "/.well-known/security.txt", "first_seen": "2002-02-02"}) + "\n")
    cli.main(["derive", "--store", "pilot"])
    d = pd.read_csv(env / "data" / "derived" / "pilot" / "domains.csv", dtype=str, keep_default_na=False).set_index("domain")
    assert d.loc["good.org", "first_seen"] == "2002-02-02"


def test_derive_prints_how_many_measured_domains_have_unjudged_pages(env, capsys):
    (config.paths().raw / "pilot_judgements.jsonl").unlink()
    cli.main(["derive", "--store", "pilot"])
    out = capsys.readouterr().out
    assert "1 measured domains have stored pages with no judgement row" in out and "noroshi judge" in out


# ---- B12: guard rails

def test_pilot_outputs_are_ignored_by_git():
    import subprocess
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    lines = (repo / ".gitignore").read_text().splitlines()
    assert "data/derived/pilot/" in lines and "results/pilot/" in lines
    for path in ("data/derived/pilot/domains.csv", "results/pilot/numbers.json"):
        assert subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", path]).returncode == 0, path


def test_main_analyze_refuses_not_crawled_domains_unless_allowed(env, capsys):
    raw = config.paths().raw
    for name in ("pilot.sqlite", "pilot_judgements.jsonl"):
        (raw / name).rename(raw / name.removeprefix("pilot_").replace("pilot.", "crawl."))
    (raw / "pilot_blobs").rename(raw / "blobs")
    cli.main(["derive"])
    with pytest.raises(SystemExit) as e:
        cli.main(["analyze"])
    assert "1 domains are not_crawled" in str(e.value) and "--allow-incomplete" in str(e.value)
    assert not (env / "results" / "numbers.json").exists()
    cli.main(["analyze", "--allow-incomplete"])
    assert (env / "results" / "numbers.json").exists()


def test_pilot_analyze_is_exempt_from_the_not_crawled_check(env):
    cli.main(["derive", "--store", "pilot"])
    d = pd.read_csv(env / "data" / "derived" / "pilot" / "domains.csv", dtype=str, keep_default_na=False)
    assert (d.status == "not_crawled").any()
    cli.main(["analyze", "--store", "pilot"])
    assert (env / "results" / "pilot" / "numbers.json").exists()


def test_analyze_lists_parser_disagreements_and_reads_the_judge_and_identity_files(env):
    """Post-pilot Y5 (T1, T6)."""
    d = env / "data" / "derived" / "pilot"
    cli.main(["derive", "--store", "pilot"])
    table = pd.read_csv(d / "domains.csv", dtype=str, keep_default_na=False)
    table.loc[table.domain == "good.org", "parsers_agree"] = "False"  # as if the library found no valid contact
    table.to_csv(d / "domains.csv", index=False)
    cli.main(["analyze", "--store", "pilot"])
    listed = pd.read_csv(env / "results" / "pilot" / "parser_disagreements.csv", dtype=str, keep_default_na=False)
    assert list(listed.domain) == ["good.org"]
    assert (listed.sectxt_url[0], listed.ours_valid_contact[0], listed.library_valid_contact[0]) == \
        ("https://good.org/.well-known/security.txt", "True", "False")
    numbers = json.loads((env / "results" / "pilot" / "numbers.json").read_text())
    assert numbers["agreement.judge.invites_reports.n"] == 0  # the synthetic judgements carry no answers of a and b
    assert (env / "results" / "pilot" / "tables" / "tableS2_agreement.md").exists()
