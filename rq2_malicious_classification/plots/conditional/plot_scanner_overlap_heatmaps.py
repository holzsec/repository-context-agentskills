#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import List

import matplotlib.pyplot as plt
import numpy as np

plt.rcParams["pdf.fonttype"] = 42
plt.rcParams["ps.fonttype"] = 42
plt.rcParams["font.family"] = "DejaVu Sans"


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Export camera-ready conditional-overlap heatmaps (PDF/PNG) in multiple color schemes.")
    ap.add_argument(
        "--summary-json",
        default="generated/conditional/skillsh_common_5scanner_summary.json",
        help="Summary JSON produced by build_skillsh_scanner_comparison.py",
    )
    ap.add_argument(
        "--out-dir",
        default="generated/conditional",
        help="Output directory for camera-ready plots",
    )
    return ap.parse_args()


def load_conditional_matrix(summary_json: Path) -> tuple[List[str], np.ndarray]:
    obj = json.loads(summary_json.read_text(encoding="utf-8"))
    d = obj.get("pairwise_conditional_overlap", {})
    labels = list(d.keys())
    m = np.zeros((len(labels), len(labels)), dtype=float)
    for i, a in enumerate(labels):
        row = d.get(a, {})
        for j, b in enumerate(labels):
            m[i, j] = float(row.get(b, 0.0))
    return labels, m


def export_variant(labels: List[str], mat: np.ndarray, cmap: str, out_base: Path) -> None:
    fig, ax = plt.subplots(figsize=(4.8, 3.4))
    im = ax.imshow(mat, cmap=cmap, vmin=0.0, vmax=1.0)

    ax.set_xticks(range(len(labels)))
    ax.set_yticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=22, ha="right", fontsize=11)
    ax.set_yticklabels(labels, fontsize=11)

    for i in range(len(labels)):
        for j in range(len(labels)):
            v = float(mat[i, j])
            txt = f"{100.0 * v:.1f}%"
            # adaptive text color for readability on dark colormaps
            color = "white" if v >= 0.58 else "#111827"
            ax.text(j, i, txt, ha="center", va="center", fontsize=10.0, color=color)

    cbar = fig.colorbar(im, ax=ax, fraction=0.050, pad=0.03)
    cbar.ax.tick_params(labelsize=10.2)
    cbar.set_label("Conditional Co-Flag Rate", fontsize=11)

    ax.set_xlabel("")
    ax.set_ylabel("")
    # no title (camera-ready requirement)

    fig.tight_layout(pad=0.3)
    out_base.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_base.with_suffix(".pdf"), bbox_inches="tight")
    fig.savefig(out_base.with_suffix(".png"), dpi=260, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    args = parse_args()
    summary_json = Path(args.summary_json).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    labels, mat = load_conditional_matrix(summary_json)
    variants = [
        ("option1_blues", "Blues"),
        ("option2_cividis", "cividis"),
        ("option3_magma", "magma"),
    ]
    outputs = []
    for suffix, cmap in variants:
        out_base = out_dir / f"skillsh_common_5scanner_conditional_overlap_camera_ready_{suffix}"
        export_variant(labels, mat, cmap, out_base)
        outputs.append(
            {
                "variant": suffix,
                "cmap": cmap,
                "pdf": str(out_base.with_suffix(".pdf")),
                "png": str(out_base.with_suffix(".png")),
            }
        )

    # Primary paper asset with requested filename (blue conditional heatmap).
    primary_base = out_dir / "skillsh_conditional"
    export_variant(labels, mat, "Blues", primary_base)

    print(
        json.dumps(
            {
                "summary_json": str(summary_json),
                "outputs": outputs,
                "primary": {
                    "pdf": str(primary_base.with_suffix(".pdf")),
                    "png": str(primary_base.with_suffix(".png")),
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
