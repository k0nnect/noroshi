"""Company name lists: FDA AI device makers, ICSMA advisory vendors, EHR developers."""
import json
import re
from pathlib import Path

import pandas as pd

FDA_PAGE = "https://www.fda.gov/medical-devices/software-medical-device-samd/artificial-intelligence-enabled-medical-devices"
FDA_CSV_URL = "https://www.fda.gov/media/178541/download?attachment"
LANTERN_URL = "https://lantern.healthit.gov/api/daily/download"
CSAF_REPO = "cisagov/CSAF"
CSAF_RAW = "https://raw.githubusercontent.com/{repo}/{ref}/csaf_files/OT/white/{path}"

_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "llc", "llp",
    "lp", "gmbh", "ag", "sa", "sl", "srl", "plc", "bv", "nv", "pty", "oy", "ab", "kk", "pllc",
}


def normalize_company(name: str) -> str:
    """Lowercase, punctuation-free name with trailing legal suffixes removed."""
    s = (name or "").lower().replace("&", " and ")
    s = re.sub(r"[.'’]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    words = s.split()
    while len(words) > 1 and words[-1] in _SUFFIXES:
        words.pop()
    return " ".join(words)


def _merge(names: list[str], counts: list[int]) -> pd.DataFrame:
    """Group by normalized key; keep the first name seen and sum the counts."""
    df = pd.DataFrame({"name": names, "n": counts})
    df["key"] = df.name.map(normalize_company)
    df = df[df.key != ""]
    out = df.groupby("key", sort=True).agg(name=("name", "first"), n=("n", "sum")).reset_index()
    return out


def load_fda_ai_list(path: Path) -> pd.DataFrame:
    raw = pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")
    names = [n.strip() for n in raw["Company"] if n.strip()]
    out = _merge(names, [1] * len(names)).rename(columns={"n": "n_devices"})
    return out[["key", "name", "n_devices"]]


def _vendors(branches: list) -> list[str]:
    found: list[str] = []
    for b in branches or []:
        if b.get("category") == "vendor" and b.get("name"):
            found.append(b["name"].strip())
        found += _vendors(b.get("branches"))
    return found


def load_icsma_vendors(csaf_dir: Path) -> pd.DataFrame:
    names: list[str] = []
    for f in sorted(csaf_dir.rglob("*.json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
            tid = doc["document"]["tracking"]["id"]
        except (ValueError, KeyError, TypeError):
            continue
        if not str(tid).upper().startswith("ICSMA-"):
            continue
        # one count per advisory per vendor, however many times the vendor appears
        names += sorted(set(_vendors(doc.get("product_tree", {}).get("branches"))))
    out = _merge(names, [1] * len(names)).rename(columns={"n": "n_advisories"})
    return out[["key", "name", "n_advisories"]]


def load_ehr_developers(path: Path) -> pd.DataFrame:
    """Distinct non-empty `api_developer_name` values of the Lantern download."""
    col = pd.read_csv(path, usecols=["api_developer_name"], dtype=str)["api_developer_name"]
    names = sorted({n.strip() for n in col.dropna() if n.strip()})
    out = _merge(names, [1] * len(names))
    out["website_raw"] = ""
    return out[["key", "name", "website_raw"]]


# ---- downloads (data sources only, never measured sites) ----

def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def download_fda(out: Path) -> dict:
    """The CSV download linked from the FDA page; the site needs an ordinary browser user agent."""
    import httpx
    from noroshi.config import UA_BROWSER

    resp = httpx.get(FDA_CSV_URL, headers={"User-Agent": UA_BROWSER}, follow_redirects=True, timeout=60)
    resp.raise_for_status()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(resp.content)
    return {"url": FDA_CSV_URL, "retrieved_at": _now(), "sha256": _sha256(resp.content)}


def download_lantern(out: Path) -> dict:
    import httpx

    resp = httpx.get(LANTERN_URL, follow_redirects=True, timeout=300)
    resp.raise_for_status()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(resp.content)
    return {"url": LANTERN_URL, "retrieved_at": _now(), "sha256": _sha256(resp.content)}


def download_csaf(out_dir: Path, workers: int = 6) -> dict:
    """ICSMA advisories listed in the folder's index.txt, pinned to one repository commit."""
    from concurrent.futures import ThreadPoolExecutor
    import httpx

    commit = httpx.get(f"https://api.github.com/repos/{CSAF_REPO}/commits/develop", timeout=60).json()["sha"]
    url = lambda p: CSAF_RAW.format(repo=CSAF_REPO, ref=commit, path=p)  # noqa: E731
    index = httpx.get(url("index.txt"), timeout=120)
    index.raise_for_status()
    paths = [p.strip() for p in index.text.splitlines() if "icsma" in p.lower()]

    def fetch(p: str) -> None:
        r = httpx.get(url(p), timeout=60)
        r.raise_for_status()
        dest = out_dir / p
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(fetch, paths))
    return {"url": url("index.txt"), "retrieved_at": _now(), "repository_commit": commit,
            "files_downloaded": len(paths)}
