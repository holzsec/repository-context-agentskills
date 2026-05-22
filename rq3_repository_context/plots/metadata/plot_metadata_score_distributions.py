#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path
from typing import Dict, List

import matplotlib.pyplot as plt


BUCKET_ORDER = ["zero", "low", "medium", "high", "very high"]
BUCKET_LABELS = {
    "repo_size_bucket": "Repo Size",
    "repo_age_bucket": "Repo Age",
    "repo_activity_bucket": "Activity",
    "stars_bucket": "Stars",
    "forks_bucket": "Forks",
    "gh_open_issues_bucket": "Open Issues",
}
PALETTE = ["#BAB0AC", "#72B7B2", "#4C78A8", "#F58518", "#E45756"]
LINE_COLORS = {
    "skills_sh": "#1D4ED8",
    "skillsdirectory": "#059669",
    "gharchive": "#D97706",
}


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Create a paper-ready side-by-side metadata score figure.")
    ap.add_argument("--db", default="../data/rq3_repository_context/repo_context.db")
    ap.add_argument("--table", default="metadata_repositories")
    ap.add_argument(
        "--cdf-csv",
        default="figures/metadata_score_percentile_lines_by_marketplace.csv",
    )
    ap.add_argument("--out", default="figures/metadata_score_distribution.png")
    return ap.parse_args()


def load_bar_distribution(db: Path, table: str) -> Dict[str, Dict[str, float]]:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        seen = set()
        rows = []
        for row in conn.execute(f"SELECT * FROM {table}"):
            repo = str(row["repository"] or "").strip().lower()
            if not repo or repo in seen:
                continue
            seen.add(repo)
            rows.append(row)
    finally:
        conn.close()
    cols = list(BUCKET_LABELS.keys())
    counts = {col: {bucket: 0.0 for bucket in BUCKET_ORDER} for col in cols}
    for row in rows:
        for col in cols:
            bucket = str(row[col] or "").strip().lower()
            if bucket in counts[col]:
                counts[col][bucket] += 1.0
    total = max(len(rows), 1)
    return {col: {bucket: 100.0 * v / total for bucket, v in bucket_map.items()} for col, bucket_map in counts.items()}


def load_cdf_series(path: Path) -> Dict[str, List[tuple[float, float]]]:
    out: Dict[str, List[tuple[float, float]]] = {}
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            market = str(row["marketplace"]).strip().lower()
            score_key = next(k for k in row.keys() if k.startswith("metadata_score_"))
            out.setdefault(market, []).append((float(row["pct_x"]), float(row[score_key])))
    for market in out:
        out[market].sort(key=lambda item: item[0])
    return out


