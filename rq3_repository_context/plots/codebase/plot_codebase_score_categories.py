#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


LABELS = {
    "api_domain_match": {"e": "evidence", "s": "some evidence", "n": "no evidence"},
    "api_code_match": {"e": "evidence", "s": "some evidence", "n": "no evidence", "na": "not applicable"},
    "api_readme_match": {"e": "evidence", "s": "some evidence", "n": "no evidence", "na": "not applicable"},
    "api_repo_maliciousness": {"e": "evidence", "s": "some evidence", "n": "no evidence"},
    "api_security_tooling": {"e": "evidence", "s": "some evidence", "n": "no evidence"},
    "api_final_verdict": {
        "ab": "aligned_and_benign",
        "as": "aligned_but_repo_suspicious",
        "na": "not_aligned",
        "i": "inconclusive",
    },
    "api_confidence": {"h": "high", "m": "medium", "l": "low"},
}

ORDER = {
    "api_domain_match": ["e", "s", "n"],
    "api_code_match": ["e", "s", "n", "na"],
    "api_readme_match": ["e", "s", "n", "na"],
    "api_repo_maliciousness": ["n", "s", "e"],
    "api_security_tooling": ["e", "s", "n"],
    "api_final_verdict": ["ab", "as", "na", "i"],
    "api_confidence": ["h", "m", "l"],
}

COLORS = {
    "e": "#1b9e77",
    "s": "#d95f02",
    "n": "#7570b3",
    "na": "#bdbdbd",
    "ab": "#1b9e77",
    "as": "#d95f02",
    "i": "#7570b3",
    "h": "#1f78b4",
    "m": "#6a3d9a",
    "l": "#b15928",
}

PAPER_COLUMNS = [
    ("api_domain_match_num", "Domain match"),
    ("api_code_match_num", "Code match"),
    ("api_readme_match_num", "README match"),
    ("api_repo_maliciousness_num", "Repository maliciousness*"),
    ("api_security_tooling_num", "Security-tooling signal"),
    ("api_confidence_num", "Confidence"),
]

PAPER_LEVELS = [
    (0, "Low", "#72B7B2"),
    (1, "Medium", "#4C78A8"),
    (2, "High", "#F58518"),
]


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Plot category shares and codebase_score distribution for repository-context API scans.")
    ap.add_argument("--db", default="../data/rq3_repository_context/repo_context.db", help="Path to repo_context.db")
    ap.add_argument("--table", default="codebase_scan_results", help="Final codebase scan source table")
    ap.add_argument(
        "--marketplace-table",
        default="metadata_repositories",
        help="Table that provides skill_marketplaces_csv for the marketplace ECDF plot",
    )
    ap.add_argument(
        "--out-dir",
        default="figures",
        help="Output directory for plots",
    )
    return ap.parse_args()


def derive_skill_name(bundle: str) -> str:
    stem = Path(bundle).stem
    parts = stem.split("__")
    if len(parts) < 4:
        return stem
    owner = parts[0]
    repo = parts[1]
    tail = parts[3]
    marker = f"_{owner}_{repo}_"
    idx = tail.find(marker)
    if idx != -1:
        skill_name = tail[idx + len(marker) :]
        if skill_name:
            return skill_name
    return tail


def load_counts(conn: sqlite3.Connection, table: str, column: str) -> dict[str, int]:
    rows = conn.execute(
        f"SELECT {column} AS label, COUNT(*) AS n FROM {table} GROUP BY {column}"
    ).fetchall()
    return {str(label): int(n) for label, n in rows if label is not None}


