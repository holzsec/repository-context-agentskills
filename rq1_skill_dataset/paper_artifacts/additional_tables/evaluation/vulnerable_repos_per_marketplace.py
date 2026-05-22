#!/usr/bin/env python3
"""Summarize vulnerable repositories and affected skills per marketplace.

Inputs are a vulnerability CSV containing repository references and a full
marketplace SQLite database. The output reports affected repositories, skills,
and star statistics by normalized marketplace name.
"""

import argparse
import csv
import statistics
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

REPO_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
MANUAL_MARKETPLACE_OVERRIDES = {
    "vangongwanxiaowan/screen-creative-skills": {"skills_sh"},
    "jumbo-memory/jumbo.cli": {"gharchive"},
}


def normalize_marketplace_name(name):
    value = (name or "").strip().lower().replace("-", "_")
    if value in {"skills_sh", "skills.sh", "skill.sh", "skill.sh_additional", "skillssh"}:
        return "skills_sh"
    if value in {"skillsdirectory", "skills_directory"}:
        return "skillsdirectory"
    if value in {"gharchive", "github", "gh_archive"}:
        return "gharchive"
    if value in {"clawhub"}:
        return "ClawHub"
    return (name or "").strip()


def table_exists(conn, table_name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def chunked(values, size):
    values = list(values)
    for i in range(0, len(values), size):
        yield values[i : i + size]


def extract_repositories(raw_value):
    """Extract GitHub owner/repo tokens from an arbitrary CSV field."""

    if not raw_value:
        return set()
    return {match.group(0).lower() for match in REPO_PATTERN.finditer(str(raw_value))}


def load_vulnerable_repositories(csv_path):
    """Load unique vulnerable repositories and best observed star counts."""

    repositories = set()
    repository_stars = {}
    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "repositories" not in (reader.fieldnames or []):
            raise ValueError(f"CSV is missing required column 'repositories': {csv_path}")
        for row in reader:
            extracted_repos = extract_repositories(row.get("repositories", ""))
            repositories.update(extracted_repos)

            raw_stars = row.get("max_stars", "")
            try:
                stars = int(raw_stars)
            except (TypeError, ValueError):
                stars = None

            if stars is not None:
                for repo in extracted_repos:
                    current = repository_stars.get(repo)
                    if current is None or stars > current:
                        repository_stars[repo] = stars

    return repositories, repository_stars


def fetch_marketplace_to_repositories(db_path, repositories):
    """Map vulnerable repositories to marketplaces using available DB schemas."""

    if not repositories:
        return {}

    db_uri = f"file:{Path(db_path)}?mode=ro&immutable=1"
    conn = sqlite3.connect(db_uri, uri=True)
    try:
        if table_exists(conn, "analyzed_skills") or table_exists(conn, "skills_additional_flat"):
            rows = []
            for repo_chunk in chunked(sorted(repositories), 500):
                placeholders = ",".join(["?"] * len(repo_chunk))
                if table_exists(conn, "analyzed_skills"):
                    rows.extend(
                        conn.execute(
                            f"""
                            SELECT DISTINCT marketplace, repository
                            FROM analyzed_skills
                            WHERE lower(repository) IN ({placeholders})
                            """,
                            repo_chunk,
                        ).fetchall()
                    )
                if table_exists(conn, "skills_additional_flat"):
                    rows.extend(
                        conn.execute(
                            f"""
                            SELECT DISTINCT marketplace, repository
                            FROM skills_additional_flat
                            WHERE lower(repository) IN ({placeholders})
                            """,
                            repo_chunk,
                        ).fetchall()
                    )
        else:
            rows = []
            for repo_chunk in chunked(sorted(repositories), 500):
                placeholders = ",".join(["?"] * len(repo_chunk))
                rows.extend(
                    conn.execute(
                        f"""
                        SELECT DISTINCT
                            m.name AS marketplace,
                            l.repository
                        FROM repo_marketplace_links l
                        JOIN marketplaces m ON m.id = l.marketplace_id
                        WHERE lower(l.repository) IN ({placeholders})
                        """,
                        repo_chunk,
                    ).fetchall()
                )
    finally:
        conn.close()

    marketplace_to_repos = {}
    for marketplace, repository in rows:
        normalized = normalize_marketplace_name(marketplace)
        repo_key = str(repository or "").strip().lower()
        if not repo_key:
            continue
        marketplace_to_repos.setdefault(normalized, set()).add(repo_key)

    # Manual overrides for known repos with missing/incomplete DB marketplace linkage.
    for repo in repositories:
        repo_key = str(repo or "").strip().lower()
        forced_marketplaces = MANUAL_MARKETPLACE_OVERRIDES.get(repo_key, set())
        for marketplace in forced_marketplaces:
            marketplace_to_repos.setdefault(marketplace, set()).add(repo_key)
    return marketplace_to_repos


def fetch_vulnerable_skill_counts(full_store_db_path, repositories):
    """Count vulnerable skill records overall and per marketplace."""

    if not repositories:
        return 0, {}

    db_uri = f"file:{Path(full_store_db_path)}?mode=ro&immutable=1"
    conn = sqlite3.connect(db_uri, uri=True)
    try:
        vulnerable_skill_keys = set()
        skills_by_marketplace = defaultdict(set)

        for repo_chunk in chunked(sorted(repositories), 500):
            placeholders = ",".join(["?"] * len(repo_chunk))

            # Overall vulnerable skills (regardless of marketplace linkage).
            rows = conn.execute(
                f"""
                SELECT DISTINCT repository, skill_path
                FROM skills
                WHERE lower(repository) IN ({placeholders})
                """,
                repo_chunk,
            ).fetchall()
            for repository, skill_path in rows:
                key = (str(repository or "").strip().lower(), str(skill_path or "").strip())
                vulnerable_skill_keys.add(key)

            rows = conn.execute(
                f"""
                SELECT DISTINCT repository, skill_path
                FROM skills_additional
                WHERE lower(repository) IN ({placeholders})
                """,
                repo_chunk,
            ).fetchall()
            for repository, skill_path in rows:
                key = (str(repository or "").strip().lower(), str(skill_path or "").strip())
                vulnerable_skill_keys.add(key)

            # Vulnerable skills per marketplace via skill marketplace links.
            rows = conn.execute(
                f"""
                SELECT DISTINCT s.repository, s.skill_path, m.name
                FROM skills s
                JOIN skill_marketplace_links l
                  ON l.repository = s.repository AND l.skill_path = s.skill_path
                JOIN marketplaces m ON m.id = l.marketplace_id
                WHERE lower(s.repository) IN ({placeholders})
                """,
                repo_chunk,
            ).fetchall()
            for repository, skill_path, marketplace in rows:
                key = (str(repository or "").strip().lower(), str(skill_path or "").strip())
                normalized = normalize_marketplace_name(marketplace)
                vulnerable_skill_keys.add(key)
                skills_by_marketplace[normalized].add(key)

            rows = conn.execute(
                f"""
                SELECT DISTINCT sa.repository, sa.skill_path, m.name
                FROM skills_additional sa
                JOIN skills_additional_marketplace_links l
                  ON l.repository = sa.repository AND l.skill_path = sa.skill_path
                JOIN marketplaces m ON m.id = l.marketplace_id
                WHERE lower(sa.repository) IN ({placeholders})
                """,
                repo_chunk,
            ).fetchall()
            for repository, skill_path, marketplace in rows:
                key = (str(repository or "").strip().lower(), str(skill_path or "").strip())
                normalized = normalize_marketplace_name(marketplace)
                vulnerable_skill_keys.add(key)
                skills_by_marketplace[normalized].add(key)
    finally:
        conn.close()

    counts_by_marketplace = {marketplace: len(skill_keys) for marketplace, skill_keys in skills_by_marketplace.items()}
    return len(vulnerable_skill_keys), counts_by_marketplace


def load_skills_sh_skill_data(skills_sh_security_db_path):
    uri = f"file:{skills_sh_security_db_path}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            """
            SELECT source, skill_id, name, installs
            FROM skills
            """
        ).fetchall()
    finally:
        conn.close()

    downloads_by_repo = {}
    skills_by_repo = {}
    for source, skill_id, name, installs in rows:
        installs_value = int(installs or 0)
        source_key = str(source or "").strip().lower()
        if not source_key:
            continue
        downloads_by_repo[source_key] = downloads_by_repo.get(source_key, 0) + installs_value
        skills_by_repo.setdefault(source_key, []).append((skill_id, name, installs_value))

    for source in skills_by_repo:
        skills_by_repo[source].sort(key=lambda item: (-item[2], item[0], item[1]))

    return downloads_by_repo, skills_by_repo


def write_summary_csv(output_path, marketplace_rows):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "marketplace",
                "vulnerable_repository_count",
                "max_stars",
                "median_stars",
                "skills_sh_downloads",
                "skills_sh_median_installs",
                "vulnerable_skill_count",
            ]
        )
        for row in marketplace_rows:
            writer.writerow(
                [
                    row["marketplace"],
                    row["count"],
                    row["max_stars"],
                    row["median_stars"],
                    row["skills_sh_downloads"],
                    row["skills_sh_median_installs"],
                    row["vulnerable_skill_count"],
                ]
            )


def main():
    default_vulnerable_csv = Path("../results/all_vulnerable_paper.csv")
    default_db_path = Path("../dataset/full_skill_marketplace.db")
    default_output_path = Path("analysis_outputs/vulnerable_repositories_per_marketplace.csv")

    parser = argparse.ArgumentParser(
        description="Count vulnerable repositories per marketplace using a vulnerable CSV and marketplace DB mappings."
    )
    parser.add_argument(
        "-v",
        "--vulnerable-csv",
        type=Path,
        default=default_vulnerable_csv,
        help="Path to CSV containing vulnerable repositories in the 'repositories' column.",
    )
    parser.add_argument(
        "-d",
        "--db",
        type=Path,
        default=default_db_path,
        help="Path to SQLite DB containing repository->marketplace mapping tables.",
    )
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        default=default_output_path,
        help="Output CSV path for marketplace-level vulnerable repository counts.",
    )
    parser.add_argument(
        "--marketplaces",
        nargs="+",
        default=["skills_sh", "skillsdirectory"],
        help="Marketplace names to include in output.",
    )
    parser.add_argument(
        "--skills-sh-security-db",
        type=Path,
        default=Path("../results/skills_sh_security_info.db"),
        help="Path to skills_sh_security_info.db for computing downloads of vulnerable repos on skills_sh.",
    )
    parser.add_argument(
        "--full-store-db",
        type=Path,
        default=Path("../results/full_store_crawl.db"),
        help="Path to full_store_crawl.db for counting vulnerable skills (overall and per marketplace).",
    )
    parser.add_argument("--top", type=int, default=20, help="How many marketplace rows to print.")
    args = parser.parse_args()

    vulnerable_repositories, repository_stars = load_vulnerable_repositories(args.vulnerable_csv)
    marketplace_to_repos = fetch_marketplace_to_repositories(args.db, vulnerable_repositories)
    total_vulnerable_skills, vulnerable_skills_by_marketplace = fetch_vulnerable_skill_counts(
        args.full_store_db, vulnerable_repositories
    )
    included_marketplaces = set(args.marketplaces)

    filtered_marketplace_to_repos = {
        marketplace: repos
        for marketplace, repos in marketplace_to_repos.items()
        if marketplace in included_marketplaces
    }
    skills_sh_downloads_by_repo, skills_sh_skills_by_repo = load_skills_sh_skill_data(args.skills_sh_security_db)

    marketplace_rows = []
    for marketplace, repos in filtered_marketplace_to_repos.items():
        stars = [repository_stars[repo] for repo in repos if repo in repository_stars]
        max_stars = max(stars) if stars else ""
        median_stars = statistics.median(stars) if stars else ""
        skills_sh_downloads = ""
        skills_sh_median_installs = ""
        if marketplace == "skills_sh":
            skills_sh_downloads = sum(skills_sh_downloads_by_repo.get(repo, 0) for repo in repos)
            all_installs = []
            for repo in repos:
                all_installs.extend(installs for _, _, installs in skills_sh_skills_by_repo.get(repo, []))
            if all_installs:
                skills_sh_median_installs = statistics.median(all_installs)
        marketplace_rows.append(
            {
                "marketplace": marketplace,
                "count": len(repos),
                "max_stars": max_stars,
                "median_stars": median_stars,
                "skills_sh_downloads": skills_sh_downloads,
                "skills_sh_median_installs": skills_sh_median_installs,
                "vulnerable_skill_count": vulnerable_skills_by_marketplace.get(marketplace, 0),
            }
        )

    marketplace_rows.sort(key=lambda row: (-row["count"], row["marketplace"]))
    write_summary_csv(args.out, marketplace_rows)

    total_vulnerable = len(vulnerable_repositories)
    mapped_repositories = set().union(*marketplace_to_repos.values()) if marketplace_to_repos else set()
    mapped_count = len(mapped_repositories)
    unmapped_count = total_vulnerable - mapped_count
    unmapped_repositories = sorted(vulnerable_repositories - mapped_repositories)

    print(f"Total unique vulnerable repositories in CSV: {total_vulnerable}")
    print(f"Vulnerable repositories mapped to at least one marketplace: {mapped_count}")
    print(f"Vulnerable repositories without marketplace mapping: {unmapped_count}")
    print(f"Total vulnerable skills in full_store_crawl.db: {total_vulnerable_skills}")
    if vulnerable_skills_by_marketplace:
        print("Vulnerable skills per marketplace (full_store_crawl.db):")
        for marketplace, count in sorted(vulnerable_skills_by_marketplace.items(), key=lambda item: (-item[1], item[0])):
            print(f"  {marketplace}: {count}")
    if unmapped_repositories:
        print("Unmapped vulnerable repositories:")
        for repo in unmapped_repositories:
            star_text = repository_stars.get(repo, "NA")
            print(f"  - {repo} (max_stars={star_text})")
    print(f"Included marketplaces: {', '.join(args.marketplaces)}")
    print("\nVulnerable repository stats per included marketplace:")

    for row in marketplace_rows[: args.top]:
        print(
            f"  {row['marketplace']}: {row['count']} repos, "
            f"max_stars={row['max_stars']}, median_stars={row['median_stars']}, "
            f"skills_sh_downloads={row['skills_sh_downloads']}, "
            f"skills_sh_median_installs={row['skills_sh_median_installs']}, "
            f"vulnerable_skills={row['vulnerable_skill_count']}"
        )

    print("\nVulnerable repositories per included marketplace:")
    for marketplace in sorted(filtered_marketplace_to_repos):
        print(f"  {marketplace}:")
        for repo in sorted(filtered_marketplace_to_repos[marketplace]):
            star_text = repository_stars.get(repo, "NA")
            skills = skills_sh_skills_by_repo.get(repo, [])
            repo_median_installs = statistics.median([installs for _, _, installs in skills]) if skills else "NA"
            repo_total_downloads = skills_sh_downloads_by_repo.get(repo, "NA")
            print(
                f"    - {repo} (max_stars={star_text}, "
                f"skills_sh_total_downloads={repo_total_downloads}, "
                f"skills_sh_median_installs={repo_median_installs})"
            )
            if skills:
                for skill_id, skill_name, installs in skills:
                    print(f"      * {skill_id} ({skill_name}): installs={installs}")
            else:
                print("      * no skills_sh entry")

    print(f"\nWrote: {args.out}")


if __name__ == "__main__":
    main()
