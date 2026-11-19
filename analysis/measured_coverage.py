"""Supplementary table (post hoc): hospitals on domains that could be measured against hospitals on domains that
could not (unreachable, blocked or mismatch), by type, ownership, beds and rural location.

Added after the first pass, for the reader who asks whether the unmeasured hospitals differ from the measured ones.
Computed from <repo>/data/derived/hospitals.csv alone (one row per hospital, with the status of its listed domain).
The groupings are those of analysis/frame_coverage.py and of protocol section 8.

Run:  uv run --no-project --python 3.12 --with pandas python analysis/measured_coverage.py
Writes analysis/measured_coverage.csv and .md.
"""
from pathlib import Path

import pandas as pd

OUT = Path(__file__).resolve().parent
REPO = OUT.parent.parent / "noroshi"
h = pd.read_csv(REPO / "data" / "derived" / "hospitals.csv", dtype=str, keep_default_na=False)

NOT_MEASURED = {"unreachable", "blocked", "mismatch"}
other = sorted(set(h.status) - NOT_MEASURED - {"measured"})
if other:
    raise SystemExit(f"statuses this table does not handle: {other}")
measured, unmeasured = h[h.status == "measured"], h[h.status.isin(NOT_MEASURED)]

TYPES = {"GENERAL ACUTE CARE": "general acute care", "CRITICAL ACCESS": "critical access", "PSYCHIATRIC": "psychiatric",
         "CHILDREN": "children", "LONG TERM CARE": "long term care", "REHABILITATION": "rehabilitation"}
OWNERS = {"NON-PROFIT": "non-profit", "PROPRIETARY": "proprietary", "GOVERNMENT - LOCAL": "non-federal government",
          "GOVERNMENT - DISTRICT/AUTHORITY": "non-federal government", "GOVERNMENT - STATE": "non-federal government",
          "NOT AVAILABLE": "unknown"}
RURAL = {"True": "nonmetropolitan county", "False": "metropolitan county"}


def beds_group(v: str) -> str:
    try:
        n = int(float(v))
    except ValueError:
        return "unknown"
    if n <= 0:
        return "unknown"
    return "under 100" if n <= 99 else "100-299" if n <= 299 else "300 or more"


def groups(df: pd.DataFrame) -> dict[str, pd.Series]:
    return {"type": df.type.map(lambda v: TYPES.get(v, "other")),
            "ownership": df.owner.map(lambda v: OWNERS.get(v, "other")),
            "beds": df.beds.map(beds_group),
            "rural": df.rural.map(lambda v: RURAL.get(v, "unknown"))}


rows = []
gm, gu = groups(measured), groups(unmeasured)
for characteristic in gm:
    levels = sorted(set(gm[characteristic]) | set(gu[characteristic]),
                    key=lambda v: (-(gm[characteristic] == v).sum() - (gu[characteristic] == v).sum()))
    for level in levels:
        m, u = int((gm[characteristic] == level).sum()), int((gu[characteristic] == level).sum())
        rows.append({"characteristic": characteristic, "level": level, "measured_n": m,
                     "measured_share": f"{100 * m / len(measured):.1f}%", "unmeasured_n": u,
                     "unmeasured_share": f"{100 * u / len(unmeasured):.1f}%",
                     "measured_of_level": f"{100 * m / (m + u):.1f}%" if m + u else ""})
table = pd.DataFrame(rows)
table.to_csv(OUT / "measured_coverage.csv", index=False)
lines = ["| " + " | ".join(table.columns) + " |", "|" + "---|" * len(table.columns)]
lines += ["| " + " | ".join(str(v) for v in r) + " |" for r in table.itertuples(index=False)]
reasons = unmeasured.status.value_counts()
note = (f"Hospitals on measured domains: {len(measured)}. Hospitals on domains that could not be measured: {len(unmeasured)} "
        f"({', '.join(f'{k} {v}' for k, v in reasons.items())}). All hospitals: {len(h)}. Source: data/derived/hospitals.csv.")
(OUT / "measured_coverage.md").write_text("\n".join(lines) + "\n\n" + note + "\n", encoding="utf-8")
print("\n".join(lines))
print(note)
