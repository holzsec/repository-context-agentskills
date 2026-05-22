#!/usr/bin/env python3
"""Generate LaTeX overview table for marketplace skill stats.

Metrics:
- Indexed: marketplace/index entries from the non-strict flat DB
- Retrieved: strict primary entries with real content hashes
- Additional: strict repository-discovered entries not listed by the marketplace
- Analyzed: distinct skill hashes from analyzed table
- New Unique: incremental distinct hash contribution left-to-right
- Owners / Repositories: from analyzed table repository field (owner/repo)
- #Total: total unique analyzed hashes across all marketplaces
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from collections import OrderedDict
from pathlib import Path

MARKET_COLUMNS: "OrderedDict[str, tuple[str, ...]]" = OrderedDict(
    {
        "ClawHub": ("clawhub",),
        "SkillsDir.": ("skillsdirectory",),
        "Skills.sh": ("skill.sh", "skill.sh_additional"),
        "GitHub": ("gharchive",),
    }
)

PRIMARY_MARKET_COLUMNS: "OrderedDict[str, tuple[str, ...]]" = OrderedDict(
    {
        "ClawHub": ("clawhub",),
        "SkillsDir.": ("skillsdirectory",),
        "Skills.sh": ("skill.sh",),
        "GitHub": ("gharchive",),
    }
)

ADDITIONAL_MARKET_COLUMNS: "OrderedDict[str, tuple[str, ...]]" = OrderedDict(
    {
        "ClawHub": tuple(),
        "SkillsDir.": tuple(),
        "Skills.sh": ("skill.sh_additional",),
        "GitHub": tuple(),
    }
)

CAPTION = (
    "Overview of collected agent skills from ClawHub~\\cite{steinberger:2026:clawhub}, "
    "SkillDirectory~\\cite{skilldirectory}, Skills.sh~\\cite{skills_sh}, and GitHub. "
    "Retrieved denotes successfully downloaded skills retained for analysis; Added denotes "
    "skills retained after cross-source deduplication. For Skills.sh, the crawl retrieved "
    "55{,}366 listed skills and 77{,}456 additional skills extracted from referenced repositories."
)

HASH_RE = re.compile(r"[0-9a-fA-F]{64}")


def load_unique_hashes(unique_skills_path: Path) -> set[str]:
    hashes: set[str] = set()
    with unique_skills_path.open("r", encoding="utf-8") as f:
        for line in f:
            match = HASH_RE.search(line)
            if match:
                hashes.add(match.group(0).lower())
    return hashes


def load_excluded_hashes(excluded_hashes_path: Path | None) -> set[str]:
    if excluded_hashes_path is None:
        return set()
    hashes: set[str] = set()
    with excluded_hashes_path.open("r", encoding="utf-8") as f:
        for line in f:
            match = HASH_RE.search(line)
            if match:
                hashes.add(match.group(0).lower())
    return hashes


def seed_hash_whitelist(conn: sqlite3.Connection, hashes: set[str]) -> None:
    conn.execute("DROP TABLE IF EXISTS temp.hash_whitelist;")
    conn.execute("CREATE TEMP TABLE hash_whitelist (hash TEXT PRIMARY KEY);")
    if hashes:
        conn.executemany(
            "INSERT OR IGNORE INTO hash_whitelist(hash) VALUES (?);",
            ((h,) for h in hashes),
        )


def existing_analyzed_table(conn: sqlite3.Connection) -> str:
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
    names = {row[0] for row in cur.fetchall()}
    if "analyzed_skills" in names:
        return "analyzed_skills"
    if "analyzed skills" in names:
        return "analyzed skills"
    raise RuntimeError("Could not find analyzed table. Expected 'analyzed_skills' or 'analyzed skills'.")


def sql_in_clause(items: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    placeholders = ", ".join("?" for _ in items)
    return f"({placeholders})", items


def get_count(conn: sqlite3.Connection, marketplaces: tuple[str, ...]) -> int | None:
    if not marketplaces:
        return None
    clause, params = sql_in_clause(marketplaces)
    query = f"""
        SELECT COUNT(*)
        FROM skills_additional_flat
        WHERE marketplace IN {clause};
    """
    return int(conn.execute(query, params).fetchone()[0])


def get_hash_set(conn: sqlite3.Connection, analyzed_table: str, marketplaces: tuple[str, ...]) -> set[str]:
    clause, params = sql_in_clause(marketplaces)
    query = f"""
        SELECT DISTINCT LOWER(skill_hash)
        FROM "{analyzed_table}"
        WHERE marketplace IN {clause}
          AND skill_hash IS NOT NULL
          AND TRIM(skill_hash) <> ''
          AND (
                marketplace = 'clawhub'
                OR EXISTS (
                    SELECT 1
                    FROM hash_whitelist w
                    WHERE w.hash = LOWER("{analyzed_table}".skill_hash)
                )
          );
    """
    return {row[0] for row in conn.execute(query, params).fetchall()}


def get_repo_owner_counts(conn: sqlite3.Connection, analyzed_table: str, marketplaces: tuple[str, ...]) -> tuple[int, int]:
    clause, params = sql_in_clause(marketplaces)
    query = f"""
        SELECT DISTINCT TRIM(LOWER(repository))
        FROM "{analyzed_table}"
        WHERE marketplace IN {clause}
          AND repository IS NOT NULL
          AND TRIM(repository) <> ''
          AND (
                marketplace = 'clawhub'
                OR EXISTS (
                    SELECT 1
                    FROM hash_whitelist w
                    WHERE w.hash = LOWER("{analyzed_table}".skill_hash)
                )
          );
    """
    repos = [row[0] for row in conn.execute(query, params).fetchall()]

    repo_count = len(repos)
    owners = set()
    for repo in repos:
        if "/" not in repo:
            continue
        owner = repo.split("/", 1)[0].strip()
        if owner:
            owners.add(owner)
    return len(owners), repo_count


def fmt_num(n: int) -> str:
    return f"{n:,}".replace(",", "{,}")


def build_table(
    indexed: dict[str, int | None],
    downloaded: dict[str, int],
    additional: dict[str, int | None],
    analyzed: dict[str, int],
    new_unique: dict[str, int],
    owners: dict[str, int | None],
    repos: dict[str, int | None],
    total_unique: int,
) -> str:
    columns = list(MARKET_COLUMNS.keys())

    def row_values(values: dict[str, int | None]) -> str:
        rendered = []
        for col in columns:
            v = values[col]
            if v is None:
                rendered.append("n/a")
            else:
                rendered.append(fmt_num(v))
        return "\n& ".join(rendered)

    lines = [
        "\\begin{table}[t]",
        "\\setlength{\\tabcolsep}{3pt}",
        f"\\caption{{{CAPTION}}}",
        "\\label{tab:clawhub_overview}",
        "\\centering",
        "\\begin{tabularx}{\\linewidth}{Xrrrr}",
        "\\toprule",
        "\\textbf{Skills Metric} & " + " & ".join(f"\\textbf{{{col}}}" for col in columns) + " \\\\",
        "\\midrule",
        "\\textbf{Indexed}",
        "& " + row_values(indexed),
        "\\\\",
        "\\textbf{Retrieved}",
        "& " + row_values(analyzed),
        "\\\\",
        "\\textbf{Added}",
        "& " + row_values(new_unique),
        "\\\\\\addlinespace",
        "\\textbf{Owners}",
        "& " + row_values(owners),
        "\\\\",
        "\\textbf{Repositories}",
        "& " + row_values(repos),
        "\\\\",
        "\\midrule",
        f"\\multicolumn{{5}}{{l}}{{\\textbf{{\\#Total}} {fmt_num(total_unique)} distinct analyzed skills after deduplication}} \\\\",
        "\\bottomrule",
        "\\end{tabularx}",
        "\\end{table}",
    ]

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate LaTeX marketplace overview table.")
    parser.add_argument("--db", type=Path, default=Path("full_skill_marketplace.db"), help="Path to SQLite DB")
    parser.add_argument(
        "--indexed-db",
        type=Path,
        default=None,
        help=(
            "Optional non-strict DB used for Indexed counts. Defaults to --db if omitted."
        ),
    )
    parser.add_argument(
        "--unique-skills",
        type=Path,
        default=Path("unique_skills.txt"),
        help="Path to unique_skills.txt used to define analyzed hashes",
    )
    parser.add_argument(
        "--exclude-hashes",
        type=Path,
        default=None,
        help="Optional file containing hashes to exclude from the analyzed hash whitelist",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional output .tex file path; prints to stdout if omitted",
    )
    args = parser.parse_args()

    if not args.db.exists():
        raise FileNotFoundError(f"Database not found: {args.db}")
    indexed_db = args.indexed_db or args.db
    if not indexed_db.exists():
        raise FileNotFoundError(f"indexed database not found: {indexed_db}")
    if not args.unique_skills.exists():
        raise FileNotFoundError(f"unique skills file not found: {args.unique_skills}")

    conn = sqlite3.connect(args.db)
    indexed_conn = sqlite3.connect(indexed_db)
    try:
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA busy_timeout=5000;")
        indexed_conn.execute("PRAGMA temp_store=MEMORY;")
        indexed_conn.execute("PRAGMA busy_timeout=5000;")
        unique_hashes = load_unique_hashes(args.unique_skills)
        excluded_hashes = load_excluded_hashes(args.exclude_hashes)
        unique_hashes -= excluded_hashes
        seed_hash_whitelist(conn, unique_hashes)

        analyzed_table = existing_analyzed_table(conn)

        indexed: dict[str, int | None] = {}
        downloaded: dict[str, int] = {}
        additional: dict[str, int | None] = {}
        analyzed_counts: dict[str, int] = {}
        owners: dict[str, int | None] = {}
        repos: dict[str, int | None] = {}
        hash_sets: dict[str, set[str]] = {}

        for label, marketplaces in MARKET_COLUMNS.items():
            indexed[label] = get_count(indexed_conn, PRIMARY_MARKET_COLUMNS[label])
            downloaded[label] = get_count(conn, PRIMARY_MARKET_COLUMNS[label]) or 0
            additional[label] = get_count(conn, ADDITIONAL_MARKET_COLUMNS[label])
            hashes = get_hash_set(conn, analyzed_table, marketplaces)
            hash_sets[label] = hashes
            analyzed_counts[label] = len(hashes)

            owner_count, repo_count = get_repo_owner_counts(conn, analyzed_table, marketplaces)
            owners[label] = None if owner_count == 0 else owner_count
            repos[label] = None if repo_count == 0 else repo_count

        seen: set[str] = set()
        new_unique: dict[str, int] = {}
        for label in MARKET_COLUMNS:
            new_hashes = hash_sets[label] - seen
            new_unique[label] = len(new_hashes)
            seen.update(hash_sets[label])

        total_unique = len(set().union(*hash_sets.values()))

        table = build_table(
            indexed=indexed,
            downloaded=downloaded,
            additional=additional,
            analyzed=analyzed_counts,
            new_unique=new_unique,
            owners=owners,
            repos=repos,
            total_unique=total_unique,
        )

        if args.out is None:
            print(table)
        else:
            args.out.write_text(table + "\n", encoding="utf-8")
            print(f"Wrote table to {args.out}")
    finally:
        conn.close()
        indexed_conn.close()


if __name__ == "__main__":
    main()
