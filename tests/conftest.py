import atexit
import os
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

import synth
from noroshi import config

_real_getaddrinfo = socket.getaddrinfo


@pytest.fixture(autouse=True)
def local_dns(monkeypatch):
    """A strict resolver for every test: localhost, `*.test` names and names a test adds to the
    returned set resolve to 127.0.0.1; every other name fails, so no test can reach a real site."""
    names: set[str] = set()

    def gai(host, port, family=0, type=0, proto=0, flags=0):
        h = (host.decode() if isinstance(host, bytes) else host or "").lower().rstrip(".")
        if h in ("", "localhost", "127.0.0.1", "::1"):
            return _real_getaddrinfo(host, port, family, type, proto, flags)
        if h.endswith(".test") or h in names:
            return _real_getaddrinfo("127.0.0.1", port, socket.AF_INET, type, proto, flags)
        raise socket.gaierror(socket.EAI_NONAME, f"the test resolver refuses {h!r}")

    monkeypatch.setattr(socket, "getaddrinfo", gai)
    return names


@pytest.fixture
def synthetic(tmp_path):
    store, frame, judg, reg = synth.build(tmp_path)
    yield store, frame, judg, reg, tmp_path / "wayback.jsonl"
    store.close()


CHECKOUT_ROOT = config.REPO  # the code's own default root: the checkout, which holds the study's real files
STAND_IN_DIR = Path(tempfile.mkdtemp(prefix="noroshi-no-model-"))
STAND_IN = STAND_IN_DIR / "claude"
STAND_IN.write_text("#!/bin/sh\necho 'tests must not call a model' >&2\nexit 97\n")
STAND_IN.chmod(0o755)
atexit.register(shutil.rmtree, STAND_IN_DIR, True)


def assert_stand_in_first() -> None:
    """The first `claude` on PATH is the failing stand-in (a test that changed PATH must keep it first)."""
    found = shutil.which("claude")
    assert found == str(STAND_IN), f"the first claude on PATH is {found}, not the failing stand-in {STAND_IN}"


@pytest.fixture(autouse=True)
def no_model_command():
    """No test may run the real `claude` command: a stand-in that fails loudly is found first on PATH, so a call that
    a test forgot to fake ends in an error instead of reaching a model. After the test, the stand-in must still be
    first (a test that set PATH itself and hid it fails). Its own patcher: a test's `monkeypatch.undo()` keeps it."""
    guard = pytest.MonkeyPatch()
    guard.setenv("PATH", f"{STAND_IN_DIR}:{os.environ.get('PATH', '')}")
    try:
        yield
        assert_stand_in_first()
    finally:
        guard.undo()


@pytest.fixture(autouse=True)
def temporary_root(tmp_path_factory):
    """No test may read or write the study's own files: every test gets a temporary project root (`NOROSHI_ROOT`), and
    the code's default root is moved there too, so `config.paths()` never resolves into the checkout's data/, logs/,
    results/ or info/, even in a test that removes the variable or undoes its own patches. A test that needs a
    committed input reads it by an explicit path."""
    root = tmp_path_factory.mktemp("root")
    guard = pytest.MonkeyPatch()
    guard.setenv("NOROSHI_ROOT", str(root))
    guard.setattr(config, "REPO", root)
    try:
        yield root
    finally:
        guard.undo()
