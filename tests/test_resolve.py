import json
import threading

import pandas as pd

from noroshi.llm import LlmResult
from noroshi.resolve import memory_pass, reconcile, search_pass, to_domain_table


def _result(text):
    return LlmResult(text=text, model="m", started_at="t", duration_ms=1)


def _rows(n):
    return pd.DataFrame({"key": [f"k{i}" for i in range(n)], "name": [f"Name {i}" for i in range(n)]})


def _lines(path):
    return [json.loads(x) for x in path.read_text().splitlines()]


def fake_memory(calls):
    def ask(prompt, model, system, purpose, tools=(), **kw):
        calls.append((prompt, model, system, tools))
        n = prompt.count("\n") and len([l for l in prompt.splitlines() if l[:1].isdigit() and ". " in l[:5]])
        return _result(json.dumps([{"n": i, "domain": f"d{i}.com"} for i in range(1, n + 1)]))
    return ask


def test_memory_pass_batches_25(tmp_path):
    calls = []
    out = tmp_path / "a.jsonl"
    memory_pass(_rows(60), "ctx", model="claude-sonnet-5-5", out=out, ask=fake_memory(calls))
    assert len(calls) == 3
    assert calls[0][1] == "claude-sonnet-5-5" and calls[0][2] == "You answer only with JSON."
    assert "Context: ctx" in calls[0][0] and "1. Name 0" in calls[0][0] and "25. Name 24" in calls[0][0]
    lines = _lines(out)
    assert len(lines) == 60 and lines[26]["key"] == "k26" and lines[26]["domain"] == "d2.com"


def test_memory_pass_resumes_and_skips_done(tmp_path):
    calls = []
    out = tmp_path / "a.jsonl"
    memory_pass(_rows(30), "ctx", model="m", out=out, ask=fake_memory(calls))
    out.write_text("".join(out.read_text().splitlines(keepends=True)[:25]))  # keep first batch only
    calls.clear()
    memory_pass(_rows(30), "ctx", model="m", out=out, ask=fake_memory(calls))
    assert len(calls) == 1 and "1. Name 25" in calls[0][0]
    assert [l["key"] for l in _lines(out)] == [f"k{i}" for i in range(30)]


def test_memory_pass_retries_unparseable_once_then_marks_failed(tmp_path):
    n = {"c": 0}

    def ask(prompt, model, system, purpose, tools=(), **kw):
        n["c"] += 1
        return _result("sorry, no")

    out = tmp_path / "a.jsonl"
    memory_pass(_rows(3), "ctx", model="m", out=out, ask=ask)
    assert n["c"] == 2
    lines = _lines(out)
    assert len(lines) == 3 and all(l["domain"] is None and l["parse_failed"] is True for l in lines)


def test_memory_pass_retry_succeeds(tmp_path):
    seq = iter(["garbage", '[{"n": 1, "domain": "https://www.Philips.com/x"}]'])
    out = tmp_path / "a.jsonl"
    memory_pass(_rows(1), "ctx", model="m", out=out, ask=lambda *a, **k: _result(next(seq)))
    (line,) = _lines(out)
    assert line["domain"] == "philips.com" and "parse_failed" not in line


def test_search_pass_one_call_per_name_with_websearch(tmp_path):
    seen = []
    lock = threading.Lock()

    def ask(prompt, model, system, purpose, tools=(), **kw):
        with lock:
            seen.append((prompt, model, tools))
        return _result('{"domain": "x.com", "evidence_url": "https://x.com"}')

    out = tmp_path / "s.jsonl"
    search_pass(_rows(5), "ctx", out=out, ask=ask, workers=3)
    assert len(seen) == 5 and all(t == ("WebSearch",) for _, _, t in seen)
    assert all(m == "claude-sonnet-5-5" for _, m, _ in seen)
    assert 'organization: "Name 0". Context: ctx.' in sorted(s[0] for s in seen)[0]
    assert len(_lines(out)) == 5
    search_pass(_rows(6), "ctx", out=out, ask=ask, workers=3)
    assert len(seen) == 6 and len(_lines(out)) == 6


def test_search_pass_parse_failure(tmp_path):
    out = tmp_path / "s.jsonl"
    search_pass(_rows(1), "ctx", out=out, ask=lambda *a, **k: _result("nope"), workers=1)
    (line,) = _lines(out)
    assert line["domain"] is None and line["parse_failed"] is True


def test_memory_pass_failed_call_logged_retried_then_left_unwritten(tmp_path):
    n = {"c": 0}

    def ask(*a, **k):
        n["c"] += 1
        raise RuntimeError("boom")

    out = tmp_path / "a.jsonl"
    memory_pass(_rows(3), "ctx", model="m", out=out, ask=ask)
    assert n["c"] == 2 and not out.exists()
    memory_pass(_rows(3), "ctx", model="m", out=out, ask=fake_memory([]))
    assert [l["key"] for l in _lines(out)] == ["k0", "k1", "k2"]


