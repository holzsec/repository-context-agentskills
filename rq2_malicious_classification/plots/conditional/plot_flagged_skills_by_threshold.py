#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List, Tuple

import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

plt.rcParams["pdf.fonttype"] = 42
plt.rcParams["ps.fonttype"] = 42
plt.rcParams["font.family"] = "DejaVu Sans"


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Export camera-ready flagged-by-k plots (PDF + PNG).")
    ap.add_argument(
        "--summary-json",
        default="generated/conditional/skillsh_common_5scanner_summary.json",
        help="Summary JSON from build_skillsh_scanner_comparison.py",
    )
    ap.add_argument(
        "--out-dir",
        default="generated/conditional",
        help="Output directory",
    )
    return ap.parse_args()


def load_k_counts(path: Path) -> List[Tuple[int, int]]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    d: Dict[str, int] = obj.get("flagged_by_k_scanners", {})
    pairs = sorted((int(k), int(v)) for k, v in d.items())
    return pairs


def kfmt(v: float, _pos: int) -> str:
    if v >= 1000:
        x = v / 1000.0
        if abs(x - int(x)) < 1e-9:
            return f"{int(x)}K"
        return f"{x:.1f}K"
    return f"{int(v)}"


def save(fig: plt.Figure, base: Path) -> None:
    base.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout(pad=0.25)
    fig.savefig(base.with_suffix(".pdf"), bbox_inches="tight")
    fig.savefig(base.with_suffix(".png"), dpi=260, bbox_inches="tight")
    plt.close(fig)


def plot_compact_bar(k_counts: List[Tuple[int, int]], out_base: Path, bar_color: str = "#1d4ed8") -> None:
    xs = [str(k) for k, _ in k_counts]
    ys = [v for _, v in k_counts]
    total = sum(ys) or 1
    fig, ax = plt.subplots(figsize=(4.3, 3.8))
    bars = ax.bar(xs, ys, color=bar_color, alpha=0.92, width=0.82)
    ax.yaxis.set_major_formatter(FuncFormatter(kfmt))
    ax.set_xlabel("Number of scanners flagging a skill", fontsize=13.0)
    ax.set_ylabel("Skills (K)", fontsize=13.0)
    ax.tick_params(axis="both", labelsize=11.6)
    ax.set_xlim(-0.45, len(xs) - 0.55)
    ax.grid(axis="y", alpha=0.24)
    for idx, (bar, v) in enumerate(zip(bars, ys), start=1):
        ax.text(bar.get_x() + bar.get_width() / 2, v, kfmt(v, 0), ha="center", va="bottom", fontsize=11.5)
        if idx in (1, 2) and v > 0:
            pct = 100.0 * v / total
            y_in = v * 0.58
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                y_in,
                f"{pct:.1f}%",
                ha="center",
                va="center",
                fontsize=12.0,
                color="white",
                fontweight="bold",
            )
    save(fig, out_base)


def plot_lollipop(k_counts: List[Tuple[int, int]], out_base: Path) -> None:
    xs = [k for k, _ in k_counts]
    ys = [v for _, v in k_counts]
    fig, ax = plt.subplots(figsize=(5.0, 3.0))
    ax.vlines(xs, [0] * len(xs), ys, color="#0f766e", linewidth=2.1, alpha=0.85)
    ax.scatter(xs, ys, color="#0f766e", s=70, zorder=3)
    ax.set_xticks(xs, [str(x) for x in xs])
    ax.yaxis.set_major_formatter(FuncFormatter(kfmt))
    ax.set_xlabel("Number of scanners flagging a skill", fontsize=13.0)
    ax.set_ylabel("Skills (K)", fontsize=13.0)
    ax.tick_params(axis="both", labelsize=12.0)
    ax.grid(axis="y", alpha=0.24)
    for x, y in zip(xs, ys):
        ax.text(x, y, kfmt(y, 0), ha="center", va="bottom", fontsize=11.5)
    save(fig, out_base)


def main() -> None:
    args = parse_args()
    summary_json = Path(args.summary_json).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    k_counts = load_k_counts(summary_json)
    if not k_counts:
        raise SystemExit("No flagged_by_k_scanners data found in summary JSON.")

    bar_base = out_dir / "skillsh_common_5scanner_flagged_by_k_camera_ready_bar"
    bar_alt_base = out_dir / "skillsh_common_5scanner_flagged_by_k_camera_ready_bar_altcolor"
    lol_base = out_dir / "skillsh_common_5scanner_flagged_by_k_camera_ready_lollipop"
    primary_bar_base = out_dir / "skillsh_conditional_bar"
    plot_compact_bar(k_counts, bar_base, bar_color="#1d4ed8")
    plot_compact_bar(k_counts, bar_alt_base, bar_color="#b45309")
    plot_compact_bar(k_counts, primary_bar_base, bar_color="#b45309")
    plot_lollipop(k_counts, lol_base)

    print(
        json.dumps(
            {
                "summary_json": str(summary_json),
                "outputs": {
                    "bar_pdf": str(bar_base.with_suffix(".pdf")),
                    "bar_png": str(bar_base.with_suffix(".png")),
                    "primary_bar_pdf": str(primary_bar_base.with_suffix(".pdf")),
                    "primary_bar_png": str(primary_bar_base.with_suffix(".png")),
                    "bar_alt_pdf": str(bar_alt_base.with_suffix(".pdf")),
                    "bar_alt_png": str(bar_alt_base.with_suffix(".png")),
                    "lollipop_pdf": str(lol_base.with_suffix(".pdf")),
                    "lollipop_png": str(lol_base.with_suffix(".png")),
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
