#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import sqlite3
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Combine flagged skills + repo metadata and rank repos per skill_hash")
    ap.add_argument("--skills-input", required=True, help="flagged_skills_input.csv from collector")
    ap.add_argument("--repo-metadata-db", required=True, help="repo_metadata.db from collect_repo_metadata.py")
    ap.add_argument("--out-dir", default="skillfix/output/flagged_repo_bundles/inputs")
    ap.add_argument(
        "--skill-manifest",
        default="",
        help="Optional flagged_skill_manifest.csv with stripped_zip paths to copy ranked bundles",
    )
    ap.add_argument(
        "--copy-ranked-zips",
        action="store_true",
        help="Copy stripped zips for repo_rank=1 and repo_rank=2..3 to output folders",
    )
    ap.add_argument(
        "--rank-strategy",
        choices=("priority", "stars"),
        default="priority",
        help="priority: composite score (default), stars: highest star occurrence first per skill_hash",
    )
    return ap.parse_args()


def read_skills(path: Path) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    with path.open("r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            skill_hash = str(row.get("skill_hash") or "").strip()
            repository = str(row.get("repository") or "").strip()
            skill_path = str(row.get("skill_path") or "").strip()
            if not skill_hash or not repository or not skill_path:
                continue
            rows.append({"skill_hash": skill_hash, "repository": repository, "skill_path": skill_path})
    return rows


def load_repo_meta(db: Path) -> Dict[str, Dict[str, object]]:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        out: Dict[str, Dict[str, object]] = {}
        for r in conn.execute(
            """
            SELECT repository,
                   COALESCE(gh_stars, state_stars, 0) AS stars,
                   gh_pushed_at,
                   gh_updated_at,
                   state_processed_at
            FROM repo_metadata
            """
        ):
            out[str(r["repository"])] = {
                "stars": int(r["stars"] or 0),
                "gh_pushed_at": r["gh_pushed_at"],
                "gh_updated_at": r["gh_updated_at"],
                "state_processed_at": r["state_processed_at"],
            }
        return out
    finally:
        conn.close()


def parse_dt(v: Optional[str]) -> Optional[datetime]:
    s = str(v or "").strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return None


def activity_score(last_dt: Optional[datetime]) -> float:
    if last_dt is None:
        return 0.3
    days = (datetime.now(timezone.utc) - last_dt).days
    if days <= 30:
        return 1.0
    if days <= 180:
        return 0.7
    if days <= 365:
        return 0.4
    return 0.2


def root_proximity_score(skill_path: str) -> float:
    p = str(skill_path or "").strip().strip("/")
    if not p:
        return 0.0
    depth = max(len(p.split("/")) - 1, 0)
    if depth == 0:
        return 1.0
    if depth <= 2:
        return 0.7
    return 0.4


def stars_score(stars: int) -> float:
    s = max(int(stars or 0), 0)
    return min(math.log10(s + 1) / 4.0, 1.0)


def write_csv(path: Path, header: List[str], rows: Iterable[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        for row in rows:
            w.writerow(row)


def read_manifest_zips(path: Path) -> Dict[Tuple[str, str, str], str]:
    out: Dict[Tuple[str, str, str], str] = {}
    with path.open("r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            k = (
                str(row.get("skill_hash") or "").strip(),
                str(row.get("repository") or "").strip(),
                str(row.get("skill_path") or "").strip(),
            )
            z = str(row.get("stripped_zip") or "").strip()
            if not all(k) or not z:
                continue
            out[k] = z
    return out


def copy_ranked_zips(
    ranked: List[Dict[str, object]],
    zip_index: Dict[Tuple[str, str, str], str],
    out_dir: Path,
) -> Tuple[int, int, int]:
    top_dir = out_dir / "top_ranked"
    round23_dir = out_dir / "rank_2_to_3"
    top_dir.mkdir(parents=True, exist_ok=True)
    round23_dir.mkdir(parents=True, exist_ok=True)

    copied_top = 0
    copied_23 = 0
    missing = 0
    copied_keys: Set[Tuple[int, Path]] = set()
    for r in ranked:
        rank = int(r["repo_rank"])
        if rank != 1 and not (2 <= rank <= 3):
            continue
        key = (str(r["skill_hash"]), str(r["repository"]), str(r["skill_path"]))
        src_s = zip_index.get(key, "")
        if not src_s:
            missing += 1
            continue
        src = Path(src_s)
        if not src.exists():
            missing += 1
            continue
        dst_dir = top_dir if rank == 1 else round23_dir
        dst = dst_dir / src.name
        uniq = (rank, dst)
        if uniq in copied_keys:
            continue
        shutil.copy2(src, dst)
        copied_keys.add(uniq)
        if rank == 1:
            copied_top += 1
        else:
            copied_23 += 1
    return copied_top, copied_23, missing


def build_ranked(
    skills: List[Dict[str, str]],
    repo_meta: Dict[str, Dict[str, object]],
    rank_strategy: str = "priority",
) -> List[Dict[str, object]]:
    grouped: Dict[str, List[Dict[str, str]]] = {}
    for r in skills:
        grouped.setdefault(r["skill_hash"], []).append(r)

    out: List[Dict[str, object]] = []
    for skill_hash, items in grouped.items():
        scored: List[Tuple[float, Dict[str, str], Dict[str, object]]] = []
        for it in items:
            meta = repo_meta.get(it["repository"], {})
            stars = int(meta.get("stars", 0) or 0)
            last_dt = parse_dt(meta.get("gh_pushed_at")) or parse_dt(meta.get("gh_updated_at")) or parse_dt(meta.get("state_processed_at"))
            sc_stars = stars_score(stars)
            sc_activity = activity_score(last_dt)
            sc_root = root_proximity_score(it["skill_path"])
            readme_proxy = 0.5
            priority = round(100.0 * (0.45 * sc_stars + 0.35 * sc_activity + 0.15 * sc_root + 0.05 * readme_proxy), 2)
            scored.append((priority, it, {"stars": stars, "activity_score": sc_activity, "root_proximity_score": sc_root, "readme_quality_proxy": readme_proxy}))

        if rank_strategy == "stars":
            scored.sort(key=lambda x: (-x[2]["stars"], -x[0], x[1]["repository"], x[1]["skill_path"]))
        else:
            scored.sort(key=lambda x: (-x[0], -x[2]["stars"], x[1]["repository"], x[1]["skill_path"]))
        repo_count = len(scored)
        for idx, (priority, it, extra) in enumerate(scored, start=1):
            out.append(
                {
                    "skill_hash": skill_hash,
                    "repository": it["repository"],
                    "skill_path": it["skill_path"],
                    "repo_count_per_skill_hash": repo_count,
                    "repo_rank": idx,
                    "priority_score": priority,
                    "stars": extra["stars"],
                    "activity_score": round(float(extra["activity_score"]), 3),
                    "root_proximity_score": round(float(extra["root_proximity_score"]), 3),
                    "readme_quality_proxy": round(float(extra["readme_quality_proxy"]), 3),
                    "rank_strategy": rank_strategy,
                }
            )
    return out


def main() -> None:
    args = parse_args()
    skills_input = Path(args.skills_input).resolve()
    repo_db = Path(args.repo_metadata_db).resolve()
    out_dir = Path(args.out_dir).resolve()

    skills = read_skills(skills_input)
    repo_meta = load_repo_meta(repo_db)
    ranked = build_ranked(skills, repo_meta, rank_strategy=args.rank_strategy)

    ranked_csv = out_dir / "flagged_skills_repo_context_ranked.csv"
    round1_csv = out_dir / "flagged_skills_repo_context_round1.csv"
    round23_csv = out_dir / "flagged_skills_repo_context_round2to3.csv"

    header = [
        "skill_hash",
        "repository",
        "skill_path",
        "repo_count_per_skill_hash",
        "repo_rank",
        "priority_score",
        "stars",
        "activity_score",
        "root_proximity_score",
        "readme_quality_proxy",
        "rank_strategy",
    ]

    write_csv(ranked_csv, header, ranked)
    write_csv(round1_csv, header, (r for r in ranked if int(r["repo_rank"]) == 1))
    write_csv(round23_csv, header, (r for r in ranked if 2 <= int(r["repo_rank"]) <= 3))

    copied_top = copied_23 = missing = 0
    if args.copy_ranked_zips:
        if not args.skill_manifest:
            raise SystemExit("--copy-ranked-zips requires --skill-manifest")
        manifest_path = Path(args.skill_manifest).resolve()
        if not manifest_path.exists():
            raise SystemExit(f"skill manifest not found: {manifest_path}")
        zip_index = read_manifest_zips(manifest_path)
        copied_top, copied_23, missing = copy_ranked_zips(ranked, zip_index, out_dir)

    print(f"Input skill rows: {len(skills)}")
    print(f"Ranked rows: {len(ranked)}")
    print(f"Ranked CSV: {ranked_csv}")
    print(f"Round1 CSV: {round1_csv}")
    print(f"Round2-3 CSV: {round23_csv}")
    print(f"Rank strategy: {args.rank_strategy}")
    if args.copy_ranked_zips:
        print(f"Top ranked zips folder: {out_dir / 'top_ranked'}")
        print(f"Rank 2-3 zips folder: {out_dir / 'rank_2_to_3'}")
        print(f"Copied top ranked zips: {copied_top}")
        print(f"Copied rank 2-3 zips: {copied_23}")
        print(f"Missing zip mappings/files: {missing}")


if __name__ == "__main__":
    main()
