#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd
from matplotlib.ticker import FuncFormatter


MARKET_DISPLAY = {
    "clawhub": "ClawHub",
    "gharchive": "GitHub",
    "skills_sh": "Skills.sh",
    "skillsdirectory": "SkillsDir.",
}

MARKET_COLORS = {
    "clawhub": "#B91C1C",
    "gharchive": "#0F766E",
    "skills_sh": "#1F3A8A",
    "skillsdirectory": "#111111",
}

MARKET_LINESTYLES = {
    "clawhub": (0, (4, 2)),
    "gharchive": "solid",
    "skills_sh": (0, (7, 2)),
    "skillsdirectory": (0, (2, 2)),
}

MARKET_MARKERS = {
    "clawhub": "D",
    "gharchive": "o",
    "skills_sh": "s",
    "skillsdirectory": "^",
}


@dataclass
class SkillTimestamp:
    market: str
    key: str
    timestamp: datetime


def parse_ts(value: object) -> Optional[datetime]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def parse_epoch_ms(value: object) -> Optional[datetime]:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def normalize_name(value: object) -> str:
    text = str(value or "").strip().lower()
    return "".join(ch for ch in text if ch.isalnum())


def make_key(skill_hash: object, fallback_name: object) -> str:
    skill_hash_text = str(skill_hash or "").strip()
    if skill_hash_text:
        return f"hash:{skill_hash_text}"
    return f"name:{normalize_name(fallback_name)}"


def upsert_latest(points: Dict[Tuple[str, str], SkillTimestamp], point: SkillTimestamp) -> None:
    key = (point.market, point.key)
    old = points.get(key)
    if old is None or point.timestamp > old.timestamp:
        points[key] = point


def load_marketplace_timestamps(conn: sqlite3.Connection, include_gharchive_additional: bool) -> Iterable[SkillTimestamp]:
    rows = conn.execute(
        """
        SELECT
            m.name AS market,
            COALESCE(NULLIF(im.skill_hash, ''), NULLIF(s.skill_hash, '')) AS skill_hash,
            COALESCE(NULLIF(im.name, ''), NULLIF(im.slug, ''), NULLIF(s.slug, ''), NULLIF(s.skill_key, ''), s.skill_path) AS fallback_name,
            COALESCE(NULLIF(im.skill_last_modified_at, ''), NULLIF(s.skill_last_modified_at, '')) AS timestamp
        FROM skill_marketplace_links AS sml
        JOIN marketplaces AS m ON m.id = sml.marketplace_id
        JOIN skills AS s ON s.repository = sml.repository AND s.skill_path = sml.skill_path
        LEFT JOIN skill_input_metadata AS im
            ON im.marketplace = m.name
           AND im.repository = sml.repository
           AND im.skill_path = sml.skill_path
        """
    )
    for row in rows:
        ts = parse_ts(row["timestamp"])
        if ts is None:
            continue
        yield SkillTimestamp(
            market=str(row["market"]).strip().lower(),
            key=make_key(row["skill_hash"], row["fallback_name"]),
            timestamp=ts,
        )

    if not include_gharchive_additional:
        return

    rows = conn.execute(
        """
        SELECT
            m.name AS market,
            sa.skill_hash AS skill_hash,
            COALESCE(NULLIF(sa.skill_name, ''), sa.skill_path) AS fallback_name,
            sa.skill_last_modified_at AS timestamp
        FROM skills_additional_marketplace_links AS saml
        JOIN marketplaces AS m ON m.id = saml.marketplace_id
        JOIN skills_additional AS sa ON sa.repository = saml.repository AND sa.skill_path = saml.skill_path
        WHERE m.name = 'gharchive'
        """
    )
    for row in rows:
        ts = parse_ts(row["timestamp"])
        if ts is None:
            continue
        yield SkillTimestamp(
            market="gharchive",
            key=make_key(row["skill_hash"], row["fallback_name"]),
            timestamp=ts,
        )


