#!/usr/bin/env python3
"""Generate a compact bar chart of skill hashes by source membership count."""

from __future__ import annotations

import argparse
import sqlite3
from collections import Counter, OrderedDict
from pathlib import Path

import matplotlib.pyplot as plt
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


def membership_counts(hash_sets: dict[str, set[str]]) -> Counter[int]:
    universe = set().union(*hash_sets.values()) if hash_sets else set()
    counts: Counter[int] = Counter()
    for h in universe:
        n = sum(1 for values in hash_sets.values() if h in values)
        counts[n] += 1
    return counts


def render_bar_chart(counts: Counter[int], out_path: Path, *, fig_width: float, fig_height: float, dpi: int) -> None:
    plt.rcParams.update(
        {
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "font.size": 10,
            "axes.linewidth": 0.8,
        }
    )

    xs = [1, 2, 3, 4]
    ys = [counts.get(x, 0) for x in xs]

    fig, ax = plt.subplots(figsize=(fig_width, fig_height))
    bars = ax.bar(xs, ys, color=["#9ECAE1", "#6BAED6", "#3182BD", "#08519C"], width=0.62)
    ax.set_xticks(xs)
    ax.set_xticklabels([f"{x} source" if x == 1 else f"{x} sources" for x in xs], fontsize=10)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _pos: format_k(float(v))))
    ax.set_ylabel("Distinct hashes", fontsize=10)
    ax.tick_params(axis="y", labelsize=9)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", color="#e6e6e6", linewidth=0.6)
    ax.set_axisbelow(True)

    ymax = max(ys) if ys else 1
    for bar, val in zip(bars, ys):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            val + ymax * 0.025,
            format_k(float(val)),
            ha="center",
            va="bottom",
            fontsize=10,
        )
    ax.set_ylim(0, ymax * 1.14)
    fig.tight_layout()
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate source-membership bar chart for skill hashes.")
    parser.add_argument("--db", type=Path, default=Path("full_skill_marketplace.db"), help="Path to SQLite DB")
    parser.add_argument("--out", type=Path, default=Path("marketplace_overlap_membership_bars.pdf"), help="Output PDF")
    parser.add_argument("--dpi", type=int, default=300, help="Output DPI")
    parser.add_argument("--fig-width", type=float, default=3.6, help="Figure width in inches")
    parser.add_argument("--fig-height", type=float, default=2.25, help="Figure height in inches")
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

    counts = membership_counts(hash_sets)
    render_bar_chart(counts, args.out, fig_width=args.fig_width, fig_height=args.fig_height, dpi=args.dpi)

    print(f"Wrote membership bar chart: {args.out}")
    for n in sorted(counts):
        label = "source" if n == 1 else "sources"
        print(f"- {n} {label}: {counts[n]:,}")


if __name__ == "__main__":
    main()
