"""The guards `tests/conftest.py` puts around every test (fix round 2, H1): no test can reach a model, and no test can
read or write the study's own files under the checkout, whatever it forgets."""
import os
import shutil
from pathlib import Path

import pytest

import conftest
from noroshi import config, llm

CHECKOUT = Path(__file__).resolve().parents[1]


def _inside_checkout(path: Path) -> bool:
    return Path(path).resolve().is_relative_to(CHECKOUT)


def test_a_test_that_sets_nothing_gets_a_root_outside_the_checkout(monkeypatch):
    assert not _inside_checkout(config.paths().root)
    for p in (config.paths().raw, config.paths().llm_log, config.paths().results, config.paths().info):
        assert not _inside_checkout(p), p
    monkeypatch.delenv("NOROSHI_ROOT")  # even a test that removes the variable gets no path in the checkout
    assert not _inside_checkout(config.paths().root)
    assert conftest.CHECKOUT_ROOT == CHECKOUT  # the code's own default is the checkout; the guard replaces it


def test_a_model_call_with_no_fake_fails_and_logs_under_the_temporary_root():
    assert not _inside_checkout(config.paths().llm_log)  # checked first, so this test itself never writes there
    with pytest.raises(RuntimeError, match="exit 97"):
        llm.ask_claude("hi", "claude-haiku-5-5", "s", "probe")  # the default runner: the stand-in answers
    row = config.paths().llm_log.read_text()
    assert '"purpose": "probe"' in row and not _inside_checkout(config.paths().llm_log)


def test_the_stand_in_is_first_on_path_and_its_check_fails_when_a_test_hides_it(tmp_path, monkeypatch):
    assert shutil.which("claude") == str(conftest.STAND_IN)
    conftest.assert_stand_in_first()
    elsewhere = tmp_path / "bin"
    elsewhere.mkdir()
    (elsewhere / "claude").write_text("#!/bin/sh\nexit 0\n")
    (elsewhere / "claude").chmod(0o755)
    with monkeypatch.context() as m:  # a test that puts another claude first fails the check (undone before teardown)
        m.setenv("PATH", f"{elsewhere}:{os.environ['PATH']}")
        with pytest.raises(AssertionError, match="stand-in"):
            conftest.assert_stand_in_first()
