"""Figure 1 of the paper, drawn only from the study's result files.

Panel A: any-channel share by sector (filled dot: per measured domain; hollow dot: every
unmeasured value counted as none), each with its exact 95% confidence interval.
Panel B: the hospital security.txt share under four units of counting.

Nothing is typed in: every value is read from
  <repo>/results/tables/table2_shares.md
  <repo>/results/tables/tableS1_sensitivity.md
  <paper>/analysis/posthoc_concentration.json
  <paper>/manuscript/build/audit_numbers.json   (number of organizations, from the audit)

Run:  uv run --no-project --python 3.12 --with matplotlib python analysis/figure1.py
Writes manuscript/figures/figure1.png (600 dpi), figure1.pdf and figure1_values.json.
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

PAPER = Path(__file__).resolve().parent.parent
REPO = PAPER.parent / "noroshi"
OUT = PAPER / "manuscript" / "figures"

INK = "#1f2a37"      # marks and primary text
MUTED = "#5b6673"    # secondary text
GRID = "#d9dde2"     # recessive grid
SURFACE = "#ffffff"

SECTORS = [  # (key in the tables, label), in the order of the paper's tables
    ("hospital", "Hospitals"),
    ("device", "Device makers"),
    ("icsma", "Advisory vendors"),
    ("ehr", "EHR developers"),
    ("federal", "Federal control"),
]


def read_table(path: Path) -> list[dict[str, str]]:
    rows = [line.strip().strip("|").split("|") for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("|")]
    header = [c.strip() for c in rows[0]]
    return [dict(zip(header, (c.strip() for c in r))) for r in rows[2:]]


def pick(rows: list[dict[str, str]], sector: str, level: str, measure: str) -> dict[str, str]:
    found = [r for r in rows if (r["sector"], r["level"], r["measure"]) == (sector, level, measure)]
    if len(found) != 1:
        raise SystemExit(f"expected one row for {sector}/{level}/{measure}, found {len(found)}")
    return found[0]


def pct(text: str) -> float:
    if not text.endswith("%"):
        raise SystemExit(f"not a percentage: {text!r}")
    return float(text[:-1])


def point(row: dict[str, str]) -> dict:
    return {"k": int(row["k"]), "n": int(row["n"]), "share": pct(row["share"]),
            "lo": pct(row["ci_lo"]), "hi": pct(row["ci_hi"])}


def interval(ax, y: float, p: dict, filled: bool) -> None:
    ax.plot([p["lo"], p["hi"]], [y, y], color=INK, linewidth=1.2, solid_capstyle="butt", zorder=2)
    ax.plot([p["share"]], [y], marker="o", markersize=6.5, markeredgewidth=1.2, markeredgecolor=INK,
            markerfacecolor=INK if filled else SURFACE, linestyle="none", zorder=3)


def style(ax, xmax: float, step: float) -> None:
    ax.set_xlim(0, xmax)
    ax.set_xticks([t * step for t in range(int(round(xmax / step)) + 1)])
    ax.xaxis.set_major_formatter(lambda v, _: f"{v:g}%")
    ax.grid(axis="x", color=GRID, linewidth=0.6, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(axis="x", colors=MUTED, labelsize=7.5, length=0, pad=4)
    ax.tick_params(axis="y", colors=INK, labelsize=8.5, length=0, pad=6)


def main() -> None:
    shares = read_table(REPO / "results" / "tables" / "table2_shares.md")
    sens = read_table(REPO / "results" / "tables" / "tableS1_sensitivity.md")
    post = json.loads((PAPER / "analysis" / "posthoc_concentration.json").read_text(encoding="utf-8"))
    audit = json.loads((PAPER / "manuscript" / "build" / "audit_numbers.json").read_text(encoding="utf-8"))

    panel_a = [(label, point(pick(shares, key, "domain", "broad")), point(pick(sens, key, "domain", "broad")))
               for key, label in SECTORS]

    by_measure = {r["measure"]: r for r in post["rows"]}

    def post_point(prefix: str) -> dict:
        found = [r for name, r in by_measure.items() if name.startswith(prefix)]
        if len(found) != 1:
            raise SystemExit(f"expected one post hoc row starting {prefix!r}, found {len(found)}")
        return point(found[0])

    panel_b = [
        ("Per listed domain", "pre-specified", point(pick(shares, "hospital", "domain", "main"))),
        ("Per final site, measured", "post hoc", post_point("per distinct final site")),
        ("Per listed domain, without\nthe most common file", "post hoc", post_point("per listed domain, without")),
        ("Per final site, unmeasured\ncounted as none", "pre-specified", point(pick(sens, "hospital", "final_site", "main"))),
    ]
    summary = post["summary"]
    organizations = audit["check2_concentration"]["organizations"]

    plt.rcParams.update({"font.family": "sans-serif", "font.sans-serif": ["Helvetica", "Arial", "DejaVu Sans"],
                         "pdf.fonttype": 42, "svg.fonttype": "none"})
    fig, (ax_a, ax_b) = plt.subplots(1, 2, figsize=(7.4, 3.5), gridspec_kw={"width_ratios": [1.0, 1.0], "wspace": 0.85},
                                     facecolor=SURFACE)

    # Panel A: one row per sector; the control is set a little apart.
    ys = [0, 1, 2, 3, 4.5]
    for y, (label, measured, all_counted) in zip(ys, panel_a):
        interval(ax_a, y - 0.16, measured, filled=True)
        interval(ax_a, y + 0.16, all_counted, filled=False)
        right = measured["hi"] <= 70
        ax_a.annotate(f"{measured['share']:.1f}%", (measured["hi"] if right else measured["lo"], y - 0.16),
                      xytext=(5 if right else -5, 0), textcoords="offset points", va="center",
                      ha="left" if right else "right", fontsize=7.5, color=INK)
    ax_a.set_yticks(ys, [label for label, _, _ in panel_a])
    ax_a.set_ylim(5.2, -0.9)
    style(ax_a, 100, 25)
    legend = ax_a.legend(
        handles=[Line2D([], [], marker="o", color=INK, markerfacecolor=INK, markeredgecolor=INK, markersize=5.5,
                        linewidth=1.2, label="Measured domains"),
                 Line2D([], [], marker="o", color=INK, markerfacecolor=SURFACE, markeredgecolor=INK, markersize=5.5,
                        linewidth=1.2, label="All listed domains, unmeasured counted as none")],
        loc="lower left", bbox_to_anchor=(0, 1.0), ncols=1, frameon=False, fontsize=7.2, labelcolor=MUTED,
        handlelength=2.0, borderaxespad=0.0, borderpad=0.0, labelspacing=0.25)

    # Panel B: the hospital security.txt share under four units of counting.
    for y, (label, kind, p) in enumerate(panel_b):
        interval(ax_b, y, p, filled=True)
        ax_b.annotate(f"{p['share']:.1f}%  ({p['k']:,}/{p['n']:,})", (p["hi"], y), xytext=(5, 0),
                      textcoords="offset points", va="center", ha="left", fontsize=7.5, color=INK)
    ax_b.set_yticks(range(len(panel_b)), [f"{label}\n({kind})" for label, kind, _ in panel_b])
    ax_b.set_ylim(len(panel_b) - 0.45, -0.9)
    xmax_b = 5 * (int(max(p["hi"] for _, _, p in panel_b) // 5) + 2)
    style(ax_b, xmax_b, 5)
    ax_b.tick_params(axis="y", labelsize=7.6)
    note = (f"{summary['distinct_files']} distinct files from {organizations} organizations; "
            f"one file on {summary['most_common_file_domains']} of {summary['positive_domains']} positive domains")
    ax_a.set_xlabel("Share of domains (95% confidence interval)", fontsize=7.5, color=MUTED, labelpad=6)
    ax_b.set_xlabel("Share of domains or final sites (95% confidence interval)", fontsize=7.5, color=MUTED, labelpad=6)

    # Headers start at the left edge of each panel's row labels, so title, key and plot read as one block.
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()

    def left_edge(ax) -> float:
        x0 = min(label.get_window_extent(renderer).x0 for label in ax.get_yticklabels())
        return ax.transAxes.inverted().transform((x0, 0))[0]

    xa, xb = left_edge(ax_a), left_edge(ax_b)
    legend.set_bbox_to_anchor((xa, 1.03), transform=ax_a.transAxes)
    ax_a.text(xa, 1.22, "A. Any channel, by sector", transform=ax_a.transAxes, fontsize=9, color=INK,
              fontweight="bold", ha="left", va="bottom")
    ax_b.text(xb, 1.22, "B. Hospital security.txt, by unit of counting", transform=ax_b.transAxes, fontsize=9,
              color=INK, fontweight="bold", ha="left", va="bottom")
    ax_b.text(xb, 1.115, note, transform=ax_b.transAxes, fontsize=7.2, color=MUTED, ha="left", va="bottom")

    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / "figure1.png", dpi=600, bbox_inches="tight", facecolor=SURFACE)
    fig.savefig(OUT / "figure1.pdf", bbox_inches="tight", facecolor=SURFACE)
    values = {"panel_a": [{"sector": label, "measured": m, "all_listed": a} for label, m, a in panel_a],
              "panel_b": [{"unit": label.replace("\n", " "), "kind": kind, **p} for label, kind, p in panel_b],
              "note": note}
    (OUT / "figure1_values.json").write_text(json.dumps(values, indent=1), encoding="utf-8")
    print("wrote", OUT / "figure1.png")


if __name__ == "__main__":
    main()
