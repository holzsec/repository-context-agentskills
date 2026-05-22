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
MARKET_ORDER = ["skills_sh", "skillsdirectory", "gharchive"]
LABEL_MAP = {
    "skills_sh": "skills.sh",
    "skillsdirectory": "skillsdirectory",
    "gharchive": "gharchive",
}


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Create camera-ready stacked bucket bars plus marketplace CDF for repository-context metadata.")
    ap.add_argument("--db", default="../data/rq3_repository_context/repo_context.db")
    ap.add_argument("--table", default="repository_context_scores")
    ap.add_argument("--marketplace-table", default="metadata_repositories")
    ap.add_argument(
        "--cdf-csv",
        default="figures/metadata_score_percentile_lines_by_marketplace.csv",
    )
    ap.add_argument(
        "--out",
        default="figures/metadata_score_categories.pdf",
    )
    ap.add_argument(
        "--dedupe-repo",
        action="store_true",
        help="Use one row per repository for the stacked bars and marketplace CDF.",
    )
    return ap.parse_args()


def load_rows(db: Path, table: str, dedupe_repo: bool = False) -> List[sqlite3.Row]:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA busy_timeout = 5000")
        rows = list(conn.execute(f"SELECT * FROM {table}"))
    finally:
        conn.close()
    if not dedupe_repo:
        return rows
    seen: set[str] = set()
    out: List[sqlite3.Row] = []
    for row in rows:
        repo = str(row["repository"] or "").strip().lower()
        if not repo or repo in seen:
            continue
        seen.add(repo)
        out.append(row)
    return out


def compute_bar_distribution(rows: List[sqlite3.Row]) -> Dict[str, Dict[str, float]]:
    cols = list(BUCKET_LABELS.keys())
    counts = {col: {bucket: 0.0 for bucket in BUCKET_ORDER} for col in cols}
    for row in rows:
        for col in cols:
            bucket = str(row[col] or "").strip().lower()
            if bucket in counts[col]:
                counts[col][bucket] += 1.0
    total = max(len(rows), 1)
    return {col: {bucket: 100.0 * v / total for bucket, v in bucket_map.items()} for col, bucket_map in counts.items()}


def build_marketplace_score_series(
    db: Path,
    table: str,
    marketplace_table: str,
    out_csv: Path,
    dedupe_repo: bool = False,
) -> Dict[str, List[tuple[float, float]]]:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA busy_timeout = 5000")
        if dedupe_repo:
            base_rows = conn.execute(
                f"""
                SELECT *
                FROM (
                    SELECT
                        a.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY lower(a.repository)
                            ORDER BY a.metadata_row_id
                        ) AS rn
                    FROM {table} a
                    WHERE a.repo_metadata_score IS NOT NULL
                )
                WHERE rn = 1
                """
            ).fetchall()
            repo_to_markets: Dict[str, set[str]] = {}
            market_rows = conn.execute(
                f"SELECT repository, skill_marketplaces_csv FROM {marketplace_table} WHERE skill_marketplaces_csv IS NOT NULL"
            ).fetchall()
            for market_row in market_rows:
                repo = str(market_row["repository"] or "").strip().lower()
                if not repo:
                    continue
                bucket = repo_to_markets.setdefault(repo, set())
                for market in [x.strip().lower() for x in str(market_row["skill_marketplaces_csv"]).split(",") if x.strip()]:
                    bucket.add(market)
            rows = [
                (str(row["repository"] or "").strip().lower(), row["repo_metadata_score"], repo_to_markets.get(str(row["repository"] or "").strip().lower(), set()))
                for row in base_rows
            ]
        else:
            rows = conn.execute(
                f"""
                SELECT
                    a.metadata_row_id,
                    a.repo_metadata_score,
                    m.skill_marketplaces_csv
                FROM {table} a
                LEFT JOIN {marketplace_table} m
                  ON a.metadata_row_id = m.id
                WHERE a.repo_metadata_score IS NOT NULL
                """
            ).fetchall()
    finally:
        conn.close()

    scores_by_market: Dict[str, List[float]] = {market: [] for market in MARKET_ORDER}
    for row in rows:
        if dedupe_repo:
            _repo, score, markets = row
            if score is None or not markets:
                continue
        else:
            _metadata_row_id, score, csv_value = row
            if score is None or not csv_value:
                continue
            markets = [x.strip().lower() for x in str(csv_value).split(",") if x.strip()]
        for market in MARKET_ORDER:
            if market in markets:
                scores_by_market[market].append(float(score))

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    series: Dict[str, List[tuple[float, float]]] = {}
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["marketplace", "pct_x", "metadata_score"])
        writer.writeheader()
        for market in MARKET_ORDER:
            scores = sorted(scores_by_market.get(market, []))
            if not scores:
                continue
            n = len(scores)
            xs = [100.0] if n == 1 else [100.0 * i / (n - 1) for i in range(n)]
            pts = list(zip(xs, scores))
            series[market] = pts
            for x, score in pts:
                writer.writerow({"marketplace": market, "pct_x": round(x, 6), "metadata_score": round(score, 6)})
    return series


def plot_combo(bar_dist: Dict[str, Dict[str, float]], cdf_series: Dict[str, List[tuple[float, float]]], out_path: Path) -> None:
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
    for spine in ax1.spines.values():
        spine.set_color("#6B7280")
        spine.set_linewidth(0.75)
    ax1.tick_params(colors="#374151")
    ax1.legend(frameon=False, loc="upper center", bbox_to_anchor=(0.50, -0.30), ncol=3)

    for market in MARKET_ORDER:
        pts = cdf_series.get(market, [])
        if not pts:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        ax2.plot(xs, ys, linewidth=2.5, color=LINE_COLORS[market], solid_capstyle="round", label=LABEL_MAP[market])
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
    fig.savefig(out_path, bbox_inches="tight", pad_inches=0.03)
    plt.close(fig)


def main() -> None:
    args = parse_args()
    db = Path(args.db).resolve()
    out_path = Path(args.out).resolve()
    cdf_csv = Path(args.cdf_csv).resolve()

    rows = load_rows(db, args.table, dedupe_repo=args.dedupe_repo)
    bar_dist = compute_bar_distribution(rows)
    cdf_series = build_marketplace_score_series(
        db,
        args.table,
        args.marketplace_table,
        cdf_csv,
        dedupe_repo=args.dedupe_repo,
    )
    plot_combo(bar_dist, cdf_series, out_path)

    print(out_path)
    print(cdf_csv)


if __name__ == "__main__":
    main()
