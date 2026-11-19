"""Model calls through the `claude` command line, every one logged."""
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from noroshi import config

_log_lock = threading.Lock()
_workdir: str | None = None
_stop = threading.Event()  # set by SIGINT, SIGTERM or SIGHUP during a model run (`stop_on_signals`)


class CallsStopped(BaseException):
    """A signal asked the model run to stop: no new model call starts. Not an Exception, so no retry loop catches it;
    the run ends once the calls in flight have finished and been logged."""


def stop_requested() -> bool:
    return _stop.is_set()


def check_stop() -> None:
    """Raise CallsStopped once a signal asked the run to stop."""
    if _stop.is_set():
        raise CallsStopped("a signal stopped the run: no new model call starts")


@contextmanager
def stop_on_signals(signals=(signal.SIGINT, signal.SIGTERM, signal.SIGHUP)):
    """For the length of a model run (`noroshi judge`, `noroshi identity`): SIGINT, SIGTERM and SIGHUP (the terminal
    closed) only ask the run to stop. The handler raises nothing, so no thread is cut off mid-call; the calls in flight
    run in their own session (a terminal's Ctrl-C does not reach them), finish and are logged; then the run ends and
    its command exits."""
    _stop.clear()
    previous = {s: signal.signal(s, lambda signum, frame: _stop.set()) for s in signals}
    try:
        yield
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)
        _stop.clear()


@dataclass(frozen=True)
class LlmResult:
    text: str
    model: str
    started_at: str
    duration_ms: int


def _neutral_dir() -> str:
    """A working directory outside the repo, so no project instructions are found. Made once, also when calls run
    on several threads."""
    global _workdir
    with _log_lock:
        if _workdir is None:
            _workdir = tempfile.mkdtemp(prefix="noroshi-llm-")
    return _workdir


def build_command(model: str, system: str, tools: tuple[str, ...]) -> list[str]:
    """Clean session on the logged-in subscription: no settings (hooks, plugins, CLAUDE.md
    from user/project/local), no MCP servers, no skills, no session file, only `tools`.
    The prompt goes on stdin."""
    argv = [
        "claude", "-p", "--model", model, "--system-prompt", system,
        "--tools", ",".join(tools),
        "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands",
        "--no-session-persistence", "--output-format", "json",
    ]
    if tools:
        argv += ["--allowedTools", ",".join(tools)]
    return argv


def ask_claude(prompt: str, model: str, system: str, purpose: str,
               tools: tuple[str, ...] = (), timeout: int = 300,
               runner=subprocess.run, log_path: Path | None = None, meta: dict | None = None) -> LlmResult:
    """One model call, logged whatever happens. `meta` (a small dict, for example the address of the page a judge
    call is about) is written to the log row as its own field and never reaches the model. No call starts once a
    signal asked the run to stop (CallsStopped); the model process runs in its own session, so a Ctrl-C sent to the
    run's process group does not cut a call short."""
    check_stop()
    argv = build_command(model, system, tools)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    t0 = time.monotonic()
    def log(record: dict) -> None:
        path = log_path or config.paths().llm_log
        with _log_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("ab+") as fh:
                fh.seek(0, os.SEEK_END)
                cut = False
                if fh.tell():
                    fh.seek(-1, os.SEEK_END)
                    cut = fh.read(1) != b"\n"  # a line cut off by a killed run: finish it first
                fh.write((("\n" if cut else "") + json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))

    base = {"purpose": purpose, "model": model}
    if meta is not None:
        base["meta"] = meta  # before the prompt, so a line cut off by a killed run still carries it
    base.update(system=system, prompt=prompt, started_at=started)
    try:
        proc = runner(argv, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=_neutral_dir(),
                      start_new_session=True)
    except subprocess.TimeoutExpired:
        log({**base, "response": None, "status": "timeout", "error": f"timed out after {timeout}s",
             "duration_ms": int((time.monotonic() - t0) * 1000)})
        raise
    duration_ms = int((time.monotonic() - t0) * 1000)
    try:
        payload = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        payload = {}
    if proc.returncode != 0 or payload.get("is_error") or "result" not in payload:
        error = f"claude call failed (exit {proc.returncode}): {(proc.stderr or proc.stdout or '')[:500]}"
        log({**base, "response": None, "status": "error", "error": error, "duration_ms": duration_ms})
        raise RuntimeError(error)
    text = payload["result"]
    log({**base, "response": text, "status": "ok", "duration_ms": duration_ms})
    return LlmResult(text=text, model=model, started_at=started, duration_ms=duration_ms)


def extract_json(text: str):
    """The first JSON value in a reply, ignoring code fences and surrounding prose."""
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "[{":
            try:
                return decoder.raw_decode(text, i)[0]
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON value in reply")
