import fcntl

import pytest
from pytest_httpserver import HTTPServer

from frozen import freeze
from noroshi import cli
from noroshi.store import Store


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NOROSHI_ROOT", str(tmp_path))
    frames, info, raw = tmp_path / "data" / "frames", tmp_path / "info", tmp_path / "data" / "raw"
    frames.mkdir(parents=True)
    info.mkdir()
    return frames, info, raw


LIVE_TEXT = "https://info.test/study 2026-11-16T00:00:00Z\n"  # the info page URL (never requested here)


def write_table(frames, url, domains=("a.org", "b.org")):
    rows = "".join(f"federal,{d},{url},1,,\n" for d in domains)
    (frames / "federal_domains.csv").write_text("sector,domain,start_url,n_units,beds_sum,name_tokens\n" + rows)


def test_load_optout(tmp_path):
    p = tmp_path / "o.txt"
    p.write_text("# comment\n\n  Foo.ORG \n#bar.org\nbaz.com\nhttps://www.Optme.org/please-exclude\n")
    assert cli.load_optout(p) == frozenset({"foo.org", "baz.com", "www.optme.org", "optme.org"})
    assert cli.load_optout(tmp_path / "missing") == frozenset()


def test_missing_table(env, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["crawl", "--sector", "federal", "--pass", "1", "--limit", "1", "--seed", "1"])
    assert "missing" in str(e.value)


def test_not_live(env):
    frames, _, raw = env
    write_table(frames, "http://localhost:1/")
    with pytest.raises(SystemExit) as e:
        cli.main(["crawl", "--sector", "federal", "--pass", "1", "--limit", "1", "--seed", "1"])
    assert "not live" in str(e.value) and not (raw / "crawl.sqlite").exists()


def test_full_pass_needs_frozen_protocol(env):
    frames, info, raw = env
    write_table(frames, "http://localhost:1/")
    (info / "LIVE").write_text(LIVE_TEXT)
    with pytest.raises(SystemExit) as e:
        cli.main(["crawl", "--sector", "federal", "--pass", "1"])
    assert "FROZEN" in str(e.value) and not (raw / "crawl.sqlite").exists()


def test_pilot_uses_its_own_store_and_prints_it(env, capsys, httpserver: HTTPServer):
    frames, info, raw = env
    httpserver.expect_request("/").respond_with_data("down", status=503)  # one request, no 2 s gaps
    write_table(frames, httpserver.url_for("/"))
    (info / "LIVE").write_text(LIVE_TEXT)
    cli.main(["crawl", "--sector", "federal", "--pass", "1", "--limit", "1", "--seed", "1"])
    out = capsys.readouterr().out
    assert "unreachable: 1" in out and "pilot.sqlite" in out and len(httpserver.log) == 1
    assert not (raw / "crawl.sqlite").exists()
    import sqlite3

    db = sqlite3.connect(raw / "pilot.sqlite")
    assert db.execute("SELECT COUNT(*) FROM done").fetchone()[0] == 1
    db.close()
    assert not (raw / "blobs").exists()  # a pilot keeps its blobs in its own folder
    assert (raw / "pilot_blobs").is_dir()


def test_limit_at_or_above_table_size_needs_frozen(env):
    frames, info, raw = env
    write_table(frames, "http://localhost:1/")
    (info / "LIVE").write_text(LIVE_TEXT)
    for limit in ("2", "99999"):
        with pytest.raises(SystemExit) as e:
            cli.main(["crawl", "--sector", "federal", "--pass", "1", "--limit", limit, "--seed", "1"])
        assert "FROZEN" in str(e.value)
    assert not (raw / "crawl.sqlite").exists() and not (raw / "pilot.sqlite").exists()


