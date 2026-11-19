import pytest
from datetime import date

from noroshi.wayback import WaybackError, first_seen, last_redirect_target, throttled


class Resp:
    def __init__(self, data=None, status=200, headers=None, text="[]"):
        self._d, self.status_code, self.headers, self.text = data, status, headers or {}, text

    def json(self):
        if self._d is ValueError:
            raise ValueError("not json")
        return self._d


class FakeGet:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        for key, resp in self.routes.items():
            if key in url:
                return resp
        raise AssertionError(f"unexpected url {url}")


def test_first_seen_parses_cdx():
    get = FakeGet({"cdx/search/cdx": Resp([["timestamp"], ["20190304050607"]])})
    assert first_seen("example.org", "/.well-known/security.txt", get=get) == date(2019, 3, 4)
    url, kw = get.calls[0]
    p = kw["params"]
    assert p["url"] == "example.org/.well-known/security.txt" and p["output"] == "json" and p["limit"] == 1
    assert set(p["filter"]) == {"statuscode:200", "mimetype:text/plain"}


def test_first_seen_none_when_empty():
    assert first_seen("example.org", "/security.txt", get=FakeGet({"cdx": Resp([])})) is None


def test_first_seen_raises_on_error_status():
    for status in (429, 503):
        with pytest.raises(WaybackError, match=str(status)):
            first_seen("example.org", "/s", get=FakeGet({"cdx": Resp(None, status=status)}))


def test_first_seen_empty_200_body_is_no_capture():
    assert first_seen("example.org", "/s", get=FakeGet({"cdx": Resp(ValueError, text="")})) is None
    assert first_seen("example.org", "/s", get=FakeGet({"cdx": Resp(ValueError, text="  \n")})) is None


def test_first_seen_non_json_200_is_error():
    with pytest.raises(WaybackError, match="JSON"):
        first_seen("example.org", "/s", get=FakeGet({"cdx": Resp(ValueError, text="<html>busy</html>")}))


def test_last_redirect_target_errors():
    with pytest.raises(WaybackError, match="503"):
        last_redirect_target("a.org", get=FakeGet({"cdx": Resp(None, status=503)}))
    with pytest.raises(WaybackError, match="JSON"):
        last_redirect_target("a.org", get=FakeGet({"cdx": Resp(ValueError, text="oops")}))
    assert last_redirect_target("a.org", get=FakeGet({"cdx": Resp(ValueError, text="")})) is None
    get = FakeGet({"cdx/search": Resp([["timestamp", "statuscode"], ["20230101000000", "301"]]),
                   "/web/20230101000000id_/": Resp(None, 429)})
    with pytest.raises(WaybackError, match="429"):
        last_redirect_target("a.org", get=get)


def test_cli_wayback_error_row_written_nothing_and_retried(tmp_path, monkeypatch, capsys):
    import json

    from noroshi import cli, wayback

    monkeypatch.setenv("NOROSHI_ROOT", str(tmp_path))
    monkeypatch.setattr(wayback, "throttled", lambda get, **kw: get)
    src = tmp_path / "t.csv"
    src.write_text("domain,host,path\nx.org,www.x.org,/a\ny.org,www.y.org,/b\n")

    def flaky(url, **kw):
        return Resp(None, status=503) if "x.org" in kw["params"]["url"] else Resp([])

    monkeypatch.setattr(wayback.httpx, "get", flaky)
    cli.main(["wayback", "--first-seen", str(src)])
    out = tmp_path / "data" / "raw" / "wayback.jsonl"
    rows = [json.loads(x) for x in out.read_text().splitlines()]
    assert [r["domain"] for r in rows] == ["y.org"] and rows[0]["first_seen"] is None
    assert "failed: 1" in capsys.readouterr().out
    monkeypatch.setattr(wayback.httpx, "get", lambda url, **kw: Resp([["timestamp"], ["20200102000000"]]))
    cli.main(["wayback", "--first-seen", str(src)])
    rows = [json.loads(x) for x in out.read_text().splitlines()]
    assert [r["domain"] for r in rows] == ["y.org", "x.org"] and rows[1]["first_seen"] == "2020-01-02"


def test_last_redirect_target_returns_other_domain():
    get = FakeGet({
        "cdx/search/cdx": Resp([["timestamp", "statuscode"], ["20230102030405", "301"]]),
        "/web/20230102030405id_/": Resp(None, 302, {"location": "http://web.archive.org/web/20230102030405id_/https://www.newowner.com/home"}),
    })
    assert last_redirect_target("old-hospital.org", get=get) == "newowner.com"
    assert get.calls[0][1]["params"]["limit"] == -1
    assert all(kw.get("follow_redirects") is False for _, kw in get.calls[1:])


