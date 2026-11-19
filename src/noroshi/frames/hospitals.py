"""US hospital frame from the HIFLD hospitals layer (PASDA mirror) and RUCC 2023."""
import hashlib
import io
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pandas as pd

from noroshi.domains import is_shared_platform, normalize_website, registrable_domain
from noroshi.frames import DOMAIN_COLUMNS, check_domain_table, name_tokens

HIFLD_URL = "https://mapservices.pasda.psu.edu/server/rest/services/pasda/HIFLD_FEMA/MapServer/51/query"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
PAGE_SIZE = 2000

STATES_AND_DC = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT "
    "NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
)

HOSPITAL_COLUMNS = ["hifld_id", "name", "state", "county_fips", "type", "owner", "beds",
                    "trauma", "rural", "website_raw", "host", "domain", "exclusion"]


def download_hifld(out: Path, url: str = HIFLD_URL) -> Path:
    """Page through the layer and write all records plus a .meta.json beside it."""
    import httpx

    features: list[dict] = []
    offset = 0
    with httpx.Client(headers={"User-Agent": BROWSER_UA}, timeout=120, follow_redirects=True) as client:
        while True:
            resp = client.get(url, params={
                "where": "1=1", "outFields": "*", "returnGeometry": "false", "f": "json",
                "orderByFields": "OBJECTID", "resultOffset": offset, "resultRecordCount": PAGE_SIZE,
            })
            resp.raise_for_status()
            body = resp.json()
            if "error" in body:
                raise RuntimeError(f"HIFLD service error: {body['error']}")
            page = body.get("features", [])
            features.extend(page)
            if not page:
                break
            offset += len(page)
            if not body.get("exceededTransferLimit") and len(page) < PAGE_SIZE:
                break
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"features": features}), encoding="utf-8")
    meta = {
        "url": url,
        "downloaded_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "record_count": len(features),
        "sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
    }
    out.with_suffix(".meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return out


def load_rucc(rucc: Path) -> dict[str, int]:
    """County FIPS -> RUCC 2023 code. Handles the long format (Attribute/Value) and a wide one."""
    data = rucc.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:  # the USDA file is Windows-1252 (e.g. "Dona Ana" with a tilde n)
        text = data.decode("cp1252")
    df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    if "Attribute" in df.columns:
        df = df[df["Attribute"].str.strip().str.upper().str.startswith("RUCC")]
        pairs = zip(df["FIPS"], df["Value"])
    else:
        col = next(c for c in df.columns if c.upper().startswith("RUCC"))
        pairs = zip(df["FIPS"], df[col])
    out: dict[str, int] = {}
    for fips, val in pairs:
        if fips.strip() and val.strip().isdigit():
            out[fips.strip().zfill(5)] = int(val)
    return out


def _positive_int(value) -> int | None:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _exclusion(row: dict, host: str | None, domain: str | None) -> str | None:
    if (row["status"] or "").strip().upper() == "CLOSED":
        return "closed"
    if (row["state"] or "").strip().upper() not in STATES_AND_DC:
        return "territory"
    if (row["owner"] or "").strip().upper() == "GOVERNMENT - FEDERAL" \
            or (row["type"] or "").strip().upper() == "MILITARY":
        return "federal"
    raw = (row["website_raw"] or "").strip()
    if not raw or raw.upper() == "NOT AVAILABLE":
        return "no_website"
    if host is None:
        return "malformed"
    if is_shared_platform(domain):
        return "shared_platform"
    return None


def build_hospital_frame(raw: Path, rucc: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    features = json.loads(Path(raw).read_text(encoding="utf-8"))["features"]
    rucc_codes = load_rucc(Path(rucc))
    rows = []
    for feat in features:
        a = feat.get("attributes", feat)
        fips = (a.get("COUNTYFIPS") or "").strip()
        fips = fips.zfill(5) if fips.isdigit() else None
        code = rucc_codes.get(fips) if fips else None
        row = {
            "hifld_id": str(a.get("ID")),
            "name": a.get("NAME"),
            "state": a.get("STATE"),
            "status": a.get("STATUS"),
            "county_fips": fips,
            "type": a.get("TYPE"),
            "owner": a.get("OWNER"),
            "beds": _positive_int(a.get("BEDS")),
            "trauma": a.get("TRAUMA"),
            "rural": pd.NA if code is None else code >= 4,
            "website_raw": a.get("WEBSITE"),
        }
        host = normalize_website(row["website_raw"])
        domain = registrable_domain(host) if host else None
        row["exclusion"] = _exclusion(row, host, domain)
        if row["exclusion"] is None:
            row["host"], row["domain"] = host, domain
        else:
            row["host"] = row["domain"] = None
        rows.append(row)

    hospitals = pd.DataFrame(rows)
    hospitals["beds"] = hospitals["beds"].astype("Int64")
    hospitals["rural"] = hospitals["rural"].astype("boolean")
    hospitals = hospitals[HOSPITAL_COLUMNS]

    domain_rows = []
    kept = hospitals[hospitals["exclusion"].isna()]
    for domain, grp in kept.groupby("domain", sort=True):
        counts = Counter(grp["host"])
        best = sorted(counts, key=lambda h: (-counts[h], len(h), h))[0]
        domain_rows.append({
            "sector": "hospital",
            "domain": domain,
            "start_url": f"https://{best}/",
            "n_units": len(grp),
            "beds_sum": pd.NA if grp["beds"].isna().all() else int(grp["beds"].sum()),
            "name_tokens": name_tokens(list(grp["name"])),
        })
    domains = pd.DataFrame(domain_rows, columns=DOMAIN_COLUMNS)
    domains["beds_sum"] = domains["beds_sum"].astype("Int64")
    check_domain_table(domains)
    return hospitals, domains


AUDIT_COLUMNS = ["hifld_id", "name", "state", "website_raw", "domain", "n_units_on_domain"]


def _has_path(website_raw: str | None) -> bool:
    s = (website_raw or "").strip()
    if "://" not in s:
        s = "//" + s
    try:
        return bool(urlsplit(s).path.strip("/"))
    except ValueError:
        return False


def audit_candidates(hospitals: pd.DataFrame) -> pd.DataFrame:
    """Kept rows whose website has a path and whose domain contains none of the hospital's own name tokens."""
    kept = hospitals[hospitals["exclusion"].isna()]
    sizes = kept.groupby("domain")["hifld_id"].transform("size")
    keep = []
    for (_, row), n in zip(kept.iterrows(), sizes):
        tokens = name_tokens([row["name"]]).split()
        in_domain = any(t in row["domain"] for t in tokens)
        keep.append(_has_path(row["website_raw"]) and not in_domain)
    out = kept[keep].copy()
    out["n_units_on_domain"] = sizes[keep].astype(int)
    return out[AUDIT_COLUMNS].reset_index(drop=True)
