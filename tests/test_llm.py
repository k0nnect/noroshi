import json
import subprocess

from noroshi.llm import ask_claude, extract_json


class Fake:
    def __init__(self, result="{\"ok\": true}", code=0):
        self.calls = []
        self.result, self.code = result, code

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        out = json.dumps({"result": self.result, "is_error": self.code != 0})
        return subprocess.CompletedProcess(argv, self.code, stdout=out, stderr="boom")


def test_ask_claude_builds_command_and_logs(tmp_path):
    fake = Fake()
    log = tmp_path / "llm_log.jsonl"
    res = ask_claude("hi", "claude-sonnet-5-5", "sys", "test", runner=fake, log_path=log)
    argv, kw = fake.calls[0]
    assert "-p" in argv and argv[argv.index("--model") + 1] == "claude-sonnet-5-5"
    assert argv[argv.index("--system-prompt") + 1] == "sys"
    assert argv[argv.index("--tools") + 1] == ""
    assert "--no-session-persistence" in argv and "--strict-mcp-config" in argv
    assert kw["input"] == "hi"
    assert res.text == '{"ok": true}' and res.model == "claude-sonnet-5-5"
    lines = log.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["purpose"] == "test" and rec["prompt"] == "hi" and rec["response"] == '{"ok": true}'
    assert {"model", "system", "started_at", "duration_ms"} <= rec.keys()


def test_ask_claude_passes_tools(tmp_path):
    fake = Fake()
    ask_claude("hi", "m", "s", "p", tools=("WebSearch",), runner=fake, log_path=tmp_path / "l.jsonl")
    argv = fake.calls[0][0]
    assert argv[argv.index("--tools") + 1] == "WebSearch"
    assert argv[argv.index("--allowedTools") + 1] == "WebSearch"


def test_ask_claude_raises_on_failure(tmp_path):
    import pytest
    with pytest.raises(RuntimeError):
        ask_claude("hi", "m", "s", "p", runner=Fake(code=1), log_path=tmp_path / "l.jsonl")


def test_extract_json_from_fenced_reply():
    assert extract_json('```json\n[{"n":1}]\n```') == [{"n": 1}]


def test_extract_json_with_prose_around():
    assert extract_json('Sure: {"domain": null} done') == {"domain": None}


def test_failed_call_is_logged_with_status(tmp_path):
    import pytest
    log = tmp_path / "l.jsonl"
    with pytest.raises(RuntimeError):
        ask_claude("hi", "m", "s", "p", runner=Fake(code=1), log_path=log)
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["status"] == "error" and "boom" in rec["error"] and rec["prompt"] == "hi"


def test_timeout_is_logged_and_reraised(tmp_path):
    import pytest

    def runner(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)

    log = tmp_path / "l.jsonl"
    with pytest.raises(subprocess.TimeoutExpired):
        ask_claude("hi", "m", "s", "p", runner=runner, log_path=log)
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["status"] == "timeout"


def test_success_log_has_ok_status(tmp_path):
    log = tmp_path / "l.jsonl"
    ask_claude("hi", "m", "s", "p", runner=Fake(), log_path=log)
    assert json.loads(log.read_text())["status"] == "ok"


def test_meta_is_logged_as_its_own_field(tmp_path):
    """Post-pilot Y1: the page's address travels beside the prompt, in the log only."""
    log = tmp_path / "l.jsonl"
    meta = {"url": "https://a.org/vdp", "final_url": "https://www.a.org/vdp", "domain": "a.org"}
    ask_claude("hi", "m", "s", "judge_policy", runner=Fake(), log_path=log, meta=meta)
    rec = json.loads(log.read_text())
    assert rec["meta"] == meta and rec["prompt"] == "hi"
    with __import__("pytest").raises(RuntimeError):
        ask_claude("hi", "m", "s", "p", runner=Fake(code=1), log_path=log, meta={"domain": "b.org"})
    assert json.loads(log.read_text().splitlines()[1])["meta"] == {"domain": "b.org"}
    ask_claude("hi", "m", "s", "resolve_memory", runner=Fake(), log_path=log)
    assert "meta" not in json.loads(log.read_text().splitlines()[2])  # a call without meta logs none


def test_a_cut_last_line_is_finished_before_the_next_call_is_logged(tmp_path):
    """Fix round 1, M5: after a hard kill the next row must not be fused onto the cut one."""
    log = tmp_path / "l.jsonl"
    log.write_text('{"purpose": "judge_policy", "model": "m", "meta": {"url": "https://a.org/x"}, "prompt": "cut sh')
    ask_claude("hi", "m", "s", "p", runner=Fake(), log_path=log, meta={"domain": "b.org"})
    first, second = log.read_text().split("\n")[:2]
    assert first.endswith('"prompt": "cut sh') and json.loads(second)["meta"] == {"domain": "b.org"}
    assert log.read_text().endswith("\n")