def test_last_redirect_target_none_cases():
    cdx = lambda ts, sc: Resp([["timestamp", "statuscode"], [ts, sc]])
    # latest capture is a normal page
    assert last_redirect_target("a.org", get=FakeGet({"cdx": cdx("20230101000000", "200")})) is None
    # no captures
    assert last_redirect_target("a.org", get=FakeGet({"cdx": Resp([])})) is None
    # redirect inside the same registrable domain
    get = FakeGet({"cdx/search": cdx("20230101000000", "301"),
                   "/web/20230101000000id_/": Resp(None, 301, {"location": "/web/20230101000000id_/https://www.a.org/en"})})
    assert last_redirect_target("a.org", get=get) is None


def test_throttled_spaces_calls():
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    g = throttled(lambda url, **kw: "ok", interval=1.0, clock=lambda: now[0], sleep=sleep)
    g("u"); g("u")
    now[0] += 5
    g("u")
    assert slept == [1.0]


def test_cli_wayback_writes_resumable_jsonl(tmp_path, monkeypatch):
    import json

    from noroshi import cli, wayback

    monkeypatch.setenv("NOROSHI_ROOT", str(tmp_path))
    monkeypatch.setattr(wayback, "throttled", lambda get, **kw: get)
    monkeypatch.setattr(wayback.httpx, "get", FakeGet({"cdx": Resp([["timestamp"], ["20200102000000"]])}))
    src = tmp_path / "t.csv"
    src.write_text("domain,host,path\nx.org,www.x.org,/.well-known/security.txt\n")
    cli.main(["wayback", "--first-seen", str(src)])
    cli.main(["wayback", "--first-seen", str(src)])  # second run adds nothing
    lines = (tmp_path / "data" / "raw" / "wayback.jsonl").read_text().splitlines()
    assert [json.loads(x) for x in lines] == [{"domain": "x.org", "host": "www.x.org",
                                               "path": "/.well-known/security.txt", "first_seen": "2020-01-02"}]


# ---- B9: the pilot has its own files, and a resume survives a cut-off line

def test_cli_wayback_pilot_writes_pilot_files_only(tmp_path, monkeypatch):
    import json

    from noroshi import cli, wayback

    monkeypatch.setenv("NOROSHI_ROOT", str(tmp_path))
    monkeypatch.setattr(wayback, "throttled", lambda get, **kw: get)
    monkeypatch.setattr(wayback.httpx, "get", FakeGet({"cdx": Resp([["timestamp"], ["20200102000000"]])}))
    first = tmp_path / "f.csv"
    first.write_text("domain,host,path\nx.org,www.x.org,/.well-known/security.txt\n")
    redirects = tmp_path / "r.csv"
    redirects.write_text("domain,host\ndead.org,dead.org\n")
    monkeypatch.setattr(wayback, "last_redirect_target", lambda host, get: "newowner.com")
    cli.main(["wayback", "--store", "pilot", "--first-seen", str(first), "--redirects", str(redirects)])
    raw = tmp_path / "data" / "raw"
    assert json.loads((raw / "pilot_wayback.jsonl").read_text())["first_seen"] == "2020-01-02"
    assert json.loads((raw / "pilot_wayback_redirects.jsonl").read_text())["redirect_target"] == "newowner.com"
    assert not (raw / "wayback.jsonl").exists() and not (raw / "wayback_redirects.jsonl").exists()


def test_cli_wayback_resume_ignores_a_truncated_last_line(tmp_path, monkeypatch):
    import json

    from noroshi import cli, wayback

    monkeypatch.setenv("NOROSHI_ROOT", str(tmp_path))
    monkeypatch.setattr(wayback, "throttled", lambda get, **kw: get)
    monkeypatch.setattr(wayback.httpx, "get", FakeGet({"cdx": Resp([["timestamp"], ["20200102000000"]])}))
    src = tmp_path / "t.csv"
    src.write_text("domain,host,path\nx.org,www.x.org,/a\ny.org,www.y.org,/b\n")
    out = tmp_path / "data" / "raw" / "wayback.jsonl"
    out.parent.mkdir(parents=True)
    out.write_text(json.dumps({"domain": "x.org", "host": "www.x.org", "path": "/a", "first_seen": "2019-01-01"})
                   + '\n{"domain": "y.org", "host": "www.y')  # killed mid-write
    cli.main(["wayback", "--first-seen", str(src)])
    rows = [json.loads(x) for x in out.read_text().splitlines() if x.endswith("}")]
    assert [(r["domain"], r["first_seen"]) for r in rows] == [("x.org", "2019-01-01"), ("y.org", "2020-01-02")]


# ---- after the freeze: archive lookup pacing, and a stop when the archive refuses or keeps failing

class Clock:
    def __init__(self):
        self.now, self.sent = 1000.0, []

    def clock(self):
        return self.now

    def sleep(self, secs):
        self.now += secs


