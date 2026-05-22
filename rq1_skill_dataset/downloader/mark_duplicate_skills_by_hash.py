#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Dict, List, Tuple


def ensure_cols(conn: sqlite3.Connection) -> None:
    s_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(skills)").fetchall()}
    if "is_duplicate" not in s_cols:
        conn.execute("ALTER TABLE skills ADD COLUMN is_duplicate INTEGER NOT NULL DEFAULT 0")
    if "canonical_repository" not in s_cols:
        conn.execute("ALTER TABLE skills ADD COLUMN canonical_repository TEXT")
    if "canonical_skill_path" not in s_cols:
        conn.execute("ALTER TABLE skills ADD COLUMN canonical_skill_path TEXT")

    sa_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(skills_additional)").fetchall()}
    if "is_duplicate" not in sa_cols:
        conn.execute("ALTER TABLE skills_additional ADD COLUMN is_duplicate INTEGER NOT NULL DEFAULT 0")
    if "canonical_repository" not in sa_cols:
        conn.execute("ALTER TABLE skills_additional ADD COLUMN canonical_repository TEXT")
    if "canonical_skill_path" not in sa_cols:
        conn.execute("ALTER TABLE skills_additional ADD COLUMN canonical_skill_path TEXT")

    sim_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(skill_input_metadata)").fetchall()}
    if "is_duplicate" not in sim_cols:
        conn.execute("ALTER TABLE skill_input_metadata ADD COLUMN is_duplicate INTEGER NOT NULL DEFAULT 0")
    if "canonical_repository" not in sim_cols:
        conn.execute("ALTER TABLE skill_input_metadata ADD COLUMN canonical_repository TEXT")
    if "canonical_skill_path" not in sim_cols:
        conn.execute("ALTER TABLE skill_input_metadata ADD COLUMN canonical_skill_path TEXT")


def main() -> None:
    ap = argparse.ArgumentParser(description="Mark duplicate skills by identical skill_hash across skills/additional/input_metadata.")
    ap.add_argument("--db", required=True, help="state sqlite db path")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        raise RuntimeError(f"db not found: {db}")

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000;")
    try:
        ensure_cols(conn)

        # Collect all hash-carrying rows from both skill tables.
        rows = list(
            conn.execute(
                """
                SELECT 'skills' AS src, id AS row_id, repository, skill_path, skill_hash
                FROM skills
                WHERE skill_hash IS NOT NULL AND trim(skill_hash) <> ''
                UNION ALL
                SELECT 'skills_additional' AS src, NULL AS row_id, repository, skill_path, skill_hash
                FROM skills_additional
                WHERE skill_hash IS NOT NULL AND trim(skill_hash) <> ''
                """
            ).fetchall()
        )

        # Deterministic canonical: prefer skills table first, then lexicographic repo/path.
        def order_key(r: sqlite3.Row) -> Tuple[int, str, str]:
            prio = 0 if str(r["src"]) == "skills" else 1
            return (prio, str(r["repository"] or "").lower(), str(r["skill_path"] or "").lower())

        by_hash: Dict[str, List[sqlite3.Row]] = {}
        for r in rows:
            by_hash.setdefault(str(r["skill_hash"]), []).append(r)
        for h in by_hash:
            by_hash[h].sort(key=order_key)

        canonical_by_hash: Dict[str, Tuple[str, str]] = {}
        for h, group in by_hash.items():
            first = group[0]
            canonical_by_hash[h] = (str(first["repository"]), str(first["skill_path"]))

        groups_with_dups = sum(1 for g in by_hash.values() if len(g) > 1)

        skills_updates = 0
        skills_dup_rows = 0
        additional_updates = 0
        additional_dup_rows = 0
        sim_updates = 0
        sim_dup_rows = 0

        if not args.dry_run and not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")

        for h, group in by_hash.items():
            canon_repo, canon_path = canonical_by_hash[h]
            for r in group:
                repo = str(r["repository"])
                path = str(r["skill_path"])
                is_dup = 0 if (repo == canon_repo and path == canon_path) else 1
                if str(r["src"]) == "skills":
                    if not args.dry_run:
                        conn.execute(
                            """
                            UPDATE skills
                            SET is_duplicate = ?, canonical_repository = ?, canonical_skill_path = ?
                            WHERE id = ?
                            """,
                            (is_dup, canon_repo, canon_path, int(r["row_id"])),
                        )
                    skills_updates += 1
                    skills_dup_rows += is_dup
                else:
                    if not args.dry_run:
                        conn.execute(
                            """
                            UPDATE skills_additional
                            SET is_duplicate = ?, canonical_repository = ?, canonical_skill_path = ?
                            WHERE repository = ? AND skill_path = ?
                            """,
                            (is_dup, canon_repo, canon_path, repo, path),
                        )
                    additional_updates += 1
                    additional_dup_rows += is_dup

        # Mark input metadata rows using same canonical map by hash.
        sim_rows = list(
            conn.execute(
                """
                SELECT repository, skill_path, marketplace, slug, skill_hash
                FROM skill_input_metadata
                WHERE skill_hash IS NOT NULL AND trim(skill_hash) <> ''
                """
            ).fetchall()
        )
        for r in sim_rows:
            h = str(r["skill_hash"])
            canon = canonical_by_hash.get(h)
            if not canon:
                # Hash exists only in input metadata; keep non-duplicate with self canonical.
                canon = (str(r["repository"]), str(r["skill_path"]))
            is_dup = 0 if (str(r["repository"]) == canon[0] and str(r["skill_path"]) == canon[1]) else 1
            if not args.dry_run:
                conn.execute(
                    """
                    UPDATE skill_input_metadata
                    SET is_duplicate = ?, canonical_repository = ?, canonical_skill_path = ?
                    WHERE repository = ? AND skill_path = ? AND marketplace = ? AND slug = ?
                    """,
                    (
                        is_dup,
                        canon[0],
                        canon[1],
                        str(r["repository"]),
                        str(r["skill_path"]),
                        str(r["marketplace"]),
                        str(r["slug"]),
                    ),
                )
            sim_updates += 1
            sim_dup_rows += is_dup

        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()

        print(f"hash rows (skills+additional):     {len(rows)}")
        print(f"hash groups:                       {len(by_hash)}")
        print(f"groups with duplicates:            {groups_with_dups}")
        print(f"skills updated:                    {skills_updates}")
        print(f"skills marked duplicate:           {skills_dup_rows}")
        print(f"skills_additional updated:         {additional_updates}")
        print(f"skills_additional marked duplicate:{additional_dup_rows}")
        print(f"skill_input_metadata updated:      {sim_updates}")
        print(f"skill_input_metadata dup:          {sim_dup_rows}")
        print(f"dry-run:                           {1 if args.dry_run else 0}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

