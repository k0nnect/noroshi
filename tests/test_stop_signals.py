"""Fix round 2, H2: a model run stopped from outside, with a real signal to its process group, as a terminal's Ctrl-C
or a `kill` sends it. The run is a child process (`noroshi identity` or `noroshi judge`) under a temporary root; the
model is a fake `claude` first on the child's PATH that records when it starts, sleeps, and answers. Reviewer's probe:
rrY1/probe_ctrlc_group.py (`_thread.interrupt_main()` cannot reach this path)."""
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import synth
from noroshi.store import Store

SLEEP = 0.8  # how long each fake model call takes
REPLY = {"site": "own", "reason": "the page names it", "invites_reports": True, "contact_method": True,
         "safe_harbor": False}
FAKE = f"""import json, os, sys, time
sys.stdin.read()
with open(os.environ["FAKE_TRACE"], "a") as fh:
    fh.write(repr(time.time()) + "\\n")
time.sleep({SLEEP})
print(json.dumps({{"result": json.dumps({REPLY!r}), "is_error": False}}))
"""
POLICY = (b"<h1>Vulnerability Disclosure Policy</h1><p>To report a security vulnerability email sec@x.org.</p>"
          + b"<p>We thank every researcher who writes to us in good faith about our systems.</p>" * 3)


def _root(tmp_path, command):
    root = tmp_path / "root"
    raw, frames = root / "data" / "raw", root / "data" / "frames"
    (frames / "resolution_passes").mkdir(parents=True)
    raw.mkdir(parents=True)
    domains = [f"d{i}.org" for i in range(6)]
    with Store(raw / "crawl.sqlite") as store:
        for i, d in enumerate(domains):
            synth.rec(store, d, "homepage", synth.HOME.replace(b"welcome", f"welcome {i}".encode()),
                      ctype="text/html", url=f"https://{d}/", sector="device")
            store.mark_done(d, 1, "measured")
            if command == "judge":
                synth.rec(store, d, "candidate", POLICY + f"<p>page {i}</p>".encode(), ctype="text/html",
                          url=f"https://{d}/vdp", sector="device")
    (frames / "device_domains.csv").write_text("sector,domain,start_url,n_units,beds_sum,name_tokens\n" + "".join(
        f"device,{d},https://{d}/,1,,\n" for d in domains))
    for which in ("memory_a", "memory_b"):
        (frames / "resolution_passes" / f"device_{which}.jsonl").write_text("".join(json.dumps(
            {"key": d, "name": f"{d} Medical", "model": "m", "domain": d}) + "\n" for d in domains))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (tmp_path / "fake_claude.py").write_text(FAKE)  # a shell wrapper: the interpreter's path may hold spaces
    (fake_bin / "claude").write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} "
                                     f"{shlex.quote(str(tmp_path / 'fake_claude.py'))} \"$@\"\n")
    (fake_bin / "claude").chmod(0o755)
    return root, fake_bin


def _lines(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def _run_and_stop(tmp_path, command, sig, after_starts):
    root, fake_bin = _root(tmp_path, command)
    trace = tmp_path / "starts.txt"
    args = ["identity", "--workers", "2"] if command == "identity" else ["judge"]
    env = {**os.environ, "NOROSHI_ROOT": str(root), "PATH": f"{fake_bin}:{os.environ['PATH']}",
           "FAKE_TRACE": str(trace)}
    child = subprocess.Popen([sys.executable, "-c", "import sys; from noroshi import cli; cli.main(sys.argv[1:])",
                              *args], cwd=tmp_path, env=env, start_new_session=True, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 30
        while len(_lines(trace)) < after_starts:
            assert time.monotonic() < deadline and child.poll() is None, child.communicate()
            time.sleep(0.01)
        time.sleep(SLEEP / 4)  # every call started so far is running (asleep), and no other can be starting
        sent = time.time()
        os.killpg(child.pid, sig)  # to the whole process group, as a terminal sends Ctrl-C
        out, err = child.communicate(timeout=30)
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
    raw = root / "data" / "raw"
    size = lambda: (raw / "llm_log.jsonl").stat().st_size if (raw / "llm_log.jsonl").exists() else 0  # noqa: E731
    log_size = size()
    time.sleep(SLEEP + 0.3)  # anything a cut-off call would still write
    assert size() == log_size
    return child.returncode, err, [float(x) for x in _lines(trace)], sent, raw


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM], ids=["SIGINT", "SIGTERM"])
@pytest.mark.parametrize("after_starts,decided", [(2, 0), (4, 2)], ids=["during-rater-a", "during-rater-b"])
def test_a_signal_lets_the_identity_calls_in_flight_finish_and_starts_none(tmp_path, sig, after_starts, decided):
    code, err, starts, sent, raw = _run_and_stop(tmp_path, "identity", sig, after_starts)
    calls = [json.loads(x) for x in _lines(raw / "llm_log.jsonl")]
    assert code != 0 and err.strip().splitlines()[-1].startswith("noroshi: the identity check was stopped")
    assert len(starts) == after_starts and all(t < sent for t in starts)  # no call started after the signal
    assert len(calls) == len(starts) and {c["status"] for c in calls} == {"ok"}  # each finished and was logged
    rows = [json.loads(x) for x in _lines(raw / "identity.jsonl")]
    assert len(rows) == decided  # every complete decision is written; a domain in progress is left undecided
    assert sorted(r["domain"] for r in rows) == sorted({c["meta"]["domain"] for c in calls})[:decided]


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM], ids=["SIGINT", "SIGTERM"])
# fix round 3, K8: no row is written once the stop was asked, so the page whose second call was in flight is not
# written either (it was 1 in round 2); it is asked again on the next run
@pytest.mark.parametrize("after_starts,decided", [(1, 0), (2, 0)], ids=["during-judge-a", "during-judge-b"])
def test_a_signal_lets_the_judge_call_in_flight_finish_and_starts_none(tmp_path, sig, after_starts, decided):
    code, err, starts, sent, raw = _run_and_stop(tmp_path, "judge", sig, after_starts)
    calls = [json.loads(x) for x in _lines(raw / "llm_log.jsonl")]
    assert code != 0 and err.strip().splitlines()[-1].startswith("noroshi: judging was stopped")
    assert len(starts) == after_starts and all(t < sent for t in starts)
    assert len(calls) == len(starts) and {c["status"] for c in calls} == {"ok"}
    rows = [json.loads(x) for x in _lines(raw / "judgements.jsonl")]
    assert len([r for r in rows if r.get("final")]) == decided


