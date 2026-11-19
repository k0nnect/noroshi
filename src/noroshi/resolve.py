"""Ask Claude models for each company's website domain, with resumable, logged passes."""
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from noroshi.domains import normalize_website, registrable_domain
from noroshi.frames import DOMAIN_COLUMNS, check_domain_table, name_tokens
from noroshi.llm import ask_claude, extract_json

SYSTEM = "You answer only with JSON."
BATCH = 25
SEARCH_MODEL = "claude-sonnet-5-5"

MEMORY_PROMPT = """For each organization below, give the registrable domain of its official corporate website, from your own knowledge. If you are not sure, answer null. Do not guess.
Context: {context}
Organizations:
{numbered}
Reply with exactly a JSON array: [{{"n": 1, "domain": "example.com"}}, {{"n": 2, "domain": null}}]"""

SEARCH_PROMPT = """Find the official corporate website of this organization: "{name}". Context: {context}.
Reply with exactly: {{"domain": "example.com", "evidence_url": "https://..."}} or {{"domain": null, "evidence_url": null}}"""


def _clean(value) -> str | None:
    """Registrable domain of whatever the model answered, or None."""
    if not isinstance(value, str):
        return None
    host = normalize_website(value)
    return registrable_domain(host) if host else None


def _read_lines(path: Path | None) -> list[dict]:
    if path is None or not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _append(path: Path, lock: threading.Lock, records: list[dict]) -> None:
    with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")


_PARSE_FAILED = object()


def _ask_parsed(ask, prompt, model, purpose, tools, check):
    """Call, parse; try twice. Returns the parsed value, `_PARSE_FAILED` if the replies
    could not be parsed, or None if the calls themselves failed (error or timeout)."""
    result = None
    for _ in range(2):
        try:
            reply = ask(prompt, model, SYSTEM, purpose, tools=tools).text
        except (RuntimeError, subprocess.TimeoutExpired):
            result = None
            continue
        try:
            return check(extract_json(reply))
        except (ValueError, TypeError, KeyError):
            result = _PARSE_FAILED
    return result


def _done_keys(path: Path) -> set[str]:
    """Names with a good answer; a parse_failed line does not count, so a restart retries it."""
    return {r["key"] for r in _read_lines(path) if not r.get("parse_failed")}


def _check_array(value):
    if not isinstance(value, list):
        raise ValueError("not an array")
    return {int(item["n"]): item.get("domain") for item in value}


def memory_pass(rows: pd.DataFrame, context: str, *, model: str, out: Path, ask=ask_claude) -> None:
    done = _done_keys(out)
    todo = [(k, n) for k, n in zip(rows["key"], rows["name"]) if k not in done]
    lock = threading.Lock()
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        numbered = "\n".join(f"{j}. {name}" for j, (_, name) in enumerate(batch, 1))
        prompt = MEMORY_PROMPT.format(context=context, numbered=numbered)
        answers = _ask_parsed(ask, prompt, model, "resolve_memory", (), _check_array)
        if answers is None:
            continue  # calls failed: leave the batch unwritten so a restart retries it
        if answers is _PARSE_FAILED:
            answers = None
        records = []
        for j, (key, name) in enumerate(batch, 1):
            rec = {"key": key, "name": name, "model": model,
                   "domain": _clean(answers.get(j)) if answers is not None else None}
            if answers is None:
                rec["parse_failed"] = True
            records.append(rec)
        _append(out, lock, records)


def _check_object(value):
    if not isinstance(value, dict) or "domain" not in value:
        raise ValueError("not an answer object")
    return value


def search_pass(rows: pd.DataFrame, context: str, *, out: Path, model: str = SEARCH_MODEL,
                ask=ask_claude, workers: int = 6) -> None:
    done = _done_keys(out)
    todo = [(k, n) for k, n in zip(rows["key"], rows["name"]) if k not in done]
    lock = threading.Lock()

    def one(item):
        key, name = item
        prompt = SEARCH_PROMPT.format(name=name, context=context)
        ans = _ask_parsed(ask, prompt, model, "resolve_search", ("WebSearch",), _check_object)
        if ans is None:
            return  # calls failed: leave the name unwritten so a restart retries it
        if ans is _PARSE_FAILED:
            ans = None
        rec = {"key": key, "name": name, "model": model,
               "domain": _clean(ans.get("domain")) if ans else None,
               "evidence_url": ans.get("evidence_url") if ans else None}
        if ans is None:
            rec["parse_failed"] = True
        _append(out, lock, [rec])

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        list(ex.map(one, todo))


def _by_key(path: Path | None) -> dict[str, dict]:
    return {r["key"]: r for r in _read_lines(path)} if path else {}


def reconcile(pass_a: Path, pass_b: Path, search: Path | None) -> pd.DataFrame:
    a, b, s = _by_key(pass_a), _by_key(pass_b), _by_key(search)
    out = []
    for key in sorted(set(a) | set(b)):
        name = (a.get(key) or b.get(key))["name"]
        da = _clean((a.get(key) or {}).get("domain"))
        db = _clean((b.get(key) or {}).get("domain"))
        ds = _clean((s.get(key) or {}).get("domain"))
        if da and da == db:
            domain, how = da, "agreed"
        elif ds:
            domain, how = ds, "search"
        else:
            domain, how = "", "unresolved"
        out.append({"key": key, "name": name, "domain": domain, "resolution": how})
    return pd.DataFrame(out, columns=["key", "name", "domain", "resolution"])


def disagreements(pass_a: Path, pass_b: Path) -> pd.DataFrame:
    """Names that still need a search pass: the two memory answers do not agree."""
    r = reconcile(pass_a, pass_b, None)
    return r[r.resolution == "unresolved"][["key", "name"]].reset_index(drop=True)


def to_domain_table(resolved: pd.DataFrame, sector: str) -> pd.DataFrame:
    """One row per resolved domain. Unresolved names are left out; count them from `resolved`."""
    ok = resolved[resolved.domain != ""]
    rows = [
        {"sector": sector, "domain": dom, "start_url": f"https://{dom}/", "n_units": len(g),
         "beds_sum": "", "name_tokens": name_tokens(list(g.name))}
        for dom, g in ok.groupby("domain", sort=True)
    ]
    table = pd.DataFrame(rows, columns=DOMAIN_COLUMNS)
    check_domain_table(table)
    return table
