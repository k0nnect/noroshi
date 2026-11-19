"""Two plain figures: shares by sector with exact intervals, and cumulative first-seen dates."""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

SECTORS = ("hospital", "federal", "device", "icsma", "ehr")
LABELS = {"hospital": "Hospitals", "federal": "Federal", "device": "Device makers", "icsma": "ICSMA vendors",
          "ehr": "EHR developers"}


def _save(fig, out: Path, name: str) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    paths = [out / f"{name}.png", out / f"{name}.pdf"]
    fig.savefig(paths[0], dpi=300, bbox_inches="tight")
    fig.savefig(paths[1], bbox_inches="tight")
    plt.close(fig)
    return paths


def shares_by_sector(numbers: dict, out: Path) -> list[Path]:
    """Figure 1: main and broad outcome shares with exact 95% intervals, one row per sector."""
    fig, ax = plt.subplots(figsize=(6.5, 3.6))
    style = {"main": ("Main outcome", "o", "black", -0.12), "broad": ("Broad outcome", "s", "0.55", 0.12)}
    for m, (label, marker, color, dy) in style.items():
        ys, xs, lo, hi = [], [], [], []
        for i, sector in enumerate(SECTORS):
            share = numbers.get(f"{sector}.domain.{m}.share")
            if share is None:
                continue
            ys.append(i + dy)
            xs.append(100 * share)
            lo.append(100 * (share - numbers[f"{sector}.domain.{m}.ci_lo"]))
            hi.append(100 * (numbers[f"{sector}.domain.{m}.ci_hi"] - share))
        ax.errorbar(xs, ys, xerr=[lo, hi], fmt=marker, color=color, markerfacecolor=color if m == "main" else "white",
                    capsize=3, label=label)
    ax.set_yticks(range(len(SECTORS)), [LABELS[s] for s in SECTORS])
    ax.invert_yaxis()
    ax.set_xlim(0, 100)
    ax.set_xlabel("Share of measured domains (%), with exact 95% interval")
    ax.legend(loc="lower right", frameon=False)
    return _save(fig, out, "figure1_shares")


def first_seen_curves(domains: pd.DataFrame, out: Path) -> list[Path]:
    """Figure 2: cumulative count of first-seen dates by sector. Skipped when no domain has a date."""
    dates = domains[domains["first_seen"].fillna("").astype(str) != ""].copy()
    if dates.empty:
        print("noroshi: figure 2 skipped: no first-seen dates yet (run `noroshi wayback --first-seen`)")
        return []
    dates["when"] = pd.to_datetime(dates["first_seen"])
    fig, ax = plt.subplots(figsize=(6.5, 3.6))
    lines = ["-", "--", "-.", ":", (0, (5, 1))]
    shades = ["black", "0.25", "0.45", "0.6", "0.35"]
    for sector, ls, color in zip(SECTORS, lines, shades):
        s = dates[dates.sector == sector].sort_values("when")
        if s.empty:
            continue
        ax.step(s["when"], range(1, len(s) + 1), where="post", linestyle=ls, color=color, label=LABELS[sector])
    ax.set_xlabel("Date the file was first captured")
    ax.set_ylabel("Cumulative domains")
    ax.legend(frameon=False)
    return _save(fig, out, "figure2_first_seen")