# ---- fix round 3, K8 to K10: a stop during the last page, during pdftotext, or by SIGHUP

PDF_SLEEP = 2.0  # how long the fake pdftotext reads a page in the stopped run
FAKE_PDFTOTEXT = """import os, sys, time
sys.stdin.buffer.read()
with open(os.environ["FAKE_TRACE"], "a") as fh:
    fh.write("pdftotext\\n")
time.sleep(float(os.environ.get("PDF_SLEEP", "0")))
print("Vulnerability Disclosure Policy. To report a security vulnerability email security@d0.org. " * 4)
"""


def _one_page(tmp_path, body, ctype):
    """A root whose store holds one measured domain with one stored page, and a fake `claude` and `pdftotext`."""
    root, fake_bin = _root(tmp_path, "identity")  # six measured homepages, no page yet
    with Store(root / "data" / "raw" / "crawl.sqlite") as store:
        synth.rec(store, "d0.org", "candidate", body, ctype=ctype, url="https://d0.org/vdp", sector="device")
    (tmp_path / "fake_pdftotext.py").write_text(FAKE_PDFTOTEXT)
    (fake_bin / "pdftotext").write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} "
                                        f"{shlex.quote(str(tmp_path / 'fake_pdftotext.py'))} \"$@\"\n")
    (fake_bin / "pdftotext").chmod(0o755)
    return root, fake_bin


def _judge(root, fake_bin, tmp_path, wait_for=None, sig=None):
    """`noroshi judge` as a child in its own process group; `sig` goes to the group once the trace holds `wait_for`
    lines (the starts of the fake `claude` and `pdftotext`), as a terminal sends it."""
    trace = tmp_path / "starts.txt"
    env = {**os.environ, "NOROSHI_ROOT": str(root), "PATH": f"{fake_bin}:{os.environ['PATH']}",
           "FAKE_TRACE": str(trace), "PDF_SLEEP": str(PDF_SLEEP if sig else 0)}
    child = subprocess.Popen([sys.executable, "-c", "import sys; from noroshi import cli; cli.main(sys.argv[1:])",
                              "judge"], cwd=tmp_path, env=env, start_new_session=True, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        if sig is not None:
            deadline = time.monotonic() + 30
            while len(_lines(trace)) < wait_for:
                assert time.monotonic() < deadline and child.poll() is None, child.communicate()
                time.sleep(0.01)
            time.sleep(0.2)  # the helper or the call is still running
            os.killpg(child.pid, sig)
        out, err = child.communicate(timeout=60)
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
    rows = [json.loads(x) for x in _lines(root / "data" / "raw" / "judgements.jsonl")]
    return child.returncode, err, rows, _lines(trace)


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP], ids=["SIGINT", "SIGTERM", "SIGHUP"])
def test_a_signal_during_the_last_pages_call_exits_stopped_and_writes_no_row(tmp_path, sig):
    """K9, K10: the call in flight finishes and is logged; the page is asked again on the next run."""
    root, fake_bin = _one_page(tmp_path, POLICY, "text/html")
    code, err, rows, trace = _judge(root, fake_bin, tmp_path, wait_for=2, sig=sig)  # during the second call
    assert code != 0 and err.strip().splitlines()[-1].startswith("noroshi: judging was stopped")
    assert rows == [] and len(_lines(root / "data" / "raw" / "llm_log.jsonl")) == 2


def test_a_ctrl_c_while_pdftotext_reads_a_page_writes_no_row_and_the_next_run_reads_it(tmp_path):
    """K8, the re-reviewer's probe rrY2/probe_pdf_sigint.py: a readable PDF was recorded pdf_unreadable for good."""
    root, fake_bin = _one_page(tmp_path, b"%PDF-1.4\n" + b"0" * 2000 + b"\n%%EOF\n", "application/pdf")
    code, err, rows, trace = _judge(root, fake_bin, tmp_path, wait_for=1, sig=signal.SIGINT)
    assert trace == ["pdftotext"]  # no model call started after the signal
    assert code != 0 and err.strip().splitlines()[-1].startswith("noroshi: judging was stopped") and rows == []
    code, err, rows, trace = _judge(root, fake_bin, tmp_path)  # the next run, with no signal
    assert code == 0 and len(rows) == 1 and rows[0]["skipped"] is None and rows[0]["final"]["invites_reports"] is True
