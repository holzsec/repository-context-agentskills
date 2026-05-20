#!/usr/bin/env python3
"""Generate LaTeX overview table for marketplace skill stats.

Metrics:
- Downloaded Skills: from skills_additional_flat
- Analyzed Skill: distinct skill hashes from analyzed table
- Unique Additional Skills: incremental distinct hash contribution left-to-right
- Skill Owners / Repositories: from analyzed table repository field (owner/repo)
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
        "Skills.sh": ("skill.sh", "skill.sh_additional"),
        "SkillsDir.": ("skillsdirectory",),
        "GitHub": ("gharchive",),
    }
)

CAPTION = (
    "Overview of agent skills distributed by ClawHub~\\cite{steinberger:2026:clawhub}, "
    "Skills.sh~\\cite{skills_sh}, SkillDirectory~\\cite{skilldirectory}, and skills indexed "
    "on GitHub. The indexed skills denote the number of skills that were accessible on the "
    "platforms. The unique skills denote the number of skills that we added from the "
    "marketplaces to our dataset after deduplication."
)

HASH_RE = re.compile(r"[0-9a-fA-F]{64}")


def load_unique_hashes(unique_skills_path: Path) -> set[str]:
    """Load the analyzed skill hash whitelist from a text file."""

    hashes: set[str] = set()
    with unique_skills_path.open("r", encoding="utf-8") as f:
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


def get_downloaded_count(conn: sqlite3.Connection, marketplaces: tuple[str, ...]) -> int:
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
    return f"{n:,}"


def build_table(
    downloaded: dict[str, int],
    analyzed: dict[str, int],
    unique_added: dict[str, int],
    owners: dict[str, int | None],
    repos: dict[str, int | None],
    total_unique: int,
    skills_sh_listed: int,
    skills_sh_additional: int,
) -> str:
    """Render the marketplace overview metrics as a LaTeX table string."""

    columns = list(MARKET_COLUMNS.keys())

    def row_values(values: dict[str, int | None], superscript_skills_sh: bool = False) -> str:
        rendered = []
        for col in columns:
            v = values[col]
            if v is None:
                rendered.append("n/a")
            else:
                cell = fmt_num(v)
                if superscript_skills_sh and col == "Skills.sh":
                    cell += "\\textsuperscript{*}"
                rendered.append(cell)
        return "\n& ".join(rendered)

    lines = [
        "\\begin{table}[t]",
        "\\setlength{\\tabcolsep}{8pt}",
        f"\\caption{{{CAPTION}}}",
        "\\label{tab:clawhub_overview}",
        "\\centering",
        "\\begin{tabularx}{\\linewidth}{Xr r r r}",
        "\\toprule",
        "\\textbf{Metric} & \\textbf{ClawHub} & \\textbf{Skills.sh} & \\textbf{SkillsDir.} & \\textbf{GitHub} \\\\",
        "\\midrule",
        "\\textbf{Downloaded Skills}",
        "& " + row_values(downloaded, superscript_skills_sh=True),
        "\\\\",
        "\\textbf{Analyzed Skill}",
        "& " + row_values(analyzed, superscript_skills_sh=True),
        "\\\\",
        "\\textbf{Unique Additional Skills}",
        "& " + row_values(unique_added, superscript_skills_sh=True),
        "\\\\\\addlinespace",
        "\\textbf{Skill Owners}",
        "& " + row_values(owners),
        "\\\\",
        "\\textbf{Repositories}",
        "& " + row_values(repos),
        "\\\\",
        "\\midrule",
        f"\\textbf{{\\#Total}} & \\multicolumn{{4}}{{l}}{{{fmt_num(total_unique)} (sum of cross-marketplace unique counts)}} \\\\",
        "\\bottomrule",
        "\\end{tabularx}",
        "\\\\",
        (
            "\\textsuperscript{*} We downloaded "
            f"{fmt_num(skills_sh_listed)} skills listed on Skills.sh. "
            "The repositories referenced by the marketplace contained an additional "
            f"{fmt_num(skills_sh_additional)} skills, which we also collected."
        ),
        "\\end{table}",
    ]

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate LaTeX marketplace overview table.")
    parser.add_argument("--db", type=Path, default=Path("full_skill_marketplace.db"), help="Path to SQLite DB")
    parser.add_argument(
        "--unique-skills",
        type=Path,
        default=Path("unique_skills.txt"),
        help="Path to unique_skills.txt used to define analyzed hashes",
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
    if not args.unique_skills.exists():
        raise FileNotFoundError(f"unique skills file not found: {args.unique_skills}")

    conn = sqlite3.connect(args.db)
    try:
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA busy_timeout=5000;")
        unique_hashes = load_unique_hashes(args.unique_skills)
        seed_hash_whitelist(conn, unique_hashes)

        analyzed_table = existing_analyzed_table(conn)

        downloaded: dict[str, int] = {}
        analyzed_counts: dict[str, int] = {}
        owners: dict[str, int | None] = {}
        repos: dict[str, int | None] = {}
        hash_sets: dict[str, set[str]] = {}

        for label, marketplaces in MARKET_COLUMNS.items():
            downloaded[label] = get_downloaded_count(conn, marketplaces)
            hashes = get_hash_set(conn, analyzed_table, marketplaces)
            hash_sets[label] = hashes
            analyzed_counts[label] = len(hashes)

            owner_count, repo_count = get_repo_owner_counts(conn, analyzed_table, marketplaces)
            owners[label] = None if owner_count == 0 else owner_count
            repos[label] = None if repo_count == 0 else repo_count

        seen: set[str] = set()
        unique_added: dict[str, int] = {}
        for label in MARKET_COLUMNS:
            new_hashes = hash_sets[label] - seen
            unique_added[label] = len(new_hashes)
            seen.update(hash_sets[label])

        total_unique = len(set().union(*hash_sets.values()))

        skills_sh_listed = get_downloaded_count(conn, ("skill.sh",))
        skills_sh_additional = get_downloaded_count(conn, ("skill.sh_additional",))

        table = build_table(
            downloaded=downloaded,
            analyzed=analyzed_counts,
            unique_added=unique_added,
            owners=owners,
            repos=repos,
            total_unique=total_unique,
            skills_sh_listed=skills_sh_listed,
            skills_sh_additional=skills_sh_additional,
        )

        if args.out is None:
            print(table)
        else:
            args.out.write_text(table + "\n", encoding="utf-8")
            print(f"Wrote table to {args.out}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