def test_full_pass_uses_main_store(env, capsys, httpserver: HTTPServer, tmp_path):
    frames, info, raw = env
    httpserver.expect_request("/").respond_with_data("down", status=503)
    write_table(frames, httpserver.url_for("/"), domains=("a.org",))
    (info / "LIVE").write_text(LIVE_TEXT)
    freeze(tmp_path)
    cli.main(["crawl", "--sector", "federal", "--pass", "1"])
    assert "crawl.sqlite" in capsys.readouterr().out and not (raw / "pilot.sqlite").exists()
    with Store(raw / "crawl.sqlite") as s:
        assert s.done_status("a.org", 1) == "unreachable"


def test_opted_out_domain_not_requested(env, capsys, httpserver: HTTPServer):
    frames, info, _ = env
    write_table(frames, httpserver.url_for("/"))
    (info / "LIVE").write_text(LIVE_TEXT)
    (info / "optout.txt").write_text("A.org\n# c\n B.ORG \n")
    cli.main(["crawl", "--sector", "federal", "--pass", "1", "--limit", "1", "--seed", "1"])
    assert "optout: 1" in capsys.readouterr().out and httpserver.log == []



# ---- A3: opt-out entries are normalized

@pytest.mark.parametrize("entry", ["https://www.optme.org/", "www.optme.org", "optme.org", "HTTP://OPTME.ORG:443/x"])
async def test_each_optout_form_blocks_every_request_to_the_apex_domain(tmp_path, httpserver, local_dns, entry):
    from noroshi.crawl import crawl_domain
    from noroshi.fetch import OptedOut, PoliteFetcher
    from datetime import datetime, timezone

    local_dns.update({"optme.org", "www.optme.org"})
    httpserver.expect_request("/").respond_with_data("<title>Optme Hospital</title>", content_type="text/html")
    (tmp_path / "optout.txt").write_text(f"# from an issue\n{entry}\n")
    (tmp_path / "LIVE").write_text(LIVE_TEXT)
    optout = cli.load_optout(tmp_path / "optout.txt")
    with Store(tmp_path / "s.sqlite") as store:
        async with PoliteFetcher(store, optout, tmp_path / "LIVE") as f:
            row = {"sector": "hospital", "domain": "optme.org", "start_url": f"http://optme.org:{httpserver.port}/",
                   "name_tokens": "optme"}
            assert await crawl_domain(row, 1, f, store, datetime.now(timezone.utc)) == "optout"
            for host in ("optme.org", "www.optme.org", "a.b.optme.org"):
                with pytest.raises(OptedOut):
                    await f.fetch("other.org", "hospital", f"http://{host}:{httpserver.port}/x", "candidate", 1)
        assert store.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
    assert httpserver.log == []


def test_optout_line_that_does_not_parse_stops_before_any_request(env, httpserver: HTTPServer):
    frames, info, raw = env
    write_table(frames, httpserver.url_for("/"))
    (info / "LIVE").write_text(LIVE_TEXT)
    (info / "optout.txt").write_text("good.org\nplease remove my site\n")
    with pytest.raises(SystemExit) as e:
        cli.main(["crawl", "--sector", "federal", "--pass", "1", "--limit", "1", "--seed", "1"])
    assert "optout.txt" in str(e.value) and "please remove my site" in str(e.value)
    assert httpserver.log == [] and not (raw / "pilot.sqlite").exists()


# ---- A11: the crawl command

def test_limit_requires_seed(env):
    frames, info, raw = env
    write_table(frames, "http://localhost:1/")
    (info / "LIVE").write_text(LIVE_TEXT)
    with pytest.raises(SystemExit) as e:
        cli.main(["crawl", "--sector", "federal", "--pass", "1", "--limit", "1"])
    assert "--seed" in str(e.value) and not raw.exists()


HEAD = "sector,domain,start_url,n_units,beds_sum,name_tokens\n"


