from dataclasses import fields

import pytest

from noroshi.store import FetchRecord, Store, is_answer


def make_record(**kw):
    base = dict(
        domain="a.org", sector="hospital", host="www.a.org", url="https://www.a.org/",
        purpose="homepage", pass_no=1, ua_kind="identified", started_at="2026-11-15T00:00:00Z",
        status=200, final_url="https://www.a.org/", redirect_chain=[], content_type="text/html",
        body_sha256=None, body_len=0, truncated=False, error=None, blocked=False,
    )
    base.update(kw)
    return FetchRecord(**base)


def test_fields_match_contract():
    assert [f.name for f in fields(FetchRecord)] == [
        "domain", "sector", "host", "url", "purpose", "pass_no", "ua_kind", "started_at",
        "status", "final_url", "redirect_chain", "content_type", "body_sha256", "body_len",
        "truncated", "error", "blocked", "site", "reused", "error_detail", "error_url"]


def test_roundtrip(tmp_path):
    s = Store(tmp_path / "c.sqlite")
    s.record(make_record(domain="a.org", pass_no=1, redirect_chain=["http://a.org/"]), b"hello")
    got = s.requests_for("a.org", 1)
    assert len(got) == 1 and s.get_blob(got[0].body_sha256) == b"hello"
    assert got[0].redirect_chain == ["http://a.org/"] and got[0].blocked is False
    assert got[0].body_len == 5
    assert s.count_requests("a.org", 1) == 1 and s.count_requests("a.org", 2) == 0


def test_no_body_and_errors(tmp_path):
    s = Store(tmp_path / "c.sqlite")
    s.record(make_record(status=None, error="timeout", final_url=None, content_type=None), None)
    got = s.requests_for("a.org", 1)[0]
    assert got.body_sha256 is None and got.error == "timeout" and got.status is None


def test_same_body_stored_once(tmp_path):
    s = Store(tmp_path / "c.sqlite")
    s.record(make_record(url="https://www.a.org/1"), b"same")
    s.record(make_record(url="https://www.a.org/2"), b"same")
    assert s.count_requests("a.org", 1) == 2
    assert len(list((tmp_path / "blobs").iterdir())) == 1


def test_done_marker(tmp_path):
    s = Store(tmp_path / "c.sqlite")
    assert s.done_status("a.org", 1) is None
    s.mark_done("a.org", 1, "measured")
    assert s.done_status("a.org", 1) == "measured"
    assert s.done_status("a.org", 2) is None
    s.mark_done("a.org", 1, "failed")
    assert s.done_status("a.org", 1) == "failed"


