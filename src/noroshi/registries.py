"""Three public registries of disclosure programs, downloaded once and pinned by commit and hash."""
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from noroshi import config
from noroshi.domains import known_registrable_domain, registrable_domain

MAX_BYTES = 20_000_000
PLATFORMS = ("hackerone", "bugcrowd", "intigriti", "yeswehack")
# file name -> (GitHub repository, branch, path inside the repository)
SOURCES = {
    "program-list.json": ("disclose/diodb", "master", "program-list.json"),
    "CNAsList.json": ("CVEProject/cve-website", "dev", "src/assets/data/CNAsList.json"),
    **{f"{p}_data.json": ("arkadiyt/bounty-targets-data", "main", f"data/{p}_data.json") for p in PLATFORMS},
}
_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z][a-z0-9-]*[a-z0-9]", re.IGNORECASE)


def fetch_snapshots(out: Path, get=httpx.get, big_dir: Path | None = None, max_bytes: int = MAX_BYTES) -> None:
    """Download each file at the head commit of its branch (recorded in registries.meta.json with the
    SHA-256 and entry count). A file over `max_bytes` goes under `big_dir` (gitignored) instead of `out`."""
    big_dir = config.paths().registries_big if big_dir is None else big_dir
    out.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, (repo, branch, path) in SOURCES.items():
        resp = get(f"https://api.github.com/repos/{repo}/commits/{branch}",
                   headers={"Accept": "application/vnd.github+json"}, timeout=60, follow_redirects=True)
        resp.raise_for_status()
        sha = resp.json()["sha"]
        url = f"https://raw.githubusercontent.com/{repo}/{sha}/{path}"
        body = get(url, timeout=120, follow_redirects=True)
        body.raise_for_status()
        content = body.content
        big = len(content) > max_bytes
        dest = (big_dir if big else out) / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        files[name] = {"repo": repo, "branch": branch, "commit": sha, "url": url,
                       "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
                       "entries": len(json.loads(content)), "stored_in": "big" if big else "reference"}
    meta = {"downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "files": files}
    (out / "registries.meta.json").write_text(json.dumps(meta, indent=2) + "\n")


def _domain(value) -> str | None:
    """Registrable domain of a URL, host, wildcard host or e-mail address; None if it is none of those. Only a
    host-like value counts: one that parses to a host under a recognized public or private suffix. An @
    separates an e-mail address only in a value with no ://."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if "://" in v:
        try:
            v = urlsplit(v).hostname or ""
        except ValueError:
            return None
    else:
        if "@" in v:
            v = v.rsplit("@", 1)[1]
        v = v.split("/")[0].split("?")[0].split("#")[0].split(":")[0]
    v = v.removeprefix("*.").strip(".")
    return known_registrable_domain(v) if _HOST.fullmatch(v) else None


def _walk(node):
    """Every string inside nested lists and dicts."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for x in node:
            yield from _walk(x)
    elif isinstance(node, dict):
        for x in node.values():
            yield from _walk(x)


# in-scope asset fields per platform file: the text fields, the type field, and the types that can
# name a website (app ids such as com.example.app, IP ranges, hardware and the like are skipped)
_ASSET_FIELDS = {"hackerone": ("asset_identifier",), "bugcrowd": ("target", "uri"),
                 "intigriti": ("endpoint",), "yeswehack": ("target",)}
_TYPE_FIELD = {"hackerone": "asset_type", "bugcrowd": "type", "intigriti": "type", "yeswehack": "type"}
_WEB_TYPES = {
    "hackerone": {"URL", "WILDCARD", "API", "OTHER"},
    "bugcrowd": {"website", "api", "other", "network"},
    "intigriti": {"url", "wildcard", "other"},
    "yeswehack": {"web-application", "api", "wildcard", "other", "application"},
}


@dataclass
class Registries:
    platforms: dict[str, set[str]] = field(default_factory=dict)
    diodb: set[str] = field(default_factory=set)
    cna: set[str] = field(default_factory=set)

    def hits(self, domain: str) -> dict:
        d = registrable_domain(domain.lower())
        return {"platforms": sorted(p for p, s in self.platforms.items() if d in s),
                "diodb": d in self.diodb, "cna": d in self.cna}


def _read(name: str, out: Path, big_dir: Path):
    for folder in (out, big_dir):
        if (folder / name).exists():
            return json.loads((folder / name).read_text(encoding="utf-8"))
    raise FileNotFoundError(f"registry file {name} is in neither {out} nor {big_dir}; run `noroshi registries fetch`")


def load(out: Path, big_dir: Path | None = None) -> Registries:
    big_dir = config.paths().registries_big if big_dir is None else big_dir
    reg = Registries()
    for p in PLATFORMS:
        found = set()
        for program in _read(f"{p}_data.json", out, big_dir):
            for asset in program.get("targets", {}).get("in_scope", []):
                kind = asset.get(_TYPE_FIELD[p])
                if kind is not None and kind not in _WEB_TYPES[p]:
                    continue
                for key in _ASSET_FIELDS[p]:
                    # an asset field can hold one host or a prose list of them
                    for token in re.split(r"[\s,;]+", str(asset.get(key) or "")):
                        if d := _domain(token):
                            found.add(d)
        reg.platforms[p] = found
    for prog in _read("program-list.json", out, big_dir):
        for key in ("policy_url", "securitytxt_url", "contact_email"):
            if d := _domain(prog.get(key)):
                reg.diodb.add(d)
    for cna in _read("CNAsList.json", out, big_dir):
        for key in ("contact", "disclosurePolicy", "securityAdvisories"):
            for s in _walk(cna.get(key)):
                if d := _domain(s):
                    reg.cna.add(d)
    return reg
