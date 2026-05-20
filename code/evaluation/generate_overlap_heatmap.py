#!/usr/bin/env python3
"""Generate a marketplace overlap heatmap from skills_additional_flat hashes.

Default marketplaces shown:
- ClawHub.ai        -> clawhub
- GHArchive.org     -> gharchive
- Skills.sh         -> skill.sh
- SkillsDirectory.com -> skillsdirectory

Diagonal cells are uncolored (NaN) by default and annotated with each marketplace total.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


DEFAULT_MARKETPLACES = [
    ("ClawHub", ("clawhub",)),
    ("GHArchive", ("gharchive",)),
    ("Skills.sh", ("skill.sh",)),
    ("SkillsDir", ("skillsdirectory",)),
]


def format_k(value: float) -> str:
    if np.isnan(value):
        return ""
    if value >= 1000:
        return f"{value / 1000:.1f}K"
    return f"{int(value)}"


def fetch_hash_sets(conn: sqlite3.Connection, marketplaces: list[tuple[str, tuple[str, ...]]]) -> dict[str, set[str]]:
    """Load distinct skill hashes for each marketplace bucket."""

    cur = conn.cursor()
    results: dict[str, set[str]] = {}

    for label, codes in marketplaces:
        placeholders = ",".join("?" for _ in codes)
        query = f"""
            SELECT DISTINCT LOWER(skill_hash)
            FROM skills_additional_flat
            WHERE marketplace IN ({placeholders})
              AND skill_hash IS NOT NULL
              AND TRIM(skill_hash) <> ''
        """
        rows = cur.execute(query, codes).fetchall()
        results[label] = {row[0] for row in rows}

    return results


def build_overlap_matrix(
    hash_sets: dict[str, set[str]], once: bool = False, color_diagonal: bool = False
) -> tuple[list[str], np.ndarray]:
    """Build the pairwise intersection matrix used by the heatmap renderer."""

    labels = list(hash_sets.keys())
    n = len(labels)
    matrix = np.full((n, n), np.nan, dtype=float)

    for i, li in enumerate(labels):
        for j, lj in enumerate(labels):
            if i == j:
                if color_diagonal:
                    matrix[i, j] = float(len(hash_sets[li]))
                continue
            if once and j > i:
                continue
            matrix[i, j] = float(len(hash_sets[li].intersection(hash_sets[lj])))

    return labels, matrix


def render_heatmap(
    labels: list[str], matrix: np.ndarray, diagonal_totals: list[int], out_path: Path, dpi: int
) -> None:
    """Render and save the overlap matrix as a PDF or image file."""

    fig, ax = plt.subplots(figsize=(9.8, 7.2))

    cmap = plt.cm.Blues.copy()
    cmap.set_bad(color="white")

    finite_vals = matrix[~np.isnan(matrix)]
    vmax = float(np.max(finite_vals)) if finite_vals.size else 1.0
    im = ax.imshow(matrix, cmap=cmap, aspect="equal", vmin=0, vmax=vmax)

    ax.set_xticks(np.arange(len(labels)))
    ax.set_yticks(np.arange(len(labels)))
    ax.set_xticklabels(labels, rotation=20, ha="right", fontsize=18)
    ax.set_yticklabels(labels, fontsize=18)
    ax.tick_params(axis="y", pad=12)
    for tick in ax.get_yticklabels():
        tick.set_horizontalalignment("right")

    # Annotate overlap values for off-diagonal cells.
    for i in range(matrix.shape[0]):
        for j in range(matrix.shape[1]):
            if i == j:
                continue
            if np.isnan(matrix[i, j]):
                continue
            value = matrix[i, j]
            text_color = "white" if value > vmax * 0.45 else "black"
            ax.text(j, i, format_k(value), ha="center", va="center", color=text_color, fontsize=18)

    # Diagonal: show total skills for each marketplace.
    for i, total in enumerate(diagonal_totals):
        diag_val = matrix[i, i]
        text_color = "black" if np.isnan(diag_val) else ("white" if diag_val > vmax * 0.45 else "black")
        ax.text(i, i, format_k(float(total)), ha="center", va="center", color=text_color, fontsize=18)

    cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.set_label("Overlapping skills (count)", fontsize=18)
    cbar.ax.tick_params(labelsize=16)

    ax.set_xlabel("")
    ax.set_ylabel("")

    fig.tight_layout()
    fig.subplots_adjust(left=0.30, bottom=0.22, right=0.90)
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate heatmap of overlapping skill hashes.")
    parser.add_argument("--db", type=Path, default=Path("full_skill_marketplace.db"), help="Path to SQLite DB")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("marketplace_overlap_heatmap.pdf"),
        help="Output image path",
    )
    parser.add_argument("--dpi", type=int, default=250, help="Output image DPI")
    parser.add_argument(
        "--include-skills-sh-additional",
        action="store_true",
        help="Merge skill.sh_additional into Skills.sh bucket",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Only render lower triangle overlaps (hide cells above diagonal).",
    )
    parser.add_argument(
        "--color-diagonal",
        action="store_true",
        help="Color diagonal cells using each marketplace total.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if not args.db.exists():
        raise FileNotFoundError(f"Database not found: {args.db}")

    marketplaces = list(DEFAULT_MARKETPLACES)
    if args.include_skills_sh_additional:
        marketplaces = [
            ("ClawHub", ("clawhub",)),
            ("GHArchive", ("gharchive",)),
            ("Skills.sh", ("skill.sh", "skill.sh_additional")),
            ("SkillsDir", ("skillsdirectory",)),
        ]

    conn = sqlite3.connect(args.db)
    try:
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA busy_timeout=5000;")
        hash_sets = fetch_hash_sets(conn, marketplaces)
    finally:
        conn.close()

    labels, matrix = build_overlap_matrix(hash_sets, once=args.once, color_diagonal=args.color_diagonal)
    diagonal_totals = [len(hash_sets[label]) for label in labels]
    render_heatmap(labels, matrix, diagonal_totals, args.out, args.dpi)

    print(f"Wrote heatmap: {args.out}")
    print("Distinct hash counts per marketplace:")
    for label in labels:
        print(f"- {label}: {len(hash_sets[label]):,}")


if __name__ == "__main__":
    main()
