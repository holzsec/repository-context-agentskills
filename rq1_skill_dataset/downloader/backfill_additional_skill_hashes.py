#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_folder_excluding_git(folder: Path) -> Optional[str]:
    if not folder.exists() or not folder.is_dir():
        return None
    h = hashlib.sha256()
    try:
        files = sorted(folder.rglob("*"))
    except OSError:
        return None
    for p in files:
        if ".git" in p.parts:
            continue
        try:
            is_file = p.is_file()
        except OSError:
            continue
        if not is_file:
            continue
        try:
            rel = str(p.relative_to(folder)).replace("\\", "/")
            with p.open("rb") as f:
                h.update(rel.encode("utf-8"))
                h.update(b"\0")
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
                h.update(b"\0")
        except OSError:
            continue
    return h.hexdigest()


def folder_last_modified_iso(folder: Path) -> Optional[str]:
    if not folder.exists() or not folder.is_dir():
        return None
    latest = None
    for p in folder.rglob("*"):
        try:
            if not p.is_file():
                continue
            m = p.stat().st_mtime
            latest = m if latest is None else max(latest, m)
        except OSError:
            continue
    if latest is None:
        return None
    return datetime.fromtimestamp(latest, tz=timezone.utc).isoformat()


def load_old_lookup(old_db: Path) -> Dict[Tuple[str, str], Tuple[Optional[str], Optional[str]]]:
    out: Dict[Tuple[str, str], Tuple[Optional[str], Optional[str]]] = {}
    if not old_db.exists():
        return out
    conn = sqlite3.connect(str(old_db))
    conn.row_factory = sqlite3.Row
    try:
        tables = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

        if "skills_additional" in tables:
            cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(skills_additional)").fetchall()}
            h_col = "skill_hash" if "skill_hash" in cols else "NULL"
            lm_col = "skill_last_modified_at" if "skill_last_modified_at" in cols else "NULL"
            q = f"SELECT repository, skill_path, {h_col} AS h, {lm_col} AS lm FROM skills_additional"
            for r in conn.execute(q):
                k = (str(r["repository"] or ""), str(r["skill_path"] or ""))
                out[k] = (str(r["h"] or "").strip() or None, str(r["lm"] or "").strip() or None)

        if "skills" in tables:
            cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(skills)").fetchall()}
            if {"repository", "skill_path"} <= cols:
                h_col = "skill_hash" if "skill_hash" in cols else "NULL"
                lm_col = "skill_last_modified_at" if "skill_last_modified_at" in cols else "NULL"
                q = f"SELECT repository, skill_path, {h_col} AS h, {lm_col} AS lm FROM skills"
                for r in conn.execute(q):
                    k = (str(r["repository"] or ""), str(r["skill_path"] or ""))
                    if k not in out:
                        out[k] = (str(r["h"] or "").strip() or None, str(r["lm"] or "").strip() or None)
    finally:
        conn.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill skills_additional.skill_hash (+ last_modified).")
    ap.add_argument("--db", required=True, help="state sqlite db")
    ap.add_argument("--root", required=True, help="download root containing skills_additional/")
    ap.add_argument("--old-db", default="", help="optional old sqlite db to copy hashes from")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = Path(args.db)
    root = Path(args.root)
    old_db = Path(args.old_db) if args.old_db else None
    if not db.exists():
        raise RuntimeError(f"db not found: {db}")
    if not root.exists():
        raise RuntimeError(f"root not found: {root}")

    old_lookup: Dict[Tuple[str, str], Tuple[Optional[str], Optional[str]]] = {}
    if old_db and old_db.exists():
        old_lookup = load_old_lookup(old_db)

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000;")
    try:
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(skills_additional)").fetchall()}
        if "skill_hash" not in cols:
            conn.execute("ALTER TABLE skills_additional ADD COLUMN skill_hash TEXT")
        if "skill_last_modified_at" not in cols:
            conn.execute("ALTER TABLE skills_additional ADD COLUMN skill_last_modified_at TEXT")

        if not args.dry_run and not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")

        rows = list(
            conn.execute(
                """
                SELECT repository, skill_path, skill_hash, skill_last_modified_at
                FROM skills_additional
                ORDER BY repository, skill_path
                """
            ).fetchall()
        )

        updated = 0
        from_old = 0
        from_fs = 0
        for r in rows:
            repo = str(r["repository"] or "")
            skill_path = str(r["skill_path"] or "").replace("\\", "/")
            cur_h = str(r["skill_hash"] or "").strip() or None
            cur_lm = str(r["skill_last_modified_at"] or "").strip() or None
            if cur_h and cur_lm:
                continue

            h = cur_h
            lm = cur_lm
            old = old_lookup.get((repo, skill_path))
            if old:
                h = h or old[0]
                lm = lm or old[1]
                if old[0] or old[1]:
                    from_old += 1

            if not h or not lm:
                folder = root / skill_path
                if not h:
                    h = hash_folder_excluding_git(folder)
                if not lm:
                    lm = folder_last_modified_iso(folder)
                if h or lm:
                    from_fs += 1

            if (h != cur_h) or (lm != cur_lm):
                updated += 1
                if not args.dry_run:
                    conn.execute(
                        """
                        UPDATE skills_additional
                        SET skill_hash = ?, skill_last_modified_at = ?
                        WHERE repository = ? AND skill_path = ?
                        """,
                        (h, lm, repo, skill_path),
                    )

        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()

        print(f"rows total:              {len(rows)}")
        print(f"rows updated:            {updated}")
        print(f"used old db lookup:      {from_old}")
        print(f"used filesystem compute: {from_fs}")
        print(f"timestamp:               {now_iso()}")
        print(f"dry-run:                 {1 if args.dry_run else 0}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