def test_sector_all_runs_hospital_first_and_never_requests_a_done_domain_again(env, httpserver: HTTPServer, tmp_path,
                                                                              monkeypatch):
    from noroshi import config

    monkeypatch.setattr(config, "MIN_DELAY_SECONDS", 0.0)  # the gap is not what this test is about
    frames, info, raw = env
    url = httpserver.url_for("/")
    httpserver.expect_request("/").respond_with_data("<title>Dual Hospital</title>", content_type="text/html")
    (frames / "hospital_domains.csv").write_text(HEAD + f"hospital,dual.org,{url},1,10,dual\n")
    (frames / "device_domains.csv").write_text(HEAD + f"device,dual.org,{url},1,,dual\n")
    for name in ("federal", "icsma", "ehr"):
        (frames / f"{name}_domains.csv").write_text(HEAD)
    (info / "LIVE").write_text(LIVE_TEXT)
    freeze(tmp_path)
    cli.main(["crawl", "--sector", "all", "--pass", "1"])
    with Store(raw / "crawl.sqlite") as s:
        recs = s.requests_for("dual.org", 1)
        assert {r.sector for r in recs} == {"hospital"} and "cmshpt" in [r.purpose for r in recs]
    assert [r.path for r, _ in httpserver.log].count("/") == 1


def test_progress_every_50_domains_printed_and_logged(env, capsys):
    frames, info, raw = env
    names = [f"d{i}.org" for i in range(120)]
    write_table(frames, "http://localhost:1/", domains=names)
    (info / "LIVE").write_text(LIVE_TEXT)
    (info / "optout.txt").write_text("\n".join(names) + "\n")  # every domain ends at once, nothing is sent
    cli.main(["crawl", "--sector", "federal", "--pass", "1", "--limit", "119", "--seed", "3"])
    out = [line for line in capsys.readouterr().out.splitlines() if "domains done" in line]
    logged = [line for line in (raw / "pilot_crawl.log").read_text().splitlines() if "domains done" in line]
    assert len(out) >= 2 and out[:2] == logged[:2]
    assert "50 domains done" in out[0] and "optout 50" in out[0] and "elapsed" in out[0]
    assert "100 domains done" in out[1] and "optout 100" in out[1]