def main() -> None:
    args = parse_args()
    db = Path(args.db).resolve()
    cdf_csv = Path(args.cdf_csv).resolve()
    out_path = Path(args.out).resolve()

    bar_dist = load_bar_distribution(db, args.table)
    cdf_series = load_cdf_series(cdf_csv)

    plt.rcParams.update(
        {
            "font.size": 13.0,
            "axes.labelsize": 15.0,
            "xtick.labelsize": 13.0,
            "ytick.labelsize": 13.0,
            "legend.fontsize": 12.8,
        }
    )

    fig, (ax1, ax2) = plt.subplots(
        1,
        2,
        figsize=(8.1, 3.55),
        gridspec_kw={"width_ratios": [1.28, 1.22]},
    )

    metrics = list(BUCKET_LABELS.keys())
    x = list(range(len(metrics)))
    bottoms = [0.0 for _ in metrics]
    for idx, bucket in enumerate(BUCKET_ORDER):
        vals = [float(bar_dist[col].get(bucket, 0.0)) for col in metrics]
        ax1.bar(
            x,
            vals,
            width=0.54,
            bottom=bottoms,
            color=PALETTE[idx],
            label=bucket,
            edgecolor="white",
            linewidth=0.75,
        )
        for j, xi in enumerate(x):
            if vals[j] >= 8.0:
                ax1.text(xi, bottoms[j] + vals[j] / 2.0, f"{vals[j]:.0f}%", ha="center", va="center", fontsize=8.0)
        bottoms = [bottoms[j] + vals[j] for j in range(len(metrics))]
    ax1.set_xticks(x)
    ax1.set_xticklabels([BUCKET_LABELS[m] for m in metrics], rotation=28, ha="right")
    ax1.set_ylabel("% repositories")
    ax1.set_ylim(0, 100)
    ax1.grid(axis="y", color="#D1D5DB", linewidth=0.7, alpha=0.8)
    for spine in ax1.spines.values():
        spine.set_color("#6B7280")
        spine.set_linewidth(0.75)
    ax1.tick_params(colors="#374151")
    ax1.legend(
        frameon=False,
        loc="upper center",
        bbox_to_anchor=(0.50, -0.30),
        ncol=3,
    )

    label_map = {"skills_sh": "skills.sh", "skillsdirectory": "skillsdirectory", "gharchive": "gharchive"}
    for market in ["skills_sh", "skillsdirectory", "gharchive"]:
        pts = cdf_series.get(market, [])
        if not pts:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        ax2.plot(xs, ys, linewidth=2.5, color=LINE_COLORS[market], solid_capstyle="round", label=label_map[market])
    ax2.set_xlim(0, 100)
    ax2.set_ylim(15, 101)
    ax2.set_xlabel("Sorted repositories (%)")
    ax2.set_ylabel("Metadata score")
    ax2.set_xticks([0, 20, 40, 60, 80, 100])
    ax2.set_yticks([20, 40, 60, 80, 100])
    ax2.grid(axis="y", color="#D1D5DB", linewidth=0.7, alpha=0.8)
    ax2.grid(axis="x", color="#E5E7EB", linewidth=0.45, alpha=0.35)
    for spine in ax2.spines.values():
        spine.set_color("#6B7280")
        spine.set_linewidth(0.75)
    ax2.tick_params(colors="#374151")
    ax2.legend(frameon=False, loc="upper left")

    fig.tight_layout(rect=[0, 0.08, 1, 1], pad=0.30, w_pad=0.7)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=240, bbox_inches="tight", pad_inches=0.03)
    fig.savefig(out_path.with_suffix(".pdf"), bbox_inches="tight", pad_inches=0.03)
    plt.close(fig)

    # Standalone bar panel.
    fig_bar, ax_bar = plt.subplots(figsize=(5.15, 4.05))
    bottoms = [0.0 for _ in metrics]
    for idx, bucket in enumerate(BUCKET_ORDER):
        vals = [float(bar_dist[col].get(bucket, 0.0)) for col in metrics]
        ax_bar.bar(
            x,
            vals,
            width=0.54,
            bottom=bottoms,
            color=PALETTE[idx],
            label=bucket,
            edgecolor="white",
            linewidth=0.75,
        )
        for j, xi in enumerate(x):
            if vals[j] >= 8.0:
                ax_bar.text(xi, bottoms[j] + vals[j] / 2.0, f"{vals[j]:.0f}%", ha="center", va="center", fontsize=8.0)
        bottoms = [bottoms[j] + vals[j] for j in range(len(metrics))]
    ax_bar.set_xticks(x)
    ax_bar.set_xticklabels([BUCKET_LABELS[m] for m in metrics], rotation=28, ha="right")
    ax_bar.set_ylabel("% repositories")
    ax_bar.set_ylim(0, 100)
    ax_bar.grid(axis="y", color="#D1D5DB", linewidth=0.7, alpha=0.8)
    for spine in ax_bar.spines.values():
        spine.set_color("#6B7280")
        spine.set_linewidth(0.75)
    ax_bar.tick_params(colors="#374151")
    ax_bar.legend(
        frameon=False,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.30),
        ncol=3,
        fontsize=8.8,
    )
    fig_bar.tight_layout(rect=[0.04, 0.12, 1, 1], pad=0.28)
    bar_out = out_path.with_name(out_path.stem + "_bar_only" + out_path.suffix)
    fig_bar.savefig(bar_out, dpi=240, bbox_inches="tight", pad_inches=0.03)
    fig_bar.savefig(bar_out.with_suffix(".pdf"), bbox_inches="tight", pad_inches=0.03)
    plt.close(fig_bar)

    # Standalone CDF panel.
    fig_cdf, ax_cdf = plt.subplots(figsize=(3.95, 3.55))
    for market in ["skills_sh", "skillsdirectory", "gharchive"]:
        pts = cdf_series.get(market, [])
        if not pts:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        ax_cdf.plot(xs, ys, linewidth=2.8, color=LINE_COLORS[market], solid_capstyle="round", label=label_map[market])
    ax_cdf.set_xlim(0, 100)
    ax_cdf.set_ylim(15, 101)
    ax_cdf.set_xlabel("Sorted repositories (%)")
    ax_cdf.set_ylabel("Metadata score")
    ax_cdf.set_xticks([0, 20, 40, 60, 80, 100])
    ax_cdf.set_yticks([20, 40, 60, 80, 100])
    ax_cdf.grid(axis="y", color="#D1D5DB", linewidth=0.7, alpha=0.8)
    ax_cdf.grid(axis="x", color="#E5E7EB", linewidth=0.45, alpha=0.35)
    for spine in ax_cdf.spines.values():
        spine.set_color("#6B7280")
        spine.set_linewidth(0.75)
    ax_cdf.tick_params(colors="#374151")
    ax_cdf.legend(frameon=False, loc="upper left", fontsize=13.2)
    fig_cdf.tight_layout(pad=0.30)
    cdf_out = out_path.with_name(out_path.stem + "_cdf_only" + out_path.suffix)
    fig_cdf.savefig(cdf_out, dpi=240, bbox_inches="tight", pad_inches=0.03)
    fig_cdf.savefig(cdf_out.with_suffix(".pdf"), bbox_inches="tight", pad_inches=0.03)
    plt.close(fig_cdf)

    # Standalone bar panel without grid.
    fig_bar_ng, ax_bar_ng = plt.subplots(figsize=(5.15, 4.05))
    bottoms = [0.0 for _ in metrics]
    for idx, bucket in enumerate(BUCKET_ORDER):
        vals = [float(bar_dist[col].get(bucket, 0.0)) for col in metrics]
        ax_bar_ng.bar(
            x,
            vals,
            width=0.54,
            bottom=bottoms,
            color=PALETTE[idx],
            label=bucket,
            edgecolor="white",
            linewidth=0.75,
        )
        for j, xi in enumerate(x):
            if vals[j] >= 8.0:
                ax_bar_ng.text(xi, bottoms[j] + vals[j] / 2.0, f"{vals[j]:.0f}%", ha="center", va="center", fontsize=8.0)
        bottoms = [bottoms[j] + vals[j] for j in range(len(metrics))]
    ax_bar_ng.set_xticks(x)
    ax_bar_ng.set_xticklabels([BUCKET_LABELS[m] for m in metrics], rotation=28, ha="right")
    ax_bar_ng.set_ylabel("% repositories")
    ax_bar_ng.set_ylim(0, 100)
    for spine in ax_bar_ng.spines.values():
        spine.set_color("#6B7280")
        spine.set_linewidth(0.75)
    ax_bar_ng.tick_params(colors="#374151")
    ax_bar_ng.legend(
        frameon=False,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.48),
        ncol=3,
        fontsize=8.8,
    )
    fig_bar_ng.tight_layout(rect=[0.04, 0.22, 1, 1], pad=0.28)
    bar_ng_out = out_path.with_name(out_path.stem + "_bar_only_nogrid" + out_path.suffix)
    fig_bar_ng.savefig(bar_ng_out, dpi=240, bbox_inches="tight", pad_inches=0.03)
    fig_bar_ng.savefig(bar_ng_out.with_suffix(".pdf"), bbox_inches="tight", pad_inches=0.03)
    plt.close(fig_bar_ng)

    # Standalone CDF panel without grid.
    fig_cdf_ng, ax_cdf_ng = plt.subplots(figsize=(3.95, 3.55))
    for market in ["skills_sh", "skillsdirectory", "gharchive"]:
        pts = cdf_series.get(market, [])
        if not pts:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        ax_cdf_ng.plot(xs, ys, linewidth=2.8, color=LINE_COLORS[market], solid_capstyle="round", label=label_map[market])
    ax_cdf_ng.set_xlim(0, 100)
    ax_cdf_ng.set_ylim(15, 101)
    ax_cdf_ng.set_xlabel("Sorted repositories (%)")
    ax_cdf_ng.set_ylabel("Metadata score")
    ax_cdf_ng.set_xticks([0, 20, 40, 60, 80, 100])
    ax_cdf_ng.set_yticks([20, 40, 60, 80, 100])
    for spine in ax_cdf_ng.spines.values():
        spine.set_color("#6B7280")
        spine.set_linewidth(0.75)
    ax_cdf_ng.tick_params(colors="#374151")
    ax_cdf_ng.legend(frameon=False, loc="upper left", fontsize=13.2)
    fig_cdf_ng.tight_layout(pad=0.30)
    cdf_ng_out = out_path.with_name(out_path.stem + "_cdf_only_nogrid" + out_path.suffix)
    fig_cdf_ng.savefig(cdf_ng_out, dpi=240, bbox_inches="tight", pad_inches=0.03)
    fig_cdf_ng.savefig(cdf_ng_out.with_suffix(".pdf"), bbox_inches="tight", pad_inches=0.03)
    plt.close(fig_cdf_ng)
    print(out_path.with_suffix(".pdf"))
    print(out_path)
    print(bar_out.with_suffix(".pdf"))
    print(bar_out)
    print(cdf_out.with_suffix(".pdf"))
    print(cdf_out)
    print(bar_ng_out.with_suffix(".pdf"))
    print(bar_ng_out)
    print(cdf_ng_out.with_suffix(".pdf"))
    print(cdf_ng_out)


if __name__ == "__main__":
    main()