def load_clawhub_hash_filter(path: Path) -> Optional[set[str]]:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict):
        return {str(key).strip().lower() for key in data if str(key).strip()}
    if isinstance(data, list):
        return {str(value).strip().lower() for value in data if str(value).strip()}
    raise RuntimeError(f"Unsupported ClawHub hash filter format: {path}")


def load_clawhub_timestamps(path: Path, allowed_hashes: Optional[set[str]]) -> Iterable[SkillTimestamp]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue

            skill_hash = str(obj.get("skill_hash") or "").strip().lower()
            if allowed_hashes is not None and skill_hash not in allowed_hashes:
                continue

            ts = (
                parse_ts(obj.get("created_at_iso"))
                or parse_epoch_ms(obj.get("createdAt"))
                or parse_ts(obj.get("skill_uploaded_at"))
                or parse_ts(obj.get("skill_last_modified_at"))
                or parse_ts(obj.get("updated_at_iso"))
                or parse_epoch_ms(obj.get("updatedAt"))
                or parse_epoch_ms(obj.get("ts"))
            )
            if ts is None:
                continue
            fallback_name = obj.get("slug") or obj.get("key") or ""
            yield SkillTimestamp("clawhub", make_key(skill_hash, fallback_name), ts)


def apply_style() -> None:
    plt.style.use("seaborn-v0_8-white")
    plt.rcParams.update(
        {
            "figure.dpi": 180,
            "savefig.dpi": 350,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "font.size": 10.5,
            "axes.labelsize": 10.5,
            "xtick.labelsize": 9.5,
            "ytick.labelsize": 9.5,
            "legend.fontsize": 8.8,
            "axes.linewidth": 0.8,
            "lines.linewidth": 1.8,
        }
    )


def kfmt(value: float, _pos: int) -> str:
    if value <= 0:
        return ""
    if value >= 1000:
        k = value / 1000.0
        return f"{int(k)}K" if abs(k - round(k)) < 1e-9 else f"{k:.1f}K"
    return f"{int(value)}"


