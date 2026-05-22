#!/usr/bin/env python3
import argparse
import re
import sqlite3
from pathlib import Path

HASH_RE = re.compile(r"[0-9a-fA-F]{64}")


def load_unique_hashes(unique_skills_path: Path) -> set[str]:
    hashes: set[str] = set()
    with unique_skills_path.open("r", encoding="utf-8") as f:
        for line in f:
            match = HASH_RE.search(line)
            if match:
                hashes.add(match.group(0).lower())
    return hashes


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Create analyzed_skills table in full_skill_marketplace.db and populate it "
            "with rows from skills_additional_flat for all clawhub entries plus "
            "entries whose skill_hash appears in unique_skills.txt."
        )
    )
    parser.add_argument(
        "--db",
        default="full_skill_marketplace.db",
        type=Path,
        help="Path to SQLite DB (default: full_skill_marketplace.db)",
    )
    parser.add_argument(
        "--unique-skills",
        default="unique_skills.txt",
        type=Path,
        help="Path to unique_skills.txt (default: unique_skills.txt)",
    )
    args = parser.parse_args()

    if not args.db.exists():
        raise FileNotFoundError(f"DB not found: {args.db}")
    if not args.unique_skills.exists():
        raise FileNotFoundError(f"unique skills file not found: {args.unique_skills}")
    unique_hashes = load_unique_hashes(args.unique_skills)
    print(f"Loaded {len(unique_hashes)} unique hashes from {args.unique_skills}")

    conn = sqlite3.connect(args.db)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA busy_timeout=60000;")
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA temp_store=MEMORY;")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS analyzed_skills (
                repository TEXT,
                skill_hash TEXT,
                skill_name TEXT,
                skill_path TEXT,
                marketplace TEXT,
                PRIMARY KEY (repository, skill_hash, skill_name, skill_path, marketplace)
            );
            """
        )

        cur.execute("DELETE FROM analyzed_skills;")

        cur.execute(
            """
            INSERT OR IGNORE INTO analyzed_skills
            SELECT repository, LOWER(skill_hash), skill_name, skill_path, marketplace
            FROM skills_additional_flat
            WHERE marketplace = 'clawhub';
            """
        )
        clawhub_inserted = cur.rowcount if cur.rowcount != -1 else 0

        unique_inserted = 0
        if unique_hashes:
            placeholders = ",".join("?" for _ in unique_hashes)
            query = f"""
                INSERT OR IGNORE INTO analyzed_skills
                SELECT repository, LOWER(skill_hash), skill_name, skill_path, marketplace
                FROM skills_additional_flat
                WHERE marketplace <> 'clawhub'
                  AND LOWER(skill_hash) IN ({placeholders});
            """
            cur.execute(query, tuple(unique_hashes))
            unique_inserted = cur.rowcount if cur.rowcount != -1 else 0

        cur.execute("SELECT COUNT(*) FROM analyzed_skills;")
        total_rows = cur.fetchone()[0]

        conn.commit()

        print(f"Inserted clawhub rows: {clawhub_inserted}")
        print(f"Inserted hash-matched rows: {unique_inserted}")
        print(f"Total rows in analyzed_skills: {total_rows}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