def test_memory_pass_timeout_then_success(tmp_path):
    import subprocess
    seq = iter([subprocess.TimeoutExpired("claude", 1), None])

    def ask(*a, **k):
        e = next(seq)
        if e:
            raise e
        return _result('[{"n": 1, "domain": "a.com"}]')

    out = tmp_path / "a.jsonl"
    memory_pass(_rows(1), "ctx", model="m", out=out, ask=ask)
    (line,) = _lines(out)
    assert line["domain"] == "a.com" and "parse_failed" not in line


def test_search_pass_failure_does_not_abort_and_is_retried_on_restart(tmp_path):
    def bad_one(prompt, *a, **k):
        if '"Name 1"' in prompt:
            raise RuntimeError("boom")
        return _result('{"domain": "x.com", "evidence_url": null}')

    out = tmp_path / "s.jsonl"
    search_pass(_rows(3), "ctx", out=out, ask=bad_one, workers=2)
    assert sorted(l["key"] for l in _lines(out)) == ["k0", "k2"]
    search_pass(_rows(3), "ctx", out=out, ask=lambda *a, **k: _result('{"domain": "y.com"}'), workers=2)
    assert sorted(l["key"] for l in _lines(out)) == ["k0", "k1", "k2"]


def test_resume_retries_parse_failed_and_reconcile_takes_last_line(tmp_path):
    out = tmp_path / "a.jsonl"
    memory_pass(_rows(2), "ctx", model="m", out=out, ask=lambda *a, **k: _result("nope"))
    assert all(l.get("parse_failed") for l in _lines(out))
    calls = []
    memory_pass(_rows(2), "ctx", model="m", out=out, ask=fake_memory(calls))
    assert len(calls) == 1
    assert [l.get("parse_failed") for l in _lines(out)] == [True, True, None, None]
    b = tmp_path / "b.jsonl"
    _write(b, [{"key": "k0", "name": "Name 0", "domain": "d1.com"}, {"key": "k1", "name": "Name 1", "domain": "d2.com"}])
    df = reconcile(out, b, None).set_index("key")
    assert df.loc["k0", "resolution"] == "agreed" and df.loc["k1", "resolution"] == "agreed"


def test_passes_take_keyword_only_arguments(tmp_path):
    import pytest
    with pytest.raises(TypeError):
        memory_pass(_rows(1), "ctx", "m", tmp_path / "a.jsonl")
    with pytest.raises(TypeError):
        search_pass(_rows(1), "ctx", tmp_path / "s.jsonl")


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_reconcile_agree_search_unresolved(tmp_path):
    a, b, s = tmp_path / "a", tmp_path / "b", tmp_path / "s"
    _write(a, [{"key": "k1", "name": "One", "domain": "philips.com"},
               {"key": "k2", "name": "Two", "domain": "two.com"},
               {"key": "k3", "name": "Three", "domain": None}])
    _write(b, [{"key": "k1", "name": "One", "domain": "philips.com"},
               {"key": "k2", "name": "Two", "domain": None},
               {"key": "k3", "name": "Three", "domain": "other.com"}])
    _write(s, [{"key": "k2", "name": "Two", "domain": "two.org"},
               {"key": "k3", "name": "Three", "domain": None}])
    df = reconcile(a, b, s).set_index("key")
    assert list(reconcile(a, b, s).columns) == ["key", "name", "domain", "resolution"]
    assert df.loc["k1", "resolution"] == "agreed" and df.loc["k1", "domain"] == "philips.com"
    assert df.loc["k2", "resolution"] == "search" and df.loc["k2", "domain"] == "two.org"
    assert df.loc["k3", "resolution"] == "unresolved" and df.loc["k3", "domain"] == ""


def test_reconcile_compares_registrable_domains(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a, [{"key": "k", "name": "P", "domain": "www.philips.com"}])
    _write(b, [{"key": "k", "name": "P", "domain": "philips.com"}])
    df = reconcile(a, b, None)
    assert df.loc[0, "resolution"] == "agreed" and df.loc[0, "domain"] == "philips.com"


def test_reconcile_without_search_file_leaves_disagreements_unresolved(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a, [{"key": "k", "name": "P", "domain": "a.com"}])
    _write(b, [{"key": "k", "name": "P", "domain": "b.com"}])
    assert reconcile(a, b, None).loc[0, "resolution"] == "unresolved"


def test_to_domain_table(tmp_path):
    resolved = pd.DataFrame({
        "key": ["a", "b", "c"], "name": ["Philips Ultrasound", "Philips Healthcare", "Mystery"],
        "domain": ["philips.com", "philips.com", ""], "resolution": ["agreed", "search", "unresolved"],
    })
    t = to_domain_table(resolved, "device")
    assert list(t.columns) == ["sector", "domain", "start_url", "n_units", "beds_sum", "name_tokens"]
    assert len(t) == 1
    row = t.iloc[0]
    assert row.sector == "device" and row.start_url == "https://philips.com/" and row.n_units == 2
    assert row.name_tokens == "philips ultrasound"
