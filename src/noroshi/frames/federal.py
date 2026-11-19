"""Federal health website frame from the CISA .gov domain list."""
from pathlib import Path

import pandas as pd

from noroshi.frames import DOMAIN_COLUMNS, check_domain_table

FEDERAL_CSV_URL = "https://raw.githubusercontent.com/cisagov/dotgov-data/main/current-federal.csv"
DOMAIN_TYPE = "Federal - Executive"
ORGANIZATIONS = ("Department of Health and Human Services", "Department of Veterans Affairs")


def download_federal(out: Path, url: str = FEDERAL_CSV_URL) -> Path:
    import httpx

    resp = httpx.get(url, follow_redirects=True, timeout=60)
    resp.raise_for_status()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(resp.content)
    return out


def build_federal_frame(csv: Path) -> pd.DataFrame:
    """One row per federal health domain. The security contact column is never read."""
    raw = pd.read_csv(
        csv, dtype=str, keep_default_na=False,
        usecols=["Domain name", "Domain type", "Organization name"],
    )
    keep = raw[(raw["Domain type"].str.strip() == DOMAIN_TYPE)
               & raw["Organization name"].str.strip().isin(ORGANIZATIONS)]
    domains = sorted({d.strip().lower().rstrip(".") for d in keep["Domain name"] if d.strip()})
    frame = pd.DataFrame({
        "sector": "federal",
        "domain": domains,
        "start_url": [f"https://{d}/" for d in domains],
        "n_units": 1,
        "beds_sum": pd.array([pd.NA] * len(domains), dtype="Int64"),
        "name_tokens": "",
    })[DOMAIN_COLUMNS]
    check_domain_table(frame)
    return frame