def plot_weekly(df: pd.DataFrame, out_path: Path, ylabel: str, fig_width: float, fig_height: float) -> None:
    fig, ax = plt.subplots(figsize=(fig_width, fig_height))
    ordered_markets = ["skillsdirectory", "skills_sh", "gharchive", "clawhub"]

    for market in ordered_markets:
        g = df[df["market"] == market].copy()
        if g.empty:
            continue
        g["week"] = g["timestamp_utc"].dt.tz_localize(None).dt.to_period("W").dt.start_time
        weekly = g.groupby("week").size().sort_index()
        ax.plot(
            weekly.index,
            weekly.values,
            label=MARKET_DISPLAY.get(market, market),
            color=MARKET_COLORS.get(market, "#5470C6"),
            linestyle=MARKET_LINESTYLES.get(market, "solid"),
            marker=MARKET_MARKERS.get(market, "o"),
            markersize=3.0,
            markevery=max(1, len(weekly) // 8),
        )

    ax.set_yscale("log")
    ax.set_xlabel("Week")
    ax.set_ylabel(ylabel)
    ax.yaxis.set_major_formatter(FuncFormatter(kfmt))
    ax.yaxis.set_major_locator(mticker.LogLocator(base=10, numticks=5))
    ax.yaxis.set_minor_locator(mticker.NullLocator())
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=1))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.grid(axis="y", alpha=0.18, linewidth=0.7)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.legend(loc="upper left", frameon=False, ncol=1)
    for label in ax.get_xticklabels():
        label.set_rotation(35)
        label.set_ha("right")

    fig.tight_layout(pad=0.25)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, bbox_inches="tight")
    if out_path.suffix.lower() != ".pdf":
        fig.savefig(out_path.with_suffix(".pdf"), bbox_inches="tight")
    if out_path.suffix.lower() != ".png":
        fig.savefig(out_path.with_suffix(".png"), bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate the weekly skill freshness timeline from the marketplace crawl database."
    )
    parser.add_argument("--db", default="../../data/sqlite.db", help="Path to the crawl SQLite database")
    parser.add_argument(
        "--clawhub-jsonl",
        default="../../data/clawhub_hashed.jsonl",
        help="Optional enriched ClawHub JSONL input",
    )
    parser.add_argument(
        "--clawhub-hashes",
        default="../clawhub_hashes.json",
        help="Optional ClawHub hash filter; defaults to the 16,755-hash repro set",
    )
    parser.add_argument(
        "--no-clawhub-hash-filter",
        action="store_true",
        help="Use every timestamped ClawHub row from --clawhub-jsonl instead of filtering to repro/clawhub_hashes.json.",
    )
    parser.add_argument("--out", default="timeline_weekly_latest_updated.pdf", help="Output PDF or PNG path")
    parser.add_argument("--timeline-start", default="2025-10-15", help="Lower timestamp bound, YYYY-MM-DD")
    parser.add_argument(
        "--ylabel",
        default="Modified/uploaded skills",
        help="Y-axis label. The y-axis is log-scaled.",
    )
    parser.add_argument("--fig-width", type=float, default=3.35, help="Figure width in inches")
    parser.add_argument("--fig-height", type=float, default=3.0, help="Figure height in inches")
    parser.add_argument(
        "--no-gharchive-additional",
        action="store_true",
        help="Exclude additional skills discovered in GitHub repositories.",
    )
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = (script_dir / db_path).resolve()
    clawhub_path = Path(args.clawhub_jsonl)
    if not clawhub_path.is_absolute():
        clawhub_path = (script_dir / clawhub_path).resolve()
    clawhub_hashes_path = Path(args.clawhub_hashes)
    if not clawhub_hashes_path.is_absolute():
        clawhub_hashes_path = (script_dir / clawhub_hashes_path).resolve()
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = (script_dir / out_path).resolve()

    apply_style()
    points: Dict[Tuple[str, str], SkillTimestamp] = {}
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        for point in load_marketplace_timestamps(conn, not args.no_gharchive_additional):
            upsert_latest(points, point)
    finally:
        conn.close()

    clawhub_allowed_hashes = None if args.no_clawhub_hash_filter else load_clawhub_hash_filter(clawhub_hashes_path)
    if clawhub_path.exists():
        for point in load_clawhub_timestamps(clawhub_path, clawhub_allowed_hashes):
            upsert_latest(points, point)

    if not points:
        raise RuntimeError("No timestamped skill rows found.")

    timeline_start = datetime.fromisoformat(args.timeline_start).replace(tzinfo=timezone.utc)
    df = pd.DataFrame(
        [{"market": point.market, "timestamp_utc": pd.to_datetime(point.timestamp, utc=True)} for point in points.values()]
    )
    df = df[df["timestamp_utc"] >= pd.Timestamp(timeline_start)]
    if df.empty:
        raise RuntimeError("No timestamped skill rows remain after --timeline-start.")

    plot_weekly(df, out_path, args.ylabel, args.fig_width, args.fig_height)

    summary = {
        "db": str(db_path),
        "clawhub_jsonl": str(clawhub_path) if clawhub_path.exists() else "",
        "clawhub_hash_filter": str(clawhub_hashes_path) if clawhub_allowed_hashes is not None else "",
        "clawhub_hash_filter_size": len(clawhub_allowed_hashes) if clawhub_allowed_hashes is not None else 0,
        "timeline_start": args.timeline_start,
        "ylabel": args.ylabel,
        "log_y_axis": True,
        "rows_after_dedup_and_filter": int(len(df)),
        "rows_by_market": {str(k): int(v) for k, v in df.groupby("market").size().sort_index().items()},
        "outputs": [str(out_path), str(out_path.with_suffix(".png"))],
        "semantics": "Each skill is counted in the week of its last-modified or uploaded timestamp; ClawHub uses upload/created timestamps first when available.",
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
