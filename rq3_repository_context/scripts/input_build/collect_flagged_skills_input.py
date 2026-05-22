#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


@dataclass
class SkillRow:
    skill_hash: str
    repository: str
    skill_path: str


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Collect strict flagged skills mapped to repositories")
    ap.add_argument("--scan-db", required=True, help="security_scan_with_questions.db")
    ap.add_argument("--state-db", required=True, help="sqlite.db with skills + input metadata")
    ap.add_argument(
        "--existing-scan-db",
        default="",
        help="Optional existing scan DB (e.g., skills_sh_security_scan_manual_crawl.db) merged into union input",
    )
    ap.add_argument("--out-dir", default="skillfix/output/flagged_repo_bundles/inputs")
    ap.add_argument("--run-id", type=int, default=None, help="question run id; default latest")
    ap.add_argument("--llm-threshold", type=int, default=3)
    ap.add_argument("--skillsh-marketplace", default="skills_sh", help="marketplace name used for Skill.sh-only any-scanner fail export")
    ap.add_argument("--repo-context-exclude-marketplaces", default="clawhub", help="marketplaces excluded from repository_context input files")
    ap.add_argument("--skip-unmapped-debug", action="store_true", help="Skip expensive unmapped hash diagnostics CSV generation")
    return ap.parse_args()


def normalize_hash(hash_with_prefix: str) -> str:
    m = re.match(r"^\d{2,3}_(.+)$", hash_with_prefix or "")
    return m.group(1) if m else (hash_with_prefix or "")


def normalize_marketplace_name(name: str) -> str:
    n = str(name or "").strip().lower().replace("-", "_").replace(".", "_")
    aliases = {
        "skills_sh": "skills_sh",
        "skillssh": "skills_sh",
        "skillssh": "skills_sh",
        "skills__sh": "skills_sh",
        "skills_sh_": "skills_sh",
        "skillsdirectory": "skillsdirectory",
        "gharchive": "gharchive",
        "clawhub": "clawhub",
    }
    return aliases.get(n, n)


def latest_run_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(run_id) FROM question_skill_results").fetchone()
    if not row or row[0] is None:
        raise RuntimeError("question_skill_results is empty")
    return int(row[0])


def load_fail_flag_sets(
    scan_db: Path, run_id: Optional[int], llm_threshold: int
) -> Tuple[Set[str], Set[str], Set[str]]:
    conn = sqlite3.connect(str(scan_db))
    conn.row_factory = sqlite3.Row
    try:
        rid = run_id if run_id is not None else latest_run_id(conn)
        cisco_high_fail: Set[str] = set()
        llm_fail: Set[str] = set()
        platform_fail: Set[str] = set()

        for row in conn.execute(
            """
            SELECT hash_with_prefix
            FROM scan_results
            WHERE max_severity IN ('HIGH', 'CRITICAL')
               OR high_count > 0
               OR critical_count > 0
            """
        ):
            h = normalize_hash(row["hash_with_prefix"])
            if h:
                cisco_high_fail.add(h)

        for row in conn.execute(
            """
            SELECT hash_with_prefix
            FROM question_skill_results
            WHERE run_id = ?
              AND COALESCE(overal_malicousness_rating, 0) > ?
            """,
            (rid, llm_threshold),
        ):
            h = normalize_hash(row["hash_with_prefix"])
            if h:
                llm_fail.add(h)

        for row in conn.execute(
            """
            SELECT hash_with_prefix
            FROM question_skill_results
            WHERE run_id = ?
              AND COALESCE(estimated_security_tool, 0) > 0
            """,
            (rid,),
        ):
            h = normalize_hash(row["hash_with_prefix"])
            if h:
                platform_fail.add(h)
        return cisco_high_fail, llm_fail, platform_fail
    finally:
        conn.close()