def test_second_crawl_on_the_same_store_exits(env, httpserver: HTTPServer):
    frames, info, raw = env
    write_table(frames, httpserver.url_for("/"))
    (info / "LIVE").write_text(LIVE_TEXT)
    raw.mkdir(parents=True)
    with open(raw / "pilot.sqlite.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another crawl holds the store
        with pytest.raises(SystemExit) as e:
            cli.main(["crawl", "--sector", "federal", "--pass", "1", "--limit", "1", "--seed", "1"])
    assert "in use by another noroshi command" in str(e.value) and httpserver.log == []


# ---- A13: the freeze gate checks the hash

def test_full_pass_refused_unless_protocol_matches_the_frozen_commit(env, httpserver: HTTPServer, tmp_path):
    frames, info, raw = env
    httpserver.expect_request("/").respond_with_data("down", status=503)
    write_table(frames, httpserver.url_for("/"), domains=("a.org",))
    (info / "LIVE").write_text(LIVE_TEXT)
    frozen = freeze(tmp_path)
    (tmp_path / "protocol" / "PROTOCOL.md").write_text("# Protocol\n\nEdited after the freeze.\n")
    with pytest.raises(SystemExit) as e:
        cli.main(["crawl", "--sector", "federal", "--pass", "1"])
    assert "PROTOCOL.md" in str(e.value) and httpserver.log == []
    frozen.write_text("0123456789abcdef0123456789abcdef01234567\n")  # a hash that is not a commit here
    with pytest.raises(SystemExit) as e:
        cli.main(["crawl", "--sector", "federal", "--pass", "1"])
    assert "commit" in str(e.value) and httpserver.log == []


def test_sector_all_pilot_skips_empty_tables(env, httpserver: HTTPServer, capsys, monkeypatch):
    from noroshi import config

    monkeypatch.setattr(config, "MIN_DELAY_SECONDS", 0.0)  # the gap is not what this test is about
    frames, info, raw = env
    httpserver.expect_request("/").respond_with_data("down", status=503)
    url = httpserver.url_for("/")
    (frames / "hospital_domains.csv").write_text(HEAD + f"hospital,h1.org,{url},1,10,h\nhospital,h2.org,{url},1,10,h\n")
    (frames / "federal_domains.csv").write_text(HEAD + f"federal,f1.gov,{url},1,,\nfederal,f2.gov,{url},1,,\n")
    for name in ("device", "icsma", "ehr"):
        (frames / f"{name}_domains.csv").write_text(HEAD)
    (info / "LIVE").write_text(LIVE_TEXT)
    cli.main(["crawl", "--sector", "all", "--pass", "1", "--limit", "1", "--seed", "1"])
    assert "unreachable: 2" in capsys.readouterr().out and (raw / "pilot.sqlite").exists()


# ---- rulings round, R3: info/LIVE must hold the info page URL

@pytest.mark.parametrize("limit", [["--limit", "1", "--seed", "1"], []])
def test_live_file_without_a_url_stops_the_crawl_before_any_request(env, httpserver: HTTPServer, tmp_path, limit):
    frames, info, raw = env
    write_table(frames, httpserver.url_for("/"))
    freeze(tmp_path)
    for text in ("go\n", "", "ftp://example.org/info\n"):
        (info / "LIVE").write_text(text)
        with pytest.raises(SystemExit) as e:
            cli.main(["crawl", "--sector", "federal", "--pass", "1", *limit])
        assert "LIVE" in str(e.value) and "URL" in str(e.value)
    assert httpserver.log == [] and not raw.exists()


# ---- residuals round, S6: the control request is written to the run log

def test_outage_stop_writes_the_control_request_to_the_run_log(env):
    frames, info, raw = env
    rows = "".join(f"federal,down{i}.org,https://down{i}.invalid/,1,,\n" for i in range(25))
    (frames / "federal_domains.csv").write_text(HEAD + rows)
    (info / "LIVE").write_text("http://info.test:1/study 2026-11-16T00:00:00Z\n")  # the control cannot load
    with pytest.raises(SystemExit) as e:
        cli.main(["crawl", "--sector", "federal", "--pass", "1", "--limit", "24", "--seed", "1"])
    assert "stopped" in str(e.value)
    control = [line for line in (raw / "pilot_crawl.log").read_text().splitlines() if "control request" in line]
    assert len(control) == 1 and "http://info.test:1/study" in control[0] and "failed" in control[0]
    assert control[0][:4].isdigit()  # it starts with its UTC time


# ---- post-pilot Y9: a second pilot must not revisit the first pilot's sites

import pandas as pd  # noqa: E402

from noroshi.crawl import draw_sample  # noqa: E402


def _table(n, sector="device"):
    return pd.DataFrame([{"sector": sector, "domain": f"d{i:03d}.org", "start_url": f"https://d{i:03d}.org/",
                          "n_units": "1", "beds_sum": "", "name_tokens": ""} for i in range(n)])


def test_an_excluded_domain_is_never_drawn_and_the_sample_keeps_its_size():
    table = _table(100)
    first = set(draw_sample(table, 30, 20261115).domain)
    again = draw_sample(table, 30, 20261116, exclude=frozenset(first))
    assert len(again) == 30 and not set(again.domain) & first
    assert list(again.domain) == list(draw_sample(table, 30, 20261116, exclude=frozenset(first)).domain)  # repeatable
    rest = draw_sample(table, 30, 1, exclude=frozenset(f"d{i:03d}.org" for i in range(80)))
    assert len(rest) == 20 and set(rest.domain) == {f"d{i:03d}.org" for i in range(80, 100)}  # fewer remain: all


def test_the_sample_without_an_exclusion_is_unchanged():
    table = _table(100)
    assert list(draw_sample(table, 30, 20261115).domain) == list(table.sample(n=30, random_state=20261115).domain)
    assert list(draw_sample(table, 30, 7, exclude=frozenset()).domain) == list(table.sample(n=30, random_state=7).domain)


def test_exclude_needs_a_pilot(env, tmp_path):
    frames, info, raw = env
    write_table(frames, "http://localhost:1/")
    (info / "LIVE").write_text(LIVE_TEXT)
    names = tmp_path / "first_pilot.txt"
    names.write_text("a.org\n")
    with pytest.raises(SystemExit) as e:
        cli.main(["crawl", "--sector", "federal", "--pass", "1", "--exclude", str(names)])
    assert e.value.code != 0 and "--limit" in str(e.value)
    freeze(tmp_path)
    with pytest.raises(SystemExit) as e:  # a limit at or above the table size is a full pass
        cli.main(["crawl", "--sector", "federal", "--pass", "1", "--limit", "5", "--seed", "1", "--exclude", str(names)])
    assert e.value.code != 0 and "pilot" in str(e.value)
    assert not raw.exists() or not any(raw.iterdir())


def test_an_excluded_domain_in_two_tables_is_excluded_from_both_and_the_counts_are_printed(env, httpserver: HTTPServer,
                                                                                            tmp_path, capsys,
                                                                                            monkeypatch):
    from noroshi import config

    monkeypatch.setattr(config, "MIN_DELAY_SECONDS", 0.0)
    frames, info, raw = env
    url = httpserver.url_for("/")
    httpserver.expect_request("/").respond_with_data("down", status=503)
    (frames / "hospital_domains.csv").write_text(HEAD + f"hospital,dual.org,{url},1,10,dual\n"
                                                 + f"hospital,h1.org,{url},1,10,h\nhospital,h2.org,{url},1,10,h\n")
    (frames / "device_domains.csv").write_text(HEAD + f"device,dual.org,{url},1,,dual\ndevice,d1.com,{url},1,,d\n"
                                               + f"device,d2.com,{url},1,,d\n")
    for name in ("federal", "icsma", "ehr"):
        (frames / f"{name}_domains.csv").write_text(HEAD)
    (info / "LIVE").write_text(LIVE_TEXT)
    names = tmp_path / "first_pilot.txt"
    names.write_text("# the first pilot\n\nhttps://www.DUAL.org/\nh1.org\n")
    cli.main(["crawl", "--sector", "all", "--pass", "1", "--limit", "2", "--seed", "1", "--exclude", str(names)])
    out = capsys.readouterr().out
    assert "hospital: 2 domains excluded" in out and "device: 1 domains excluded" in out
    with Store(raw / "pilot.sqlite") as s:
        crawled = {row[0] for row in s.db.execute("SELECT domain FROM done")}
    assert crawled == {"h2.org", "d1.com", "d2.com"}


def test_exclude_with_a_missing_file_exits_and_with_an_empty_file_excludes_nothing(env, httpserver: HTTPServer,
                                                                                    tmp_path, capsys):
    """Fix round 1, M7."""
    frames, info, raw = env
    httpserver.expect_request("/").respond_with_data("down", status=503)
    write_table(frames, httpserver.url_for("/"))
    (info / "LIVE").write_text(LIVE_TEXT)
    with pytest.raises(SystemExit) as e:
        cli.main(["crawl", "--sector", "federal", "--pass", "1", "--limit", "1", "--seed", "1", "--exclude",
                  str(tmp_path / "no-such-file.txt")])
    assert e.value.code != 0 and "does not exist" in str(e.value) and httpserver.log == []
    empty = tmp_path / "empty.txt"
    empty.write_text("# nothing to leave out\n\n")
    cli.main(["crawl", "--sector", "federal", "--pass", "1", "--limit", "1", "--seed", "1", "--exclude", str(empty)])
    out = capsys.readouterr().out
    assert "federal: 0 domains excluded" in out and "unreachable: 1" in out
    with Store(raw / "pilot.sqlite") as s:
        drawn = [row[0] for row in s.db.execute("SELECT domain FROM done")]
    assert drawn == list(draw_sample(pd.read_csv(frames / "federal_domains.csv", dtype=str), 1, 1).domain)
