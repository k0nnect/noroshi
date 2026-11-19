"""First-seen dates (protocol section 6.8): a summary of the earliest Internet Archive capture of each counted
security.txt file, for the domains with the main outcome. The pipeline stores the date per domain
(data/derived/domains.csv, column first_seen) and draws the cumulative figure (results/figures/figure2_first_seen);
this script only counts, so that the Supplementary Methods can state the result in words.

Run from the noroshi repository root:  uv run python ../noroshi-paper/analysis/first_seen_summary.py
Writes analysis/first_seen_summary.json and .md, and copies the pipeline's figure to manuscript/figures/figureS1_first_seen.png.
"""
import csv
import json
import shutil
from collections import Counter
from pathlib import Path

OUT = Path(__file__).resolve().parent
rows = [r for r in csv.DictReader(open("data/derived/domains.csv", encoding="utf-8"))
        if r["status"] == "measured" and r["main"] == "True"]
looked = {json.loads(line)["domain"] for line in open("data/raw/wayback.jsonl", encoding="utf-8")}
SECTORS = ["hospital", "device", "icsma", "ehr", "federal"]


def summary(group: list[dict]) -> dict:
    years = Counter(r["first_seen"][:4] for r in group if r["first_seen"])
    none = sum(1 for r in group if not r["first_seen"] and r["domain"] in looked)
    incomplete = sum(1 for r in group if not r["first_seen"] and r["domain"] not in looked)
    dated = sorted(r["first_seen"] for r in group if r["first_seen"])
    return {"domains": len(group), "with_capture": len(dated), "no_capture_found": none, "lookup_incomplete": incomplete,
            "by_year": dict(sorted(years.items())), "earliest": dated[0] if dated else None, "latest": dated[-1] if dated else None}


distinct = list({r["domain"]: r for r in rows}.values())
result = {s: summary([r for r in rows if r["sector"] == s]) for s in SECTORS}
result["all_distinct"] = summary(distinct)
for name, s in result.items():
    assert s["domains"] == s["with_capture"] + s["no_capture_found"] + s["lookup_incomplete"], name
(OUT / "first_seen_summary.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
years = sorted({y for s in result.values() for y in s["by_year"]})
head = ["list", "domains with the main outcome", "with an archived capture"] + years + ["no capture found", "lookup incomplete"]
lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
for name, s in result.items():
    lines.append("| " + " | ".join([name, str(s["domains"]), str(s["with_capture"])] + [str(s["by_year"].get(y, 0)) for y in years]
                                   + [str(s["no_capture_found"]), str(s["lookup_incomplete"])]) + " |")
(OUT / "first_seen_summary.md").write_text("\n".join(lines) + "\n\nYear columns: year of the earliest capture. Source: data/derived/domains.csv "
                                             "(first_seen) and data/raw/wayback.jsonl (which lookups completed).\n", encoding="utf-8")
figures = OUT.parent / "manuscript" / "figures"
shutil.copyfile("results/figures/figure2_first_seen.png", figures / "figureS1_first_seen.png")
print("\n".join(lines))
