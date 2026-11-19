"""Supplementary table: hospitals kept in the frame against eligible hospitals dropped at steps 4 to 6 of protocol
section 4.1 (no website listed, a website that cannot be read, a website that belongs to someone else), by type,
ownership and beds. Computed from data/frames/hospitals.csv alone (protocol section 8).
Run from the noroshi repository root:  uv run python ../noroshi-paper/analysis/frame_coverage.py"""
from pathlib import Path

import pandas as pd

OUT = Path(__file__).resolve().parent
h = pd.read_csv("data/frames/hospitals.csv", dtype=str, keep_default_na=False)
DROPPED = {"no_website": "no website listed", "malformed": "website cannot be read",
           "shared_platform": "website belongs to someone else"}
kept = h[h.exclusion == ""]
dropped = h[h.exclusion.isin(DROPPED)]
assert len(kept) == 3693 and len(dropped) == 3838, (len(kept), len(dropped))

TYPES = {"GENERAL ACUTE CARE": "general acute care", "CRITICAL ACCESS": "critical access", "PSYCHIATRIC": "psychiatric",
         "CHILDREN": "children", "LONG TERM CARE": "long term care", "REHABILITATION": "rehabilitation"}
OWNERS = {"NON-PROFIT": "non-profit", "PROPRIETARY": "proprietary", "GOVERNMENT - LOCAL": "non-federal government",
          "GOVERNMENT - DISTRICT/AUTHORITY": "non-federal government", "GOVERNMENT - STATE": "non-federal government",
          "NOT AVAILABLE": "unknown"}


def beds_group(v: str) -> str:
    try:
        n = int(float(v))
    except ValueError:
        return "unknown"
    if n <= 0:
        return "unknown"
    return "under 100" if n <= 99 else "100-299" if n <= 299 else "300 or more"  # the protocol's groups


def groups(df: pd.DataFrame) -> dict[str, pd.Series]:
    return {"type": df.type.map(lambda v: TYPES.get(v, "other")),
            "ownership": df.owner.map(lambda v: OWNERS.get(v, "other")),
            "beds": df.beds.map(beds_group)}  # type, ownership and beds, as the protocol names them


rows = []
gk, gd = groups(kept), groups(dropped)
for characteristic in gk:
    levels = sorted(set(gk[characteristic]) | set(gd[characteristic]),
                    key=lambda v: (-(gk[characteristic] == v).sum() - (gd[characteristic] == v).sum()))
    for level in levels:
        k, d = int((gk[characteristic] == level).sum()), int((gd[characteristic] == level).sum())
        rows.append({"characteristic": characteristic, "level": level, "kept_n": k,
                     "kept_share": f"{100 * k / len(kept):.1f}%", "dropped_n": d,
                     "dropped_share": f"{100 * d / len(dropped):.1f}%",
                     "kept_of_level": f"{100 * k / (k + d):.1f}%" if k + d else ""})
table = pd.DataFrame(rows)
table.to_csv(OUT / "frame_coverage.csv", index=False)
lines = ["| " + " | ".join(table.columns) + " |", "|" + "---|" * len(table.columns)]
lines += ["| " + " | ".join(str(v) for v in r) + " |" for r in table.itertuples(index=False)]
reasons = dropped.exclusion.map(DROPPED).value_counts()
note = (f"Kept: {len(kept)} hospitals. Dropped at steps 4 to 6: {len(dropped)} "
        f"({', '.join(f'{k} {v}' for k, v in reasons.items())}). Source: data/frames/hospitals.csv.")
(OUT / "frame_coverage.md").write_text("\n".join(lines) + "\n\n" + note + "\n")
print("\n".join(lines))
print(note)
