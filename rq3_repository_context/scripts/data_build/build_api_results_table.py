#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Build the final repository-context score table from codebase scans and metadata."
    )
    ap.add_argument("--db", default="../data/rq3_repository_context/repo_context.db", help="Path to repo_context.db")
    ap.add_argument("--source-table", default="codebase_scan_results", help="Codebase scan result table")
    ap.add_argument("--metadata-table", default="metadata_repositories", help="Repository metadata table")
    ap.add_argument("--out-table", default="repository_context_scores", help="Output merged table")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    conn = sqlite3.connect(str(Path(args.db)))
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.executescript(
            f"""
            DROP TABLE IF EXISTS {args.out_table};
            CREATE TABLE {args.out_table} AS
            SELECT
                m.id AS metadata_row_id,
                m.loaded_at AS metadata_loaded_at,
                a.id,
                a.loaded_at,
                a.source_jsonl,
                a.source_line_no,
                a.bundle,
                a.repo_skillname,
                COALESCE(a.skill_hash, m.skill_hash) AS skill_hash,
                COALESCE(a.repository, m.repository) AS repository,
                COALESCE(a.skill_path, m.skill_path) AS skill_path,
                a.skill_repository_count,
                a.codebase_score,
                a.api_readme_exists,
                a.api_readme_exists_num,
                a.api_skill_code_exists,
                a.api_skill_code_exists_num,
                a.api_repo_code_exists,
                a.api_repo_code_exists_num,
                a.api_domain_match,
                a.api_domain_match_num,
                a.api_code_match,
                a.api_code_match_num,
                a.api_readme_match,
                a.api_readme_match_num,
                a.api_repo_maliciousness,
                a.api_repo_maliciousness_num,
                a.api_security_tooling,
                a.api_security_tooling_num,
                a.api_final_verdict,
                a.api_final_verdict_num,
                a.api_confidence,
                a.api_confidence_num,
                a.api_why,
                m.gh_created_at,
                m.gh_pushed_at,
                m.gh_size_kb,
                m.gh_stars,
                m.gh_forks,
                m.gh_open_issues,
                m.state_repo_size_bytes,
                m.repo_size_mb,
                m.repo_size_bucket,
                m.repo_age_bucket,
                m.repo_activity_bucket,
                m.stars_bucket,
                m.forks_bucket,
                m.gh_open_issues_bucket,
                m.repo_metadata_score,
                CASE
                    WHEN a.codebase_score IS NOT NULL
                    THEN ROUND(0.7 * a.codebase_score + 0.3 * m.repo_metadata_score, 2)
                    ELSE NULL
                END AS final_score
            FROM {args.metadata_table} m
            LEFT JOIN (
                SELECT *
                FROM (
                    SELECT
                        *,
                        ROW_NUMBER() OVER (
                            PARTITION BY lower(repository), lower(skill_hash)
                            ORDER BY rowid
                        ) AS rn
                    FROM {args.source_table}
                )
                WHERE rn = 1
            ) a
              ON lower(a.repository) = lower(m.repository)
             AND lower(a.skill_hash) = lower(m.skill_hash);

            CREATE INDEX idx_{args.out_table}_repo ON {args.out_table}(repository);
            CREATE INDEX idx_{args.out_table}_skill_hash ON {args.out_table}(skill_hash);
            CREATE INDEX idx_{args.out_table}_final_score ON {args.out_table}(final_score);
            """
        )
        count = conn.execute(f"SELECT COUNT(*) FROM {args.out_table}").fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    print(json.dumps({"table": args.out_table, "rows": count}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