def test_blob_write_is_atomic(tmp_path, monkeypatch):
    import os

    import pytest

    s = Store(tmp_path / "c.sqlite")

    def boom(src, dst):
        raise OSError("killed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        s.record(make_record(), b"hello")
    assert list(s.blob_dir.iterdir()) == []  # no truncated blob, no leftover temp file
    monkeypatch.undo()
    s.record(make_record(), b"hello")
    names = [p.name for p in s.blob_dir.iterdir()]
    assert len(names) == 1 and len(names[0]) == 64 and not names[0].startswith(".")


def test_close_and_context_manager(tmp_path):
    import sqlite3

    import pytest

    with Store(tmp_path / "c.sqlite") as s:
        s.record(make_record(), None)
    with pytest.raises(sqlite3.ProgrammingError):
        s.count_requests("a.org", 1)
    s2 = Store(tmp_path / "c.sqlite")
    assert s2.count_requests("a.org", 1) == 1
    s2.close()


def test_records_by_purpose(tmp_path):
    from noroshi.store import FetchRecord, Store

    def rec(purpose):
        return FetchRecord("a.org", "hospital", "a.org", "https://a.org/" + purpose, purpose, 1, "identified", "t",
                           200, None, [], "text/html", None, 0, False, None, False)

    with Store(tmp_path / "s.sqlite") as store:
        for p in ("candidate", "probe", "robots"):
            store.record(rec(p), b"body " + p.encode())
        got = store.records_by_purpose(("candidate", "robots"))
        assert [r.purpose for r in got] == ["candidate", "robots"] and got[0].redirect_chain == []
        assert got[0].body_sha256


def test_wal_mode_and_thirty_second_busy_timeout(tmp_path):
    with Store(tmp_path / "c.sqlite") as s:
        assert s.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert s.db.execute("PRAGMA busy_timeout").fetchone()[0] == 30_000


def test_in_flight_row_counts_and_is_completed_in_place(tmp_path):
    with Store(tmp_path / "c.sqlite") as s:
        rec = make_record(status=None, final_url=None, site="a.org", purpose="robots")
        row = s.begin(rec)
        got = s.requests_for("a.org", 1)
        assert len(got) == 1 and got[0].error == "in_flight" and got[0].status is None
        assert s.count_requests("a.org", 1) == 1 and s.count_site_requests("a.org", 1) == 1
        rec.status, rec.error, rec.final_url = 200, None, "https://www.a.org/"
        s.finish(row, rec, b"hello")
        got = s.requests_for("a.org", 1)
        assert len(got) == 1 and got[0].error is None and got[0].status == 200
        assert s.get_blob(got[0].body_sha256) == b"hello" and got[0].body_len == 5


def test_site_counts_span_listed_domains_and_skip_reused_rows(tmp_path):
    with Store(tmp_path / "c.sqlite") as s:
        s.record(make_record(domain="a.org", site="hub.org", purpose="robots"), None)
        s.record(make_record(domain="b.org", site="hub.org", purpose="probe"), None)
        s.record(make_record(domain="c.org", site="hub.org", purpose="probe", reused=True), None)
        s.record(make_record(domain="b.org", site="hub.org", purpose="probe", pass_no=2), None)
        assert s.count_site_requests("hub.org", 1) == 2 and s.count_site_requests("hub.org", 2) == 1
        assert s.count_requests("c.org", 1) == 0  # a reused copy counts toward no cap
        assert s.requests_for("c.org", 1)[0].reused is True


def test_find_measurement_in_this_pass_only(tmp_path):
    with Store(tmp_path / "c.sqlite") as s:
        url = "https://hub.org/robots.txt"
        for kw in (dict(status=503), dict(status=403, blocked=True), dict(status=None, error="timeout")):
            s.record(make_record(url=url, **kw), None)
        s.begin(make_record(url=url, status=None))
        s.record(make_record(url=url, status=404, pass_no=2), None)
        assert s.find_measurement(url, 1) is None
        s.record(make_record(domain="a.org", url=url, status=404), b"nope")
        got = s.find_measurement(url, 1)
        assert got.status == 404 and got.domain == "a.org" and got.body_sha256


def test_homepage_attempts_are_not_counted_in_the_site_cap(tmp_path):
    """Rulings round, R4: a listed domain's own homepage attempts count toward its own cap, not the site cap."""
    with Store(tmp_path / "c.sqlite") as s:
        s.record(make_record(domain="hub.org", site="hub.org", purpose="homepage"), None)
        s.begin(make_record(domain="hub.org", site="hub.org", purpose="homepage", ua_kind="browser"))
        s.record(make_record(domain="a.org", site="hub.org", purpose="robots"), None)
        assert s.count_site_requests("hub.org", 1) == 1 and s.count_requests("hub.org", 1) == 2


# ---- closing fixes, C2: one definition of an answer

def test_an_answer_is_a_completed_unrefused_200_or_4xx():
    answers = [(200, False, None), (404, False, None), (410, False, None), (400, False, None)]
    others = [(202, False, None), (204, False, None), (206, False, None), (301, False, None), (304, False, None),
              (503, False, None), (403, True, None), (429, True, None), (200, True, None), (None, False, "timeout"),
              (200, False, "internal"), (429, True, "cancelled"), (None, False, "in_flight")]
    assert all(is_answer(*a) for a in answers)
    assert not any(is_answer(*o) for o in others)


@pytest.mark.parametrize("status", [202, 204, 206, 301, 304])
def test_a_record_that_is_not_an_answer_is_never_reused(tmp_path, status):
    with Store(tmp_path / "c.sqlite") as s:
        url = "https://hub.org/robots.txt"
        s.record(make_record(url=url, status=status), None)
        s.record(make_record(url=url, status=200, error="internal"), None)
        assert s.find_measurement(url, 1) is None
        s.record(make_record(url=url, status=404), None)
        s.record(make_record(url=url, status=status), None)
        assert s.find_measurement(url, 1).status == 404  # the latest answer, not the later record


# ---- post-pilot X7: the error text of a failed attempt is kept

def test_error_detail_round_trips(tmp_path):
    with Store(tmp_path / "c.sqlite") as s:
        s.record(make_record(status=None, error="tls", error_detail="ConnectError: certificate has expired"), None)
        s.record(make_record(url="https://www.a.org/2"), None)
        got = s.requests_for("a.org", 1)
        assert [r.error_detail for r in got] == ["ConnectError: certificate has expired", None]


def test_a_store_written_before_error_detail_still_opens(tmp_path):
    import sqlite3

    old = [c for c in (f.name for f in fields(FetchRecord)) if c not in ("error_detail", "error_url")]
    db = sqlite3.connect(tmp_path / "c.sqlite")
    db.execute(f"CREATE TABLE requests (id INTEGER PRIMARY KEY AUTOINCREMENT, {', '.join(old)})")
    db.execute(f"INSERT INTO requests ({', '.join(old)}) VALUES ({', '.join('?' * len(old))})",
               ["a.org", "hospital", "www.a.org", "https://www.a.org/", "homepage", 1, "identified",
                "2026-11-16T12:44:24Z", None, None, "[]", None, None, 0, 0, "dns", 0, "a.org", 0])
    db.commit()
    db.close()
    with Store(tmp_path / "c.sqlite") as s:
        old_row, = s.requests_for("a.org", 1)
        assert old_row.error == "dns" and old_row.error_detail is None
        s.record(make_record(status=None, error="dns", error_detail="ConnectError: no such host"), None)
        assert s.requests_for("a.org", 1)[-1].error_detail == "ConnectError: no such host"
