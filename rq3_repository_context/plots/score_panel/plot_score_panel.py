#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import matplotlib.pyplot as plt
import seaborn as sns


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Camera-ready histograms for codebase, metadata, and final scores.")
    ap.add_argument("--db", default="../data/rq3_repository_context/repo_context.db", help="Path to repo_context.db")
    ap.add_argument("--table", default="repository_context_scores", help="Final merged source table")
    ap.add_argument("--out-dir", default="figures", help="Output directory")
    return ap.parse_args()


def load_scores(conn: sqlite3.Connection, table: str, column: str) -> list[float]:
    return [float(r[0]) for r in conn.execute(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL")]


def plot_hist(scores: list[float], xlabel: str, color: str, out_base: Path) -> None:
    sns.set_theme(style="whitegrid")
    fig, ax = plt.subplots(figsize=(5.6, 3.9))
    sns.histplot(
        scores,
        bins=24,
        kde=True,
        color=color,
        alpha=0.58,
        edgecolor=None,
        line_kws={"linewidth": 2.2},
        ax=ax,
    )
    ax.set_xlabel(xlabel, fontsize=12)
    ax.set_ylabel("Count", fontsize=12)
    ax.tick_params(labelsize=11)
    for spine in ax.spines.values():
        spine.set_color("#BDBDBD")
        spine.set_linewidth(0.8)
    fig.tight_layout(pad=0.35)
    out_base.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_base.with_suffix(".pdf"), bbox_inches="tight", pad_inches=0.03)
    plt.close(fig)


def plot_three_panel(
    codebase_scores: list[float],
    metadata_scores: list[float],
    final_scores: list[float],
    out_base: Path,
) -> None:
    sns.set_theme(style="whitegrid")
    fig, axes = plt.subplots(1, 3, figsize=(11.8, 3.8), sharey=False)
    panels = [
        (codebase_scores, "Codebase score", "#4C78A8"),
        (metadata_scores, "Metadata score", "#72B7B2"),
        (final_scores, "Repository-Context Score", "#F58518"),
    ]
    for ax, (scores, xlabel, color) in zip(axes, panels):
        sns.histplot(
            scores,
            bins=24,
            kde=True,
            color=color,
            alpha=0.58,
            edgecolor=None,
            line_kws={"linewidth": 2.4},
            ax=ax,
        )
        ax.set_xlabel(xlabel, fontsize=15)
        ax.set_ylabel("Count", fontsize=15)
        ax.tick_params(labelsize=14)
        for spine in ax.spines.values():
            spine.set_color("#BDBDBD")
            spine.set_linewidth(0.8)
    fig.tight_layout(pad=0.3, w_pad=0.8)
    out_base.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_base.with_suffix(".png"), dpi=220, bbox_inches="tight", pad_inches=0.03)
    fig.savefig(out_base.with_suffix(".pdf"), bbox_inches="tight", pad_inches=0.03)
    plt.close(fig)


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir)
    conn = sqlite3.connect(str(Path(args.db)))
    try:
        codebase_scores = load_scores(conn, args.table, "codebase_score")
        metadata_scores = load_scores(conn, args.table, "repo_metadata_score")
        final_scores = load_scores(conn, args.table, "final_score")
    finally:
        conn.close()

    plot_three_panel(
        codebase_scores,
        metadata_scores,
        final_scores,
        out_dir / "score_panel",
    )
    print(out_dir / "score_panel.pdf")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