def plot_category_shares(conn: sqlite3.Connection, table: str, out_dir: Path) -> list[Path]:
    out_paths: list[Path] = []
    columns = list(LABELS.keys())
    fig, ax = plt.subplots(figsize=(12, 7))

    y_labels = [c.replace("api_", "").replace("_", " ") for c in columns]
    y_pos = np.arange(len(columns))
    left = np.zeros(len(columns), dtype=float)

    all_keys = []
    for column in columns:
        for key in ORDER[column]:
            if key not in all_keys:
                all_keys.append(key)

    for key in all_keys:
        widths = []
        for column in columns:
            counts = load_counts(conn, table, column)
            total = sum(counts.values()) or 1
            widths.append(counts.get(key, 0) / total * 100.0)
        if not any(widths):
            continue
        ax.barh(
            y_pos,
            widths,
            left=left,
            color=COLORS.get(key, "#4c78a8"),
            edgecolor="white",
            height=0.7,
            label=key,
        )
        left += np.array(widths)

    ax.set_yticks(y_pos)
    ax.set_yticklabels(y_labels)
    ax.set_xlim(0, 100)
    ax.set_xlabel("Share (%)")
    ax.set_title("Repository Context API Outcome Shares")
    ax.grid(axis="x", alpha=0.2)
    ax.invert_yaxis()

    handles = []
    labels = []
    legend_order = ["e", "s", "n", "na", "ab", "as", "i", "h", "m", "l"]
    legend_names = {
        "e": "evidence",
        "s": "some evidence",
        "n": "no evidence",
        "na": "not applicable / not aligned",
        "ab": "aligned_and_benign",
        "as": "aligned_but_repo_suspicious",
        "i": "inconclusive",
        "h": "high",
        "m": "medium",
        "l": "low",
    }
    for key in legend_order:
        if key in COLORS:
            handles.append(plt.Rectangle((0, 0), 1, 1, color=COLORS[key]))
            labels.append(legend_names[key])
    ax.legend(handles, labels, loc="lower center", bbox_to_anchor=(0.5, -0.28), ncol=3, frameon=False)

    fig.tight_layout()
    out_path = out_dir / "repo_context_skill_api_category_shares_stacked.png"
    fig.savefig(out_path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    out_paths.append(out_path)
    return out_paths


def plot_category_shares_paper(conn: sqlite3.Connection, table: str, out_dir: Path) -> list[Path]:
    fig, ax = plt.subplots(figsize=(8.8, 4.2))
    y_pos = np.arange(len(PAPER_COLUMNS))
    left = np.zeros(len(PAPER_COLUMNS), dtype=float)

    for level_value, level_label, color in PAPER_LEVELS:
        widths = []
        for column, _label in PAPER_COLUMNS:
            total = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} IS NOT NULL"
            ).fetchone()[0] or 1
            count = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} = ?",
                (level_value,),
            ).fetchone()[0]
            widths.append(count / total * 100.0)

        bars = ax.barh(
            y_pos,
            widths,
            left=left,
            color=color,
            edgecolor="white",
            height=0.64,
            label=level_label,
        )
        for bar, width, lft in zip(bars, widths, left):
            if width >= 7:
                ax.text(
                    lft + width / 2,
                    bar.get_y() + bar.get_height() / 2,
                    f"{width:.0f}%",
                    ha="center",
                    va="center",
                    color="white",
                    fontsize=11,
                    fontweight="bold",
                )
        left += np.array(widths)

    ax.set_yticks(y_pos)
    ax.set_yticklabels([label for _column, label in PAPER_COLUMNS], fontsize=12)
    ax.set_xlim(0, 100)
    ax.set_xlabel("Share of codebase scans (%)", fontsize=12)
    ax.tick_params(axis="x", labelsize=11)
    ax.invert_yaxis()
    ax.grid(axis="x", alpha=0.15)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, -0.31), ncol=3, frameon=False, fontsize=11)

    fig.tight_layout()
    out_pdf = out_dir / "codebase_score_categories.pdf"
    fig.savefig(out_pdf, bbox_inches="tight")
    plt.close(fig)
    return [out_pdf]