def load_existing_scan_fail_hashes(
    state_db: Path,
    existing_scan_db: Optional[Path],
    skillsh_marketplace: str,
) -> Tuple[Set[str], Dict[str, int]]:
    stats = {
        "existing_fail_source_skill": 0,
        "existing_fail_mapped_hashes": 0,
    }
    if existing_scan_db is None or not existing_scan_db.exists():
        return set(), stats

    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("ATTACH DATABASE ? AS existing_scan", (str(existing_scan_db),))
        has_skills = conn.execute(
            "SELECT 1 FROM existing_scan.sqlite_master WHERE type='table' AND name='skills'"
        ).fetchone()
        has_results = conn.execute(
            "SELECT 1 FROM existing_scan.sqlite_master WHERE type='table' AND name='skill_security_scanner_results'"
        ).fetchone()
        if not has_skills or not has_results:
            return set(), stats

        stats["existing_fail_source_skill"] = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT DISTINCT s.source, s.skill_id
                    FROM existing_scan.skill_security_scanner_results r
                    JOIN existing_scan.skills s ON s.id = r.skill_id
                    WHERE lower(COALESCE(r.status, '')) NOT IN ('pass', 'ok', 'clean')
                )
                """
            ).fetchone()[0]
        )

        rows = conn.execute(
            """
            SELECT DISTINCT sim.skill_hash
            FROM skill_input_metadata sim
            JOIN (
                SELECT DISTINCT s.source, s.skill_id
                FROM existing_scan.skill_security_scanner_results r
                JOIN existing_scan.skills s ON s.id = r.skill_id
                WHERE lower(COALESCE(r.status, '')) NOT IN ('pass', 'ok', 'clean')
            ) mf
              ON mf.source = sim.repository
             AND mf.skill_id = COALESCE(sim.slug, '')
            WHERE lower(trim(sim.marketplace)) = lower(?)
              AND COALESCE(sim.skill_hash, '') <> ''
            """,
            (normalize_marketplace_name(skillsh_marketplace),),
        ).fetchall()
        hashes = {str(r["skill_hash"]).strip() for r in rows if str(r["skill_hash"] or "").strip()}
        stats["existing_fail_mapped_hashes"] = len(hashes)
        return hashes, stats
    finally:
        try:
            conn.execute("DETACH DATABASE existing_scan")
        except sqlite3.Error:
            pass
        conn.close()


def load_skill_rows(
    state_db: Path,
    flagged_hashes: Set[str],
    exclude_marketplaces: Set[str],
    include_marketplaces: Optional[Set[str]] = None,
) -> List[SkillRow]:
    if not flagged_hashes:
        return []
    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    try:
        rows: List[SkillRow] = []
        include_norm = {normalize_marketplace_name(x) for x in (include_marketplaces or set()) if str(x).strip()}
        exclude_norm = {normalize_marketplace_name(x) for x in exclude_marketplaces if str(x).strip()}

        chunk = 900
        hashes = list(flagged_hashes)
        for i in range(0, len(hashes), chunk):
            part = hashes[i:i + chunk]
            placeholders = ",".join("?" for _ in part)
            if include_norm:
                m_ph = ",".join("?" for _ in include_norm)
                q = f"""
                    WITH entries AS (
                        SELECT s.skill_hash, s.repository, s.skill_path, lower(trim(sim.marketplace)) AS marketplace
                        FROM skills s
                        LEFT JOIN skill_input_metadata sim
                          ON sim.repository = s.repository AND sim.skill_path = s.skill_path
                        UNION ALL
                        SELECT sa.skill_hash, sa.repository, sa.skill_path, lower(trim(m.name)) AS marketplace
                        FROM skills_additional sa
                        LEFT JOIN skills_additional_marketplace_links l
                          ON l.repository = sa.repository AND l.skill_path = sa.skill_path
                        LEFT JOIN marketplaces m
                          ON m.id = l.marketplace_id
                    )
                    SELECT DISTINCT e.skill_hash, e.repository, e.skill_path
                    FROM entries e
                    WHERE e.skill_hash IN ({placeholders})
                      AND e.marketplace IN ({m_ph})
                """
                params: Sequence[str] = [*part, *sorted(include_norm)]
            elif exclude_norm:
                m_ph = ",".join("?" for _ in exclude_norm)
                q = f"""
                    WITH entries AS (
                        SELECT s.skill_hash, s.repository, s.skill_path, lower(trim(sim.marketplace)) AS marketplace
                        FROM skills s
                        LEFT JOIN skill_input_metadata sim
                          ON sim.repository = s.repository AND sim.skill_path = s.skill_path
                        UNION ALL
                        SELECT sa.skill_hash, sa.repository, sa.skill_path, lower(trim(m.name)) AS marketplace
                        FROM skills_additional sa
                        LEFT JOIN skills_additional_marketplace_links l
                          ON l.repository = sa.repository AND l.skill_path = sa.skill_path
                        LEFT JOIN marketplaces m
                          ON m.id = l.marketplace_id
                    )
                    SELECT DISTINCT e.skill_hash, e.repository, e.skill_path
                    FROM entries e
                    WHERE e.skill_hash IN ({placeholders})
                      AND (
                        e.marketplace IS NULL
                        OR e.marketplace NOT IN ({m_ph})
                      )
                """
                params = [*part, *sorted(exclude_norm)]
            else:
                q = f"""
                    SELECT DISTINCT skill_hash, repository, skill_path
                    FROM (
                        SELECT skill_hash, repository, skill_path FROM skills
                        UNION ALL
                        SELECT skill_hash, repository, skill_path FROM skills_additional
                    )
                    WHERE skill_hash IN ({placeholders})
                """
                params = part
            for r in conn.execute(q, params):
                rows.append(SkillRow(skill_hash=r["skill_hash"], repository=r["repository"], skill_path=r["skill_path"]))
        return rows
    finally:
        conn.close()


def build_marketplace_summary(
    state_db: Path, hashes: Set[str], label: str, out_dir: Path
) -> Tuple[Path, Dict[str, int]]:
    out_csv = out_dir / f"{label}_marketplace_counts.csv"
    if not hashes:
        write_csv(out_csv, ["marketplace", "flagged_hashes"], [])
        return out_csv, {}

    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    try:
        rows: List[Dict[str, object]] = []
        chunk = 900
        items = sorted(hashes)
        counts: Dict[str, int] = {}
        for i in range(0, len(items), chunk):
            part = items[i : i + chunk]
            ph = ",".join("?" for _ in part)
            q = f"""
                SELECT lower(trim(marketplace)) AS marketplace, COUNT(DISTINCT skill_hash) AS c
                FROM skill_input_metadata
                WHERE skill_hash IN ({ph})
                GROUP BY lower(trim(marketplace))
            """
            for r in conn.execute(q, part):
                k = str(r["marketplace"] or "")
                counts[k] = counts.get(k, 0) + int(r["c"] or 0)
        for mk, c in sorted(counts.items()):
            rows.append({"marketplace": mk, "flagged_hashes": c})
        write_csv(out_csv, ["marketplace", "flagged_hashes"], rows)
        return out_csv, counts
    finally:
        conn.close()


def source_label(cisco_fail: bool, llm_fail: bool) -> str:
    if cisco_fail and llm_fail:
        return "both"
    if cisco_fail:
        return "cisco_only"
    if llm_fail:
        return "llm_only"
    return "none"


def source_label_with_platform(cisco_fail: bool, llm_fail: bool, platform_fail: bool) -> str:
    active: List[str] = []
    if cisco_fail:
        active.append("cisco")
    if llm_fail:
        active.append("llm")
    if platform_fail:
        active.append("platform")
    return "+".join(active) if active else "none"


def write_unmapped_hashes(
    scan_db: Path,
    state_db: Path,
    all_hashes: Set[str],
    mapped_rows: List[SkillRow],
    out_csv: Path,
) -> int:
    mapped_hashes = {r.skill_hash for r in mapped_rows if r.skill_hash}
    unmapped = sorted(h for h in all_hashes if h not in mapped_hashes)
    if not unmapped:
        write_csv(
            out_csv,
            ["skill_hash", "in_skills", "in_skills_additional", "marketplaces_from_scan_db"],
            [],
        )
        return 0

    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    rows: List[Dict[str, object]] = []
    try:
        conn.execute("ATTACH DATABASE ? AS scan", (str(scan_db),))
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _unmapped_hashes (skill_hash TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM _unmapped_hashes")
        conn.executemany(
            "INSERT OR IGNORE INTO _unmapped_hashes(skill_hash) VALUES (?)",
            [(h,) for h in unmapped],
        )

        # marketplace list depends on scan.market_presence availability.
        has_market_presence = conn.execute(
            "SELECT 1 FROM scan.sqlite_master WHERE type='table' AND name='market_presence'"
        ).fetchone()
        marketplace_expr = (
            "COALESCE(m.marketplaces, '') AS marketplaces_from_scan_db"
            if has_market_presence
            else "'' AS marketplaces_from_scan_db"
        )
        marketplace_join = (
            """
            LEFT JOIN (
                SELECT mp.skill_hash, GROUP_CONCAT(mp.marketplace, ',') AS marketplaces
                FROM (
                    SELECT DISTINCT skill_hash, marketplace
                    FROM scan.market_presence
                    WHERE skill_hash IN (SELECT skill_hash FROM _unmapped_hashes)
                    ORDER BY marketplace
                ) mp
                GROUP BY mp.skill_hash
            ) m ON m.skill_hash = u.skill_hash
            """
            if has_market_presence
            else ""
        )

        q = f"""
            SELECT
                u.skill_hash,
                COALESCE(s.cnt, 0) AS in_skills,
                COALESCE(a.cnt, 0) AS in_skills_additional,
                {marketplace_expr}
            FROM _unmapped_hashes u
            LEFT JOIN (
                SELECT skill_hash, COUNT(*) AS cnt
                FROM skills
                WHERE skill_hash IN (SELECT skill_hash FROM _unmapped_hashes)
                GROUP BY skill_hash
            ) s ON s.skill_hash = u.skill_hash
            LEFT JOIN (
                SELECT skill_hash, COUNT(*) AS cnt
                FROM skills_additional
                WHERE skill_hash IN (SELECT skill_hash FROM _unmapped_hashes)
                GROUP BY skill_hash
            ) a ON a.skill_hash = u.skill_hash
            {marketplace_join}
            ORDER BY u.skill_hash
        """
        for r in conn.execute(q):
            rows.append(
                {
                    "skill_hash": r["skill_hash"],
                    "in_skills": int(r["in_skills"] or 0),
                    "in_skills_additional": int(r["in_skills_additional"] or 0),
                    "marketplaces_from_scan_db": str(r["marketplaces_from_scan_db"] or ""),
                }
            )
    finally:
        try:
            conn.execute("DROP TABLE IF EXISTS _unmapped_hashes")
            conn.execute("DETACH DATABASE scan")
        except sqlite3.Error:
            pass
        conn.close()

    write_csv(
        out_csv,
        ["skill_hash", "in_skills", "in_skills_additional", "marketplaces_from_scan_db"],
        rows,
    )
    return len(unmapped)


def write_csv(path: Path, header: List[str], rows: Iterable[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        for row in rows:
            w.writerow(row)



def main() -> None:
    args = parse_args()
    scan_db = Path(args.scan_db).resolve()
    state_db = Path(args.state_db).resolve()
    out_dir = Path(args.out_dir).resolve()

    repo_context_excluded = {x.strip().lower() for x in args.repo_context_exclude_marketplaces.split(",") if x.strip()}

    cisco_fail_hashes, llm_fail_hashes, platform_fail_hashes = load_fail_flag_sets(
        scan_db, args.run_id, args.llm_threshold
    )
    existing_scan_db = Path(args.existing_scan_db).resolve() if args.existing_scan_db else None
    existing_fail_hashes, existing_stats = load_existing_scan_fail_hashes(
        state_db=state_db,
        existing_scan_db=existing_scan_db,
        skillsh_marketplace=args.skillsh_marketplace,
    )
    flagged_hashes = cisco_fail_hashes & llm_fail_hashes
    any_fail_hashes = cisco_fail_hashes | llm_fail_hashes
    union_with_existing_hashes = any_fail_hashes | existing_fail_hashes
    skill_rows_all_markets = load_skill_rows(state_db, flagged_hashes, exclude_marketplaces=set())
    skill_rows_repo_context = load_skill_rows(state_db, flagged_hashes, exclude_marketplaces=repo_context_excluded)
    any_fail_rows = load_skill_rows(state_db, any_fail_hashes, exclude_marketplaces=set())
    union_rows_all_markets = load_skill_rows(state_db, union_with_existing_hashes, exclude_marketplaces=set())
    union_rows_repo_context = load_skill_rows(state_db, union_with_existing_hashes, exclude_marketplaces=repo_context_excluded)
    skillsh_only = {args.skillsh_marketplace.strip().lower()}
    any_fail_skillsh_rows = load_skill_rows(
        state_db=state_db,
        flagged_hashes=any_fail_hashes,
        exclude_marketplaces=set(),
        include_marketplaces=skillsh_only,
    )
    platform_fail_skillsh_rows = load_skill_rows(
        state_db=state_db,
        flagged_hashes=platform_fail_hashes,
        exclude_marketplaces=set(),
        include_marketplaces=skillsh_only,
    )
    skillsh_any_or_platform_hashes = any_fail_hashes | platform_fail_hashes
    skillsh_any_or_platform_rows = load_skill_rows(
        state_db=state_db,
        flagged_hashes=skillsh_any_or_platform_hashes,
        exclude_marketplaces=set(),
        include_marketplaces=skillsh_only,
    )

    skills_csv = out_dir / "flagged_skills_input.csv"
    repos_csv = out_dir / "flagged_repos_input.csv"
    skills_all_csv = out_dir / "flagged_skills_all_markets_input.csv"
    repos_all_csv = out_dir / "flagged_repos_all_markets_input.csv"
    any_all_skills_csv = out_dir / "any_scanner_fail_skills_input.csv"
    any_all_repos_csv = out_dir / "any_scanner_fail_repos_input.csv"
    any_skills_csv = out_dir / "any_scanner_fail_skillsh_skills_input.csv"
    any_repos_csv = out_dir / "any_scanner_fail_skillsh_repos_input.csv"
    skillsh_flags_csv = out_dir / "skillsh_any_scanner_flags.csv"
    skillsh_any_or_platform_csv = out_dir / "skillsh_any_scanner_or_platform_skills_input.csv"
    strict_unmapped_csv = out_dir / "strict_unmapped_hashes.csv"
    any_unmapped_csv = out_dir / "any_scanner_unmapped_hashes.csv"
    union_skills_csv = out_dir / "union_input.csv"
    union_repos_csv = out_dir / "union_repos_input.csv"

    # repository_context input: strict set (non-clawhub) + Skill.sh platform-scanner fails
    repo_context_rows = _dedupe_rows(skill_rows_repo_context + platform_fail_skillsh_rows)
    write_csv(
        skills_csv,
        ["skill_hash", "repository", "skill_path"],
        (
            {"skill_hash": s.skill_hash, "repository": s.repository, "skill_path": s.skill_path}
            for s in sorted(repo_context_rows, key=lambda x: (x.repository, x.skill_path, x.skill_hash))
        ),
    )
    write_csv(
        repos_csv,
        ["repository", "flagged_skill_count"],
        (
            {"repository": repo, "flagged_skill_count": count}
            for repo, count in sorted(_count_by_repo(repo_context_rows).items())
        ),
    )
    # strict all-markets (including ClawHub)
    write_csv(
        skills_all_csv,
        ["skill_hash", "repository", "skill_path"],
        (
            {"skill_hash": s.skill_hash, "repository": s.repository, "skill_path": s.skill_path}
            for s in sorted(skill_rows_all_markets, key=lambda x: (x.repository, x.skill_path, x.skill_hash))
        ),
    )
    write_csv(
        repos_all_csv,
        ["repository", "flagged_skill_count"],
        (
            {"repository": repo, "flagged_skill_count": count}
            for repo, count in sorted(_count_by_repo(skill_rows_all_markets).items())
        ),
    )
    write_csv(
        any_all_skills_csv,
        ["skill_hash", "repository", "skill_path", "source_label"],
        (
            {
                "skill_hash": s.skill_hash,
                "repository": s.repository,
                "skill_path": s.skill_path,
                "source_label": source_label(
                    s.skill_hash in cisco_fail_hashes,
                    s.skill_hash in llm_fail_hashes,
                ),
            }
            for s in sorted(any_fail_rows, key=lambda x: (x.repository, x.skill_path, x.skill_hash))
        ),
    )
    write_csv(
        any_all_repos_csv,
        ["repository", "flagged_skill_count"],
        (
            {"repository": repo, "flagged_skill_count": count}
            for repo, count in sorted(_count_by_repo(any_fail_rows).items())
        ),
    )
    write_csv(
        union_skills_csv,
        ["skill_hash", "repository", "skill_path"],
        (
            {"skill_hash": s.skill_hash, "repository": s.repository, "skill_path": s.skill_path}
            for s in sorted(union_rows_repo_context, key=lambda x: (x.repository, x.skill_path, x.skill_hash))
        ),
    )
    write_csv(
        union_repos_csv,
        ["repository", "flagged_skill_count"],
        (
            {"repository": repo, "flagged_skill_count": count}
            for repo, count in sorted(_count_by_repo(union_rows_repo_context).items())
        ),
    )
    write_csv(
        any_skills_csv,
        ["skill_hash", "repository", "skill_path", "source_label", "cisco_fail", "llm_fail", "any_fail"],
        (
            {
                "skill_hash": s.skill_hash,
                "repository": s.repository,
                "skill_path": s.skill_path,
                "source_label": source_label(
                    s.skill_hash in cisco_fail_hashes,
                    s.skill_hash in llm_fail_hashes,
                ),
                "cisco_fail": int(s.skill_hash in cisco_fail_hashes),
                "llm_fail": int(s.skill_hash in llm_fail_hashes),
                "any_fail": 1,
            }
            for s in sorted(any_fail_skillsh_rows, key=lambda x: (x.repository, x.skill_path, x.skill_hash))
        ),
    )
    write_csv(
        any_repos_csv,
        ["repository", "flagged_skill_count"],
        (
            {"repository": repo, "flagged_skill_count": count}
            for repo, count in sorted(_count_by_repo(any_fail_skillsh_rows).items())
        ),
    )
    write_csv(
        skillsh_flags_csv,
        ["skill_hash", "repository", "skill_path", "source_label", "cisco_fail", "llm_fail", "platform_fail", "any_fail"],
        (
            {
                "skill_hash": s.skill_hash,
                "repository": s.repository,
                "skill_path": s.skill_path,
                "source_label": source_label_with_platform(
                    s.skill_hash in cisco_fail_hashes,
                    s.skill_hash in llm_fail_hashes,
                    s.skill_hash in platform_fail_hashes,
                ),
                "cisco_fail": int(s.skill_hash in cisco_fail_hashes),
                "llm_fail": int(s.skill_hash in llm_fail_hashes),
                "platform_fail": int(s.skill_hash in platform_fail_hashes),
                "any_fail": int((s.skill_hash in cisco_fail_hashes) or (s.skill_hash in llm_fail_hashes)),
            }
            for s in sorted(skillsh_any_or_platform_rows, key=lambda x: (x.repository, x.skill_path, x.skill_hash))
        ),
    )
    write_csv(
        skillsh_any_or_platform_csv,
        ["skill_hash", "repository", "skill_path", "source_label", "cisco_fail", "llm_fail", "platform_fail", "any_or_platform_fail"],
        (
            {
                "skill_hash": s.skill_hash,
                "repository": s.repository,
                "skill_path": s.skill_path,
                "source_label": source_label_with_platform(
                    s.skill_hash in cisco_fail_hashes,
                    s.skill_hash in llm_fail_hashes,
                    s.skill_hash in platform_fail_hashes,
                ),
                "cisco_fail": int(s.skill_hash in cisco_fail_hashes),
                "llm_fail": int(s.skill_hash in llm_fail_hashes),
                "platform_fail": int(s.skill_hash in platform_fail_hashes),
                "any_or_platform_fail": int(
                    (s.skill_hash in cisco_fail_hashes)
                    or (s.skill_hash in llm_fail_hashes)
                    or (s.skill_hash in platform_fail_hashes)
                ),
            }
            for s in sorted(skillsh_any_or_platform_rows, key=lambda x: (x.repository, x.skill_path, x.skill_hash))
        ),
    )

    strict_market_csv, strict_market_counts = build_marketplace_summary(state_db, flagged_hashes, "strict_flagged", out_dir)
    any_market_csv, any_market_counts = build_marketplace_summary(state_db, any_fail_hashes, "any_scanner_fail", out_dir)
    if args.skip_unmapped_debug:
        strict_unmapped_n = None
        any_unmapped_n = None
    else:
        strict_unmapped_n = write_unmapped_hashes(scan_db, state_db, flagged_hashes, skill_rows_all_markets, strict_unmapped_csv)
        any_unmapped_n = write_unmapped_hashes(scan_db, state_db, any_fail_hashes, any_fail_rows, any_unmapped_csv)

    print(f"Cisco scanner fail definition: HIGH/CRITICAL only")
    print(f"Strict flagged hashes (Cisco HIGH/CRITICAL ∩ LLM>{args.llm_threshold}): {len(flagged_hashes)}")
    print(f"Mapped flagged skills (all markets): {len(skill_rows_all_markets)}")
    print(f"Mapped flagged repos (all markets): {len(_count_by_repo(skill_rows_all_markets))}")
    print(f"Skill.sh platform-fail mapped skills: {len(platform_fail_skillsh_rows)}")
    print(f"Mapped flagged skills (repo_context, excluded={sorted(repo_context_excluded)}, plus Skill.sh platform-fail): {len(repo_context_rows)}")
    print(f"Mapped flagged repos (repo_context): {len(_count_by_repo(repo_context_rows))}")
    print(f"Skills input (repo_context): {skills_csv}")
    print(f"Repos input  (repo_context): {repos_csv}")
    print("Repo ranking is done in a separate step after metadata collection.")
    print(f"Skills input (all markets):  {skills_all_csv}")
    print(f"Repos input  (all markets):  {repos_all_csv}")
    print(f"Any-scanner fail mapped skills (all marketplaces): {len(any_fail_rows)}")
    print(f"Any-scanner fail repos (all marketplaces): {len(_count_by_repo(any_fail_rows))}")
    print(f"Any fail all-market skills input: {any_all_skills_csv}")
    print(f"Any fail all-market repos input:  {any_all_repos_csv}")
    print(f"Any-scanner fail hashes (union): {len(any_fail_hashes)}")
    print(
        "Existing scan fail rows (distinct source+skill_id): "
        f"{existing_stats.get('existing_fail_source_skill', 0)}"
    )
    print(
        "Existing scan fail mapped hashes (via state skill_input_metadata): "
        f"{existing_stats.get('existing_fail_mapped_hashes', 0)}"
    )
    print(f"Merged union hashes (any-scanner OR existing-scan): {len(union_with_existing_hashes)}")
    print(
        "Merged union mapped skills (all marketplaces): "
        f"{len(union_rows_all_markets)}"
    )
    print(
        "Merged union mapped skills (repo_context exclusions): "
        f"{len(union_rows_repo_context)}"
    )
    print(f"Union input skills: {union_skills_csv}")
    print(f"Union input repos:  {union_repos_csv}")
    print(f"Any-scanner fail Skill.sh mapped skills: {len(any_fail_skillsh_rows)}")
    print(f"Any-scanner fail Skill.sh repos: {len(_count_by_repo(any_fail_skillsh_rows))}")
    print(f"Skill.sh any-scanner-or-platform mapped skills: {len(skillsh_any_or_platform_rows)}")
    print(f"Any fail Skill.sh skills input: {any_skills_csv}")
    print(f"Any fail Skill.sh repos input:  {any_repos_csv}")
    print(f"Skill.sh any-scanner flag details: {skillsh_flags_csv}")
    print(f"Skill.sh any-scanner-or-platform input: {skillsh_any_or_platform_csv}")
    print(f"Marketplace summary (strict): {strict_market_csv}")
    print(f"Marketplace summary (any):    {any_market_csv}")
    print(f"Strict ClawHub hashes: {strict_market_counts.get('clawhub', 0)}")
    if args.skip_unmapped_debug:
        print("Unmapped hash diagnostics: skipped (--skip-unmapped-debug)")
    else:
        print(f"Strict unmapped hashes: {strict_unmapped_n} -> {strict_unmapped_csv}")
        print(f"Any-scanner unmapped hashes: {any_unmapped_n} -> {any_unmapped_csv}")
    print(f"Any-fail ClawHub hashes: {any_market_counts.get('clawhub', 0)}")


def _count_by_repo(rows: List[SkillRow]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in rows:
        out[r.repository] = out.get(r.repository, 0) + 1
    return out


def _dedupe_rows(rows: List[SkillRow]) -> List[SkillRow]:
    seen: Set[Tuple[str, str, str]] = set()
    out: List[SkillRow] = []
    for r in rows:
        key = (r.skill_hash, r.repository, r.skill_path)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


if __name__ == "__main__":
    main()