def _paced(monkeypatch, clock):
    """The command's real pacing, on a fake clock: the interval it asks for is recorded."""
    from noroshi import wayback

    real, asked = wayback.throttled, []

    def throttled(get, interval):
        asked.append(interval)
        return real(get, interval, clock=clock.clock, sleep=clock.sleep)
    monkeypatch.setattr(wayback, "throttled", throttled)
    return asked


def _refused(url, **kw):
    try:
        raise ConnectionRefusedError(61, "Connection refused")
    except ConnectionRefusedError as exc:
        import httpx
        raise httpx.ConnectError("[Errno 61] Connection refused") from exc


def test_every_request_to_the_archive_is_paced_four_and_a_half_seconds_apart(tmp_path, monkeypatch):
    from noroshi import cli, config, wayback

    monkeypatch.setenv("NOROSHI_ROOT", str(tmp_path))
    clock = Clock()
    asked = _paced(monkeypatch, clock)
    routes = FakeGet({"cdx/search": Resp([["timestamp", "statuscode"], ["20230101000000", "301"]]),
                      "/web/20230101000000id_/": Resp(None, 302, {"location": "/web/1id_/https://new.com/"})})

    def get(url, **kw):
        clock.sent.append(clock.now)
        return routes(url, **kw)
    monkeypatch.setattr(wayback.httpx, "get", get)
    first, redirects = tmp_path / "f.csv", tmp_path / "r.csv"
    first.write_text("domain,host,path\nx.org,www.x.org,/a\ny.org,www.y.org,/b\n")
    redirects.write_text("domain,host\ndead.org,dead.org\n")
    cli.main(["wayback", "--first-seen", str(first), "--redirects", str(redirects)])
    assert config.WAYBACK_INTERVAL_SECONDS == 4.5 and asked == [4.5]
    assert len(clock.sent) == 4  # two first-seen lookups, then a redirect lookup's CDX and replay requests
    assert all(b - a >= 4.5 for a, b in zip(clock.sent, clock.sent[1:]))


def _rows_of(path):
    import json

    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def test_the_run_stops_at_the_first_refused_connection(tmp_path, monkeypatch, capsys):
    from noroshi import cli, wayback

    monkeypatch.setenv("NOROSHI_ROOT", str(tmp_path))
    _paced(monkeypatch, Clock())
    src = tmp_path / "t.csv"
    src.write_text("domain,host,path\na.org,a.org,/a\nb.org,b.org,/b\nc.org,c.org,/c\n")
    asked = []

    def get(url, **kw):
        asked.append(kw["params"]["url"])
        return _refused(url) if kw["params"]["url"] == "b.org/b" else Resp([])
    monkeypatch.setattr(wayback.httpx, "get", get)
    with pytest.raises(SystemExit) as stop:
        cli.main(["wayback", "--first-seen", str(src)])
    assert stop.value.code not in (None, 0) and "refus" in str(stop.value.code) and "again" in str(stop.value.code)
    assert asked == ["a.org/a", "b.org/b"]  # nothing more after the refusal
    out = tmp_path / "data" / "raw" / "wayback.jsonl"
    assert [r["domain"] for r in _rows_of(out)] == ["a.org"]  # nothing written for the refused lookup
    asked.clear()
    monkeypatch.setattr(wayback.httpx, "get", lambda url, **kw: asked.append(kw["params"]["url"]) or Resp([]))
    cli.main(["wayback", "--first-seen", str(src)])  # the same command later: stored lookups are not asked again
    assert asked == ["b.org/b", "c.org/c"] and [r["domain"] for r in _rows_of(out)] == ["a.org", "b.org", "c.org"]


def test_the_run_stops_after_three_failed_lookups_in_a_row(tmp_path, monkeypatch):
    from noroshi import cli, wayback

    monkeypatch.setenv("NOROSHI_ROOT", str(tmp_path))
    _paced(monkeypatch, Clock())
    names = ["a", "b", "c", "d", "e", "f", "g"]
    src = tmp_path / "t.csv"
    src.write_text("domain,host,path\n" + "".join(f"{n}.org,{n}.org,/x\n" for n in names))
    failing, asked = {"b", "c", "e", "f", "g"}, []  # two failures, a success, then three failures

    def get(url, **kw):
        n = kw["params"]["url"][0]
        asked.append(n)
        return Resp(None, status=503) if n in failing else Resp([])
    monkeypatch.setattr(wayback.httpx, "get", get)
    with pytest.raises(SystemExit) as stop:
        cli.main(["wayback", "--first-seen", str(src)])
    assert stop.value.code not in (None, 0) and "fail" in str(stop.value.code) and "again" in str(stop.value.code)
    assert asked == names  # the third failure in a row (g) ends it; two in a row did not
    assert [r["domain"] for r in _rows_of(tmp_path / "data" / "raw" / "wayback.jsonl")] == ["a.org", "d.org"]