def plot_codebase_score(conn: sqlite3.Connection, table: str, out_dir: Path) -> list[Path]:
    scores = [
        float(row[0])
        for row in conn.execute(f"SELECT codebase_score FROM {table} WHERE codebase_score IS NOT NULL")
        if row[0] is not None
    ]
    if not scores:
        return []

    data = np.array(scores, dtype=float)
    bins = np.arange(0, 105, 5)
    hist, edges = np.histogram(data, bins=bins, density=False)
    centers = (edges[:-1] + edges[1:]) / 2

    # Smooth trend using a simple moving average over histogram counts.
    kernel = np.array([1, 2, 3, 2, 1], dtype=float)
    kernel /= kernel.sum()
    trend = np.convolve(hist, kernel, mode="same")

    out_paths: list[Path] = []

    fig, ax = plt.subplots(figsize=(11, 6))
    ax.hist(data, bins=bins, color="#4c78a8", alpha=0.75, edgecolor="white")
    ax.plot(centers, trend, color="#d95f02", linewidth=2.5, label="Smoothed trend")
    ax.set_title("Codebase Score Distribution")
    ax.set_xlabel("Codebase score")
    ax.set_ylabel("Count")
    ax.set_xlim(0, 100)
    ax.legend(frameon=False)
    ax.grid(axis="y", alpha=0.2)
    fig.tight_layout()
    out_path = out_dir / "repo_context_skill_api_codebase_score_hist_trend.png"
    fig.savefig(out_path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    out_paths.append(out_path)

    sorted_data = np.sort(data)
    y = np.arange(1, len(sorted_data) + 1) / len(sorted_data)
    fig, ax = plt.subplots(figsize=(11, 6))
    ax.plot(sorted_data, y, color="#1b9e77", linewidth=2.5)
    ax.set_title("Codebase Score ECDF")
    ax.set_xlabel("Codebase score")
    ax.set_ylabel("Cumulative share")
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 1)
    ax.grid(alpha=0.2)
    fig.tight_layout()
    out_path = out_dir / "repo_context_skill_api_codebase_score_ecdf.png"
    fig.savefig(out_path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    out_paths.append(out_path)

    return out_paths


def plot_codebase_score_ecdf_by_marketplace(
    conn: sqlite3.Connection,
    table: str,
    marketplace_table: str,
    out_dir: Path,
) -> list[Path]:
    rows = conn.execute(
        f"""
        SELECT a.codebase_score, r.skill_marketplaces_csv
        FROM {table} a
        JOIN {marketplace_table} r
          ON lower(a.skill_hash) = lower(r.skill_hash)
         AND lower(a.repository) = lower(r.repository)
        WHERE a.codebase_score IS NOT NULL
          AND r.skill_marketplaces_csv IS NOT NULL
        """
    ).fetchall()
    marketplace_scores: dict[str, list[float]] = {}
    for score, csv_value in rows:
        if score is None or not csv_value:
            continue
        for market in [x.strip() for x in str(csv_value).split(",") if x.strip()]:
            marketplace_scores.setdefault(market, []).append(float(score))

    if not marketplace_scores:
        return []

    fig, ax = plt.subplots(figsize=(11, 6))
    palette = ["#1b9e77", "#d95f02", "#7570b3", "#e7298a", "#66a61e", "#e6ab02"]
    for idx, market in enumerate(sorted(marketplace_scores)):
        data = np.sort(np.array(marketplace_scores[market], dtype=float))
        y = np.arange(1, len(data) + 1) / len(data)
        ax.plot(data, y, linewidth=2.2, label=f"{market} (n={len(data)})", color=palette[idx % len(palette)])

    ax.set_title("Codebase Score ECDF by Marketplace")
    ax.set_xlabel("Codebase score")
    ax.set_ylabel("Cumulative share")
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 1)
    ax.grid(alpha=0.2)
    ax.legend(frameon=False, loc="lower right")
    fig.tight_layout()
    out_path = out_dir / "repo_context_skill_api_codebase_score_ecdf_by_marketplace.png"
    fig.savefig(out_path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return [out_path]


def plot_codebase_score_ecdf_by_skill_repo_count(conn: sqlite3.Connection, table: str, out_dir: Path) -> list[Path]:
    rows = conn.execute(
        f"SELECT bundle, codebase_score FROM {table} WHERE codebase_score IS NOT NULL"
    ).fetchall()
    skill_to_scores: dict[str, list[float]] = {}
    skill_to_repos: dict[str, set[str]] = {}
    for bundle, score in rows:
        if bundle is None or score is None:
            continue
        skill_name = derive_skill_name(str(bundle))
        stem = Path(str(bundle)).stem
        parts = stem.split("__", 3)
        repo = f"{parts[0]}/{parts[1]}" if len(parts) >= 2 else ""
        skill_to_scores.setdefault(skill_name, []).append(float(score))
        skill_to_repos.setdefault(skill_name, set()).add(repo)

    grouped_scores: dict[str, list[float]] = {"1 repo": [], "2 repos": [], "3 repos": [], "4+ repos": []}
    for skill_name, scores in skill_to_scores.items():
        repo_count = len(skill_to_repos.get(skill_name, set()))
        if repo_count <= 1:
            grouped_scores["1 repo"].extend(scores)
        elif repo_count == 2:
            grouped_scores["2 repos"].extend(scores)
        elif repo_count == 3:
            grouped_scores["3 repos"].extend(scores)
        else:
            grouped_scores["4+ repos"].extend(scores)

    fig, ax = plt.subplots(figsize=(11, 6))
    colors = {"1 repo": "#1b9e77", "2 repos": "#d95f02", "3 repos": "#7570b3", "4+ repos": "#e7298a"}
    for label in ["1 repo", "2 repos", "3 repos", "4+ repos"]:
        values = grouped_scores[label]
        if not values:
            continue
        data = np.sort(np.array(values, dtype=float))
        y = np.arange(1, len(data) + 1) / len(data)
        ax.plot(data, y, linewidth=2.2, label=f"{label} (n={len(data)})", color=colors[label])

    ax.set_title("Codebase Score ECDF by Skill Repository Support")
    ax.set_xlabel("Codebase score")
    ax.set_ylabel("Cumulative share")
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 1)
    ax.grid(alpha=0.2)
    ax.legend(frameon=False, loc="lower right")
    fig.tight_layout()
    out_path = out_dir / "repo_context_skill_api_codebase_score_ecdf_by_skill_repo_count.png"
    fig.savefig(out_path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return [out_path]


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(Path(args.db)))
    try:
        out_paths = plot_category_shares_paper(conn, args.table, out_dir)
    finally:
        conn.close()

    for path in out_paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
