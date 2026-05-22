#!/usr/bin/env python3
"""Generate a compact UpSet-style overlap plot for marketplace skill hashes."""

from __future__ import annotations

import argparse
import sqlite3
from collections import OrderedDict
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.ticker import FuncFormatter


DEFAULT_MARKETPLACES: "OrderedDict[str, tuple[str, ...]]" = OrderedDict(
    {
        "SkillsDir.": ("skillsdirectory",),
        "Skills.sh": ("skill.sh",),
        "GitHub": ("gharchive",),
        "ClawHub": ("clawhub",),
    }
)


def format_k(value: float) -> str:
    if value >= 1000:
        scaled = value / 1000
        if scaled >= 10:
            return f"{scaled:.0f}K"
        return f"{scaled:.1f}K"
    return f"{int(value)}"


def fetch_hash_sets(conn: sqlite3.Connection, marketplaces: OrderedDict[str, tuple[str, ...]]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for label, codes in marketplaces.items():
        placeholders = ",".join("?" for _ in codes)
        rows = conn.execute(
            f"""
            SELECT DISTINCT LOWER(skill_hash)
            FROM skills_additional_flat
            WHERE marketplace IN ({placeholders})
              AND skill_hash IS NOT NULL
              AND TRIM(skill_hash) <> ''
            """,
            codes,
        ).fetchall()
        out[label] = {str(r[0]).lower() for r in rows}
    return out


def exact_intersections(hash_sets: dict[str, set[str]]) -> list[tuple[tuple[str, ...], int]]:
    labels = list(hash_sets)
    universe = set().union(*hash_sets.values()) if hash_sets else set()
    rows: list[tuple[tuple[str, ...], int]] = []
    for h in universe:
        membership = tuple(label for label in labels if h in hash_sets[label])
        if membership:
            rows.append((membership, 1))

    counts: dict[tuple[str, ...], int] = {}
    for membership, count in rows:
        counts[membership] = counts.get(membership, 0) + count

    return sorted(counts.items(), key=lambda x: (-x[1], len(x[0]), x[0]))


def render_upset(
    hash_sets: dict[str, set[str]],
    intersections: list[tuple[tuple[str, ...], int]],
    out_path: Path,
    *,
    top_n: int,
    fig_width: float,
    fig_height: float,
    dpi: int,
    show_set_sizes: bool,
) -> None:
    labels = list(hash_sets)
    shown = intersections[:top_n]
    x = np.arange(len(shown))
    sizes = [count for _members, count in shown]

    plt.rcParams.update(
        {
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "font.size": 10,
            "axes.linewidth": 0.8,
        }
    )
    fig = plt.figure(figsize=(fig_width, fig_height))
    if show_set_sizes:
        gs = fig.add_gridspec(
            2,
            2,
            width_ratios=[1.15, 4.8],
            height_ratios=[2.25, 1.45],
            wspace=0.08,
            hspace=0.04,
        )
        ax_bar = fig.add_subplot(gs[0, 1])
        ax_matrix = fig.add_subplot(gs[1, 1], sharex=ax_bar)
        ax_sets = fig.add_subplot(gs[1, 0])
    else:
        gs = fig.add_gridspec(
            2,
            1,
            height_ratios=[2.25, 1.45],
            hspace=0.04,
        )
        ax_bar = fig.add_subplot(gs[0, 0])
        ax_matrix = fig.add_subplot(gs[1, 0], sharex=ax_bar)

    bar_color = "#4C78A8"
    ax_bar.bar(x, sizes, color=bar_color, width=0.72)
    max_size = max(sizes) if sizes else 1
    ax_bar.set_ylim(0, max_size * 1.22)
    ax_bar.yaxis.set_major_formatter(FuncFormatter(lambda v, _pos: format_k(float(v))))
    ax_bar.set_ylabel("Intersection", fontsize=10)
    ax_bar.tick_params(axis="x", bottom=False, labelbottom=False)
    ax_bar.tick_params(axis="y", labelsize=9)
    ax_bar.spines["top"].set_visible(False)
    ax_bar.spines["right"].set_visible(False)

    for xi, val in zip(x, sizes):
        ax_bar.text(
            xi,
            val + max_size * 0.01,
            format_k(float(val)),
            ha="center",
            va="bottom",
            fontsize=8,
            rotation=90,
            color="black",
        )

    y = np.arange(len(labels))
    ax_matrix.set_ylim(-0.6, len(labels) - 0.4)
    ax_matrix.set_yticks(y)
    ax_matrix.set_yticklabels([])
    ax_matrix.set_xticks([])
    ax_matrix.tick_params(left=False, bottom=False)
    for spine in ax_matrix.spines.values():
        spine.set_visible(False)

    inactive_color = "#d9d9d9"
    active_color = "#222222"
    line_color = "#333333"
    for xi, (members, _count) in enumerate(shown):
        active_ys = [labels.index(label) for label in members]
        ax_matrix.scatter([xi] * len(labels), y, s=22, color=inactive_color, zorder=1)
        ax_matrix.scatter([xi] * len(active_ys), active_ys, s=30, color=active_color, zorder=3)
        if len(active_ys) > 1:
            ax_matrix.plot([xi, xi], [min(active_ys), max(active_ys)], color=line_color, linewidth=1.2, zorder=2)

    if show_set_sizes:
        set_sizes = [len(hash_sets[label]) for label in labels]
        ax_sets.barh(y, set_sizes, color="#9ECAE1", height=0.55)
        ax_sets.set_yticks(y)
        ax_sets.set_yticklabels(labels, fontsize=10)
        ax_sets.invert_xaxis()
        ax_sets.xaxis.set_major_formatter(FuncFormatter(lambda v, _pos: format_k(float(v))))
        ax_sets.tick_params(axis="x", labelsize=8)
        ax_sets.tick_params(axis="y", length=0)
        ax_sets.set_xlabel("Set size", fontsize=9)
        ax_sets.spines["top"].set_visible(False)
        ax_sets.spines["right"].set_visible(False)
        ax_sets.spines["left"].set_visible(False)
        for yi, val in zip(y, set_sizes):
            ax_sets.text(val, yi, f" {format_k(float(val))}", va="center", ha="right", fontsize=8)
    else:
        ax_matrix.set_yticklabels(labels, fontsize=10)
        ax_matrix.tick_params(axis="y", left=False, labelleft=True, pad=8)

    fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate an UpSet-style skill hash overlap plot.")
    parser.add_argument("--db", type=Path, default=Path("full_skill_marketplace.db"), help="Path to SQLite DB")
    parser.add_argument("--out", type=Path, default=Path("marketplace_overlap_upset.pdf"), help="Output PDF path")
    parser.add_argument("--dpi", type=int, default=300, help="Output DPI")
    parser.add_argument("--top-n", type=int, default=14, help="Number of exact intersections to show")
    parser.add_argument("--fig-width", type=float, default=6.0, help="Figure width in inches")
    parser.add_argument("--fig-height", type=float, default=3.6, help="Figure height in inches")
    parser.add_argument("--show-set-sizes", action="store_true", help="Show left-side set size bars")
    parser.add_argument(
        "--include-skills-sh-additional",
        action="store_true",
        help="Merge skill.sh_additional into the Skills.sh set",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.db.exists():
        raise FileNotFoundError(f"Database not found: {args.db}")

    marketplaces = DEFAULT_MARKETPLACES.copy()
    if args.include_skills_sh_additional:
        marketplaces["Skills.sh"] = ("skill.sh", "skill.sh_additional")

    conn = sqlite3.connect(args.db)
    try:
        hash_sets = fetch_hash_sets(conn, marketplaces)
    finally:
        conn.close()

    intersections = exact_intersections(hash_sets)
    render_upset(
        hash_sets,
        intersections,
        args.out,
        top_n=args.top_n,
        fig_width=args.fig_width,
        fig_height=args.fig_height,
        dpi=args.dpi,
        show_set_sizes=args.show_set_sizes,
    )

    print(f"Wrote UpSet plot: {args.out}")
    print("Set sizes:")
    for label, values in hash_sets.items():
        print(f"- {label}: {len(values):,}")
    print("Top intersections:")
    for members, count in intersections[: args.top_n]:
        print(f"- {' + '.join(members)}: {count:,}")


if __name__ == "__main__":
    main()
