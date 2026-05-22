#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Collect GitHub metadata for flagged repos (cache first, API fallback)")
    ap.add_argument("--repo-manifest", default="", help="CSV from bundle_flagged_skills.py")
    ap.add_argument("--repos-input", default="", help="CSV from collect_flagged_skills_input.py")
    ap.add_argument("--ranked-input", default="", help="Optional ranked per-repo score CSV from collect_flagged_skills_input.py")
    ap.add_argument("--state-db", required=True, help="sqlite.db")
    ap.add_argument("--existing-db", default="", help="Optional existing db with repository_context_checks cache")
    ap.add_argument("--out-db", default="skillfix/output/flagged_repo_bundles/repo_metadata.db")
    ap.add_argument("--github-token", default=os.getenv("GITHUB_TOKEN", ""))
    ap.add_argument("--allow-api", action="store_true", help="If set, call GitHub API for missing cache entries")
    return ap.parse_args()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read_repos_from_manifest(path: Path) -> List[str]:
    repos: List[str] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            if "clone_ok" in row and str(row.get("clone_ok", "0")) != "1":
                continue
            repo = (row.get("repository") or "").strip()
            if repo:
                repos.append(repo)
    return sorted(set(repos))


def init_out_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS repo_metadata (
            repository TEXT PRIMARY KEY,
            fetched_at TEXT NOT NULL,
            source TEXT NOT NULL,
            gh_api_url TEXT,
            gh_api_response_json TEXT,
            gh_created_at TEXT,
            gh_updated_at TEXT,
            gh_pushed_at TEXT,
            gh_size_kb INTEGER,
            gh_stars INTEGER,
            gh_forks INTEGER,
            gh_open_issues INTEGER,
            gh_archived INTEGER,
            gh_default_branch TEXT,
            gh_error TEXT,
            state_stars INTEGER,
            state_repo_size_bytes INTEGER,
            state_skills_size_bytes INTEGER,
            state_processed_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_repo_metadata_source ON repo_metadata(source)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_repo_scores (
            skill_hash TEXT NOT NULL,
            repository TEXT NOT NULL,
            skill_path TEXT NOT NULL,
            repo_count_per_skill_hash INTEGER,
            repo_rank INTEGER,
            priority_score REAL,
            stars INTEGER,
            activity_score REAL,
            root_proximity_score REAL,
            readme_quality_proxy REAL,
            PRIMARY KEY(skill_hash, repository, skill_path)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_skill_repo_scores_hash ON skill_repo_scores(skill_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_skill_repo_scores_repo ON skill_repo_scores(repository)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_spread_summary (
            skill_hash TEXT PRIMARY KEY,
            repo_count_per_skill_hash INTEGER NOT NULL,
            avg_stars REAL,
            avg_activity_score REAL,
            avg_root_proximity_score REAL,
            avg_readme_quality_proxy REAL,
            avg_priority_score REAL,
            max_stars INTEGER,
            top1_repository TEXT
        )
        """
    )
    conn.commit()
    return conn


def _to_int(v: object) -> Optional[int]:
    try:
        return int(float(str(v)))
    except Exception:
        return None


def _to_float(v: object) -> Optional[float]:
    try:
        return float(str(v))
    except Exception:
        return None


def load_ranked_rows(path: Path) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            skill_hash = str(row.get("skill_hash") or "").strip()
            repository = str(row.get("repository") or "").strip()
            skill_path = str(row.get("skill_path") or "").strip()
            if not skill_hash or not repository or not skill_path:
                continue
            rows.append(
                {
                    "skill_hash": skill_hash,
                    "repository": repository,
                    "skill_path": skill_path,
                    "repo_count_per_skill_hash": _to_int(row.get("repo_count_per_skill_hash")),
                    "repo_rank": _to_int(row.get("repo_rank")),
                    "priority_score": _to_float(row.get("priority_score")),
                    "stars": _to_int(row.get("stars")),
                    "activity_score": _to_float(row.get("activity_score")),
                    "root_proximity_score": _to_float(row.get("root_proximity_score")),
                    "readme_quality_proxy": _to_float(row.get("readme_quality_proxy")),
                }
            )
    return rows


def upsert_skill_repo_scores(conn: sqlite3.Connection, ranked_rows: List[Dict[str, object]]) -> None:
    conn.executemany(
        """
        INSERT OR REPLACE INTO skill_repo_scores(
            skill_hash, repository, skill_path, repo_count_per_skill_hash, repo_rank,
            priority_score, stars, activity_score, root_proximity_score, readme_quality_proxy
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                r["skill_hash"],
                r["repository"],
                r["skill_path"],
                r["repo_count_per_skill_hash"],
                r["repo_rank"],
                r["priority_score"],
                r["stars"],
                r["activity_score"],
                r["root_proximity_score"],
                r["readme_quality_proxy"],
            )
            for r in ranked_rows
        ],
    )


def rebuild_skill_spread_summary(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM skill_spread_summary")
    conn.execute(
        """
        INSERT INTO skill_spread_summary(
            skill_hash, repo_count_per_skill_hash, avg_stars, avg_activity_score,
            avg_root_proximity_score, avg_readme_quality_proxy, avg_priority_score, max_stars, top1_repository
        )
        SELECT
            s.skill_hash,
            COUNT(*) AS repo_count_per_skill_hash,
            AVG(COALESCE(s.stars, 0)) AS avg_stars,
            AVG(COALESCE(s.activity_score, 0.0)) AS avg_activity_score,
            AVG(COALESCE(s.root_proximity_score, 0.0)) AS avg_root_proximity_score,
            AVG(COALESCE(s.readme_quality_proxy, 0.0)) AS avg_readme_quality_proxy,
            AVG(COALESCE(s.priority_score, 0.0)) AS avg_priority_score,
            MAX(COALESCE(s.stars, 0)) AS max_stars,
            (
                SELECT s2.repository
                FROM skill_repo_scores s2
                WHERE s2.skill_hash = s.skill_hash
                ORDER BY COALESCE(s2.repo_rank, 999999), COALESCE(s2.priority_score, -1) DESC
                LIMIT 1
            ) AS top1_repository
        FROM skill_repo_scores s
        GROUP BY s.skill_hash
        """
    )


def load_existing_cache(existing_db: Path) -> Dict[str, Dict[str, object]]:
    if not existing_db.exists():
        return {}
    conn = sqlite3.connect(str(existing_db))
    conn.row_factory = sqlite3.Row
    cache: Dict[str, Dict[str, object]] = {}
    try:
        # Prefer newest computed_at per repository.
        q = """
            SELECT t.*
            FROM repository_context_checks t
            JOIN (
              SELECT repository, MAX(computed_at) AS max_c
              FROM repository_context_checks
              GROUP BY repository
            ) x ON x.repository = t.repository AND x.max_c = t.computed_at
        """
        for row in conn.execute(q):
            cache[row["repository"]] = dict(row)
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return cache


def load_state_repo_row(state_db: Path, repository: str) -> Dict[str, object]:
    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT repository, stars, repo_size_bytes, skills_size_bytes, processed_at
            FROM repos
            WHERE repository = ?
            """,
            (repository,),
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def gh_api_repo(repository: str, token: str) -> Tuple[str, Dict[str, object], str]:
    url = f"https://api.github.com/repos/{repository}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "skillfix-metadata-collector"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return "", data, url
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        return f"HTTP {e.code}: {body[:300]}", {}, url
    except Exception as e:
        return str(e), {}, url


def upsert_repo_metadata(conn: sqlite3.Connection, row: Dict[str, object]) -> None:
    cols = [
        "repository", "fetched_at", "source", "gh_api_url", "gh_api_response_json", "gh_created_at", "gh_updated_at",
        "gh_pushed_at", "gh_size_kb", "gh_stars", "gh_forks", "gh_open_issues", "gh_archived", "gh_default_branch",
        "gh_error", "state_stars", "state_repo_size_bytes", "state_skills_size_bytes", "state_processed_at",
    ]
    placeholders = ",".join("?" for _ in cols)
    conn.execute(
        f"INSERT OR REPLACE INTO repo_metadata ({','.join(cols)}) VALUES ({placeholders})",
        tuple(row.get(c) for c in cols),
    )


def main() -> None:
    args = parse_args()
    state_db = Path(args.state_db).resolve()
    existing_db = Path(args.existing_db).resolve() if args.existing_db else None
    out_db = Path(args.out_db).resolve()
    ranked_input = Path(args.ranked_input).resolve() if args.ranked_input else None

    repos: List[str]
    if args.repos_input:
        repos = read_repos_from_manifest(Path(args.repos_input).resolve())
    elif args.repo_manifest:
        repos = read_repos_from_manifest(Path(args.repo_manifest).resolve())
    else:
        raise SystemExit("Provide either --repos-input or --repo-manifest")
    existing_cache = load_existing_cache(existing_db) if existing_db else {}

    out_conn = init_out_db(out_db)

    print(f"Repos to process: {len(repos)}")
    print(f"Existing metadata cache rows: {len(existing_cache)}")

    for idx, repository in enumerate(repos, start=1):
        state_row = load_state_repo_row(state_db, repository)

        source = "state_only"
        gh_api_url = None
        gh_api_response_json = None
        gh_created_at = None
        gh_updated_at = None
        gh_pushed_at = None
        gh_size_kb = None
        gh_stars = None
        gh_forks = None
        gh_open_issues = None
        gh_archived = None
        gh_default_branch = None
        gh_error = None

        cached = existing_cache.get(repository)
        if cached:
            source = "existing_db_cache"
            gh_api_url = cached.get("gh_api_url")
            gh_api_response_json = cached.get("gh_api_response_json")
            gh_created_at = cached.get("gh_created_at")
            gh_updated_at = cached.get("gh_updated_at")
            gh_pushed_at = cached.get("gh_pushed_at")
            gh_size_kb = cached.get("gh_size_kb")
            gh_stars = cached.get("gh_stars")
            gh_forks = cached.get("gh_forks")
            gh_open_issues = cached.get("gh_open_issues")
            gh_archived = cached.get("gh_archived")
            gh_default_branch = cached.get("gh_default_branch")
            gh_error = cached.get("gh_error")

        if (not cached) and args.allow_api:
            err, payload, api_url = gh_api_repo(repository, args.github_token)
            source = "github_api"
            gh_api_url = api_url
            gh_error = err or None
            if payload:
                gh_api_response_json = json.dumps(payload, ensure_ascii=False)
                gh_created_at = payload.get("created_at")
                gh_updated_at = payload.get("updated_at")
                gh_pushed_at = payload.get("pushed_at")
                gh_size_kb = payload.get("size")
                gh_stars = payload.get("stargazers_count")
                gh_forks = payload.get("forks_count")
                gh_open_issues = payload.get("open_issues_count")
                gh_archived = 1 if payload.get("archived") else 0
                gh_default_branch = payload.get("default_branch")

        row = {
            "repository": repository,
            "fetched_at": utc_now(),
            "source": source,
            "gh_api_url": gh_api_url,
            "gh_api_response_json": gh_api_response_json,
            "gh_created_at": gh_created_at,
            "gh_updated_at": gh_updated_at,
            "gh_pushed_at": gh_pushed_at,
            "gh_size_kb": gh_size_kb,
            "gh_stars": gh_stars,
            "gh_forks": gh_forks,
            "gh_open_issues": gh_open_issues,
            "gh_archived": gh_archived,
            "gh_default_branch": gh_default_branch,
            "gh_error": gh_error,
            "state_stars": state_row.get("stars"),
            "state_repo_size_bytes": state_row.get("repo_size_bytes"),
            "state_skills_size_bytes": state_row.get("skills_size_bytes"),
            "state_processed_at": state_row.get("processed_at"),
        }

        upsert_repo_metadata(out_conn, row)

        if idx % 100 == 0:
            out_conn.commit()
            print(f"Progress repos: {idx}/{len(repos)}")

    out_conn.commit()
    if ranked_input and ranked_input.exists():
        ranked_rows = load_ranked_rows(ranked_input)
        upsert_skill_repo_scores(out_conn, ranked_rows)
        rebuild_skill_spread_summary(out_conn)
        out_conn.commit()
        print(f"Loaded per-repo scores: {len(ranked_rows)} from {ranked_input}")
    out_conn.close()

    print(f"Done. Metadata DB: {out_db}")


if __name__ == "__main__":
    main()
