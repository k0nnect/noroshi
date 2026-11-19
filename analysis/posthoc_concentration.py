"""Post hoc (not in the frozen protocol): how concentrated the hospital security.txt result is.
The hospital main outcome per listed domain (pre-specified), per distinct final site, per distinct file,
and without the domains that serve the most common file. Reads data/derived/domains.csv and, read-only,
the crawl store for the hash of each counted file. Run from the noroshi repository root:
    uv run python ../noroshi-paper/analysis/posthoc_concentration.py"""
import json
import sqlite3
from collections import Counter
from pathlib import Path

import pandas as pd
from scipy.stats import beta

OUT = Path(__file__).resolve().parent


def ci(k: int, n: int) -> tuple[float, float]:
    lo = 0.0 if k == 0 else beta.ppf(0.025, k, n - k + 1)
    hi = 1.0 if k == n else beta.ppf(0.975, k + 1, n - k)
    return lo, hi


def row(label: str, k: int, n: int) -> dict:
    lo, hi = ci(k, n)
    return {"measure": label, "k": k, "n": n, "share": f"{100 * k / n:.1f}%", "ci_lo": f"{100 * lo:.1f}%",
            "ci_hi": f"{100 * hi:.1f}%"}


d = pd.read_csv("data/derived/domains.csv", dtype=str, keep_default_na=False)
h = d[(d.sector == "hospital") & (d.status == "measured") & d.main.isin(["True", "False"])].copy()
pos = h[h.main == "True"]
rows = [row("per listed domain (pre-specified)", len(pos), len(h))]

site = h.final_domain.where(h.final_domain != "", h.domain)
h["site"] = site
by_site = h.groupby("site").main.agg(lambda s: (s == "True").any())
rows.append(row("per distinct final site (post hoc)", int(by_site.sum()), len(by_site)))

con = sqlite3.connect("file:data/raw/crawl.sqlite?mode=ro", uri=True)
file_of: dict[str, str] = {}
for dom, url in zip(pos.domain, pos.sectxt_url):
    got = con.execute("SELECT body_sha256 FROM requests WHERE domain = ? AND purpose IN ('sectxt_wellknown', 'sectxt_root') "
                      "AND status = 200 AND body_sha256 IS NOT NULL AND (url = ? OR final_url = ?) ORDER BY id DESC LIMIT 1",
                      (dom, url, url)).fetchone()
    if got is None:
        got = con.execute("SELECT body_sha256 FROM requests WHERE domain = ? AND purpose IN ('sectxt_wellknown', "
                          "'sectxt_root') AND status = 200 AND body_sha256 IS NOT NULL ORDER BY id DESC LIMIT 1",
                          (dom,)).fetchone()
    file_of[dom] = got[0] if got else "?"
files = Counter(file_of.values())
top_hash, top_n = files.most_common(1)[0]
top_domains = {dom for dom, sha in file_of.items() if sha == top_hash}
rest = h[~h.domain.isin(top_domains)]
rows.append(row("per listed domain, without the domains serving the most common file (post hoc)",
                int((rest.main == "True").sum()), len(rest)))
rest_sites = rest.groupby("site").main.agg(lambda s: (s == "True").any())
table = pd.DataFrame(rows)
summary = {
    "positive_domains": len(pos), "distinct_files": len(files), "most_common_file_domains": top_n,
    "most_common_file_share_of_positive_domains": f"{100 * top_n / len(pos):.1f}%",
    "most_common_file_final_sites": int(h[h.domain.isin(top_domains)].site.nunique()),
    "second_most_common_file_domains": files.most_common(2)[1][1] if len(files) > 1 else 0,
    "files_on_one_domain": sum(1 for n in files.values() if n == 1),
    "positive_final_sites": int(by_site.sum()), "final_sites": len(by_site),
    "domains_csv_md5": __import__("hashlib").md5(Path("data/derived/domains.csv").read_bytes()).hexdigest(),
}
table.to_csv(OUT / "posthoc_concentration.csv", index=False)
(OUT / "posthoc_concentration.json").write_text(json.dumps({"rows": rows, "summary": summary}, indent=1))
print(table.to_string(index=False))
print(json.dumps(summary, indent=1))
