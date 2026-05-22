#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Optional, Tuple


def parse_repo(repository: str) -> Optional[Tuple[str, str]]:
    r = (repository or "").strip().strip("/")
    if "/" not in r:
        return None
    owner, repo = r.split("/", 1)
    if not owner or not repo:
        return None
    return owner, repo


def safe_fs_name(name: str) -> str:
    n = (name or "").strip().replace("\\", "/").strip("/")
    if not n:
        return "root"
    n = re.sub(r"[^A-Za-z0-9._-]+", "-", n)
    n = re.sub(r"-{2,}", "-", n).strip("-")
    return n or "root"


def additional_rel(owner: str, repo: str, name: str) -> str:
    return f"skills_additional/{owner}/{repo}/{safe_fs_name(name)}"


def move_merge(src: Path, dst: Path) -> None:
    if src.resolve() == dst.resolve():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        shutil.copytree(src, dst, dirs_exist_ok=True)
        shutil.rmtree(src, ignore_errors=True)
    else:
        shutil.move(str(src), str(dst))


def normalize_name(repo: str, skill_name: str, skill_path: str) -> str:
    base = (skill_name or "").strip()
    if not base:
        base = Path(skill_path).name
    if base.lower() == "root":
        return repo
    return base


def main() -> None:
    ap = argparse.ArgumentParser(description="Fix additional skill names/paths and move folders to skills_additional.")
    ap.add_argument("--db", required=True, help="state sqlite db path")
    ap.add_argument("--root", required=True, help="download root path")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db_path = Path(args.db)
    root = Path(args.root)
    if not db_path.exists():
        raise RuntimeError(f"db not found: {db_path}")
    if not root.exists():
        raise RuntimeError(f"root not found: {root}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")

    fixed_rows = 0
    moved_dirs = 0
    fs_root_dirs = 0
    removed_from_skills = 0

    try:
        if not args.dry_run and not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")

        rows = list(
            conn.execute(
                """
                SELECT repository, skill_name, skill_path, detected_at
                FROM skills_additional
                ORDER BY repository, skill_path
                """
            ).fetchall()
        )

        for row in rows:
            repository = str(row["repository"] or "").strip()
            old_name = str(row["skill_name"] or "").strip()
            old_path = str(row["skill_path"] or "").strip().replace("\\", "/")
            detected_at = str(row["detected_at"] or "").strip()

            parsed = parse_repo(repository)
            if not parsed:
                continue
            owner, repo = parsed

            new_name = normalize_name(repo, old_name, old_path)
            new_path = additional_rel(owner, repo, new_name)
            needs_fix = (new_name != old_name) or (new_path != old_path) or old_path.startswith("skills/")
            if not needs_fix:
                continue

            old_abs = root / old_path
            new_abs = root / new_path
            if old_abs.exists() and old_abs.is_dir():
                moved_dirs += 1
                if not args.dry_run:
                    move_merge(old_abs, new_abs)

            if not args.dry_run:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO skills_additional(repository, skill_name, skill_path, detected_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (repository, new_name, new_path, detected_at),
                )
                conn.execute(
                    """
                    UPDATE skills_additional
                    SET skill_name = ?, detected_at = ?
                    WHERE repository = ? AND skill_path = ?
                    """,
                    (new_name, detected_at, repository, new_path),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO skills_additional_marketplace_links(repository, skill_path, marketplace_id)
                    SELECT repository, ?, marketplace_id
                    FROM skills_additional_marketplace_links
                    WHERE repository = ? AND skill_path = ?
                    """,
                    (new_path, repository, old_path),
                )
                conn.execute(
                    "DELETE FROM skills_additional_marketplace_links WHERE repository = ? AND skill_path = ?",
                    (repository, old_path),
                )
                if old_path != new_path:
                    conn.execute(
                        "DELETE FROM skills_additional WHERE repository = ? AND skill_path = ?",
                        (repository, old_path),
                    )
                if old_path.startswith("skills/"):
                    cur1 = conn.execute(
                        "DELETE FROM skills WHERE repository = ? AND skill_path = ?",
                        (repository, old_path),
                    )
                    cur2 = conn.execute(
                        "DELETE FROM skill_marketplace_links WHERE repository = ? AND skill_path = ?",
                        (repository, old_path),
                    )
                    removed_from_skills += int(cur1.rowcount or 0) + int(cur2.rowcount or 0)

            fixed_rows += 1

        # Also fix bare root folders on disk even if missing in DB rows.
        for p in sorted((root / "skills").glob("*/*/root")):
            if not p.is_dir():
                continue
            owner = p.parent.parent.name
            repo = p.parent.name
            repository = f"{owner}/{repo}"
            new_name = repo
            new_path = additional_rel(owner, repo, new_name)
            new_abs = root / new_path
            old_rel = str(p.relative_to(root)).replace("\\", "/")

            fs_root_dirs += 1
            if not args.dry_run:
                move_merge(p, new_abs)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO skills_additional(repository, skill_name, skill_path, detected_at)
                    VALUES (?, ?, ?, datetime('now'))
                    """,
                    (repository, new_name, new_path),
                )
                conn.execute(
                    "DELETE FROM skills WHERE repository = ? AND skill_path = ?",
                    (repository, old_rel),
                )
                conn.execute(
                    "DELETE FROM skill_marketplace_links WHERE repository = ? AND skill_path = ?",
                    (repository, old_rel),
                )

        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()
    finally:
        conn.close()

    print(f"fixed additional rows:   {fixed_rows}")
    print(f"moved directories:       {moved_dirs}")
    print(f"bare root dirs handled:  {fs_root_dirs}")
    print(f"removed skills entries:  {removed_from_skills}")
    print(f"dry-run:                 {1 if args.dry_run else 0}")


if __name__ == "__main__":
    main()

