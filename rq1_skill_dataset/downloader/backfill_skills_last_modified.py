#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Dict, Optional, Tuple


def load_old_lookups(old_db: Path) -> Tuple[Dict[Tuple[str, str], str], Dict[str, str]]:
    by_repo_path: Dict[Tuple[str, str], str] = {}
    by_hash: Dict[str, str] = {}
    if not old_db.exists():
        return by_repo_path, by_hash

    conn = sqlite3.connect(str(old_db))
    conn.row_factory = sqlite3.Row
    try:
        tables = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "skills" not in tables:
            return by_repo_path, by_hash

        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(skills)").fetchall()}
        if "repository" not in cols or "skill_path" not in cols:
            return by_repo_path, by_hash
        lm_col = "skill_last_modified_at" if "skill_last_modified_at" in cols else None
        hash_col = "skill_hash" if "skill_hash" in cols else None
        if not lm_col:
            return by_repo_path, by_hash

        q = f"""
            SELECT repository, skill_path, {lm_col} AS lm, {hash_col if hash_col else "NULL"} AS h
            FROM skills
        """
        for r in conn.execute(q):
            lm = str(r["lm"] or "").strip()
            if not lm:
                continue
            repo = str(r["repository"] or "").strip()
            path = str(r["skill_path"] or "").strip()
            h = str(r["h"] or "").strip()
            if repo and path:
                by_repo_path.setdefault((repo, path), lm)
            if h:
                by_hash.setdefault(h, lm)
    finally:
        conn.close()
    return by_repo_path, by_hash


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill skills.skill_last_modified_at from old sqlite DB.")
    ap.add_argument("--db", required=True, help="target state sqlite db")
    ap.add_argument("--old-db", required=True, help="old sqlite db used as source of last_modified values")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = Path(args.db)
    old_db = Path(args.old_db)
    if not db.exists():
        raise RuntimeError(f"db not found: {db}")
    if not old_db.exists():
        raise RuntimeError(f"old db not found: {old_db}")

    by_repo_path, by_hash = load_old_lookups(old_db)

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000;")
    try:
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(skills)").fetchall()}
        if "skill_last_modified_at" not in cols:
            conn.execute("ALTER TABLE skills ADD COLUMN skill_last_modified_at TEXT")

        rows = list(
            conn.execute(
                """
                SELECT id, repository, skill_path, skill_hash, skill_last_modified_at
                FROM skills
                ORDER BY id
                """
            ).fetchall()
        )

        if not args.dry_run and not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")

        updated = 0
        from_repo_path = 0
        from_hash = 0
        unresolved = 0

        for r in rows:
            cur_lm = str(r["skill_last_modified_at"] or "").strip()
            if cur_lm:
                continue
            repo = str(r["repository"] or "").strip()
            path = str(r["skill_path"] or "").strip()
            h = str(r["skill_hash"] or "").strip()

            lm: Optional[str] = by_repo_path.get((repo, path))
            source = ""
            if lm:
                source = "repo_path"
            elif h:
                lm = by_hash.get(h)
                if lm:
                    source = "hash"

            if not lm:
                unresolved += 1
                continue

            if not args.dry_run:
                conn.execute("UPDATE skills SET skill_last_modified_at = ? WHERE id = ?", (lm, int(r["id"])))
            updated += 1
            if source == "repo_path":
                from_repo_path += 1
            elif source == "hash":
                from_hash += 1

        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()

        print(f"skills rows total:        {len(rows)}")
        print(f"skills rows updated:      {updated}")
        print(f"matched by repo+path:     {from_repo_path}")
        print(f"matched by hash:          {from_hash}")
        print(f"still unresolved:         {unresolved}")
        print(f"dry-run:                 {1 if args.dry_run else 0}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
