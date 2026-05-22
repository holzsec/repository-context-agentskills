#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
import re


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def norm_token(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS repos (
            repository TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            repo TEXT NOT NULL,
            stars INTEGER,
            redirected_to TEXT,
            market_skills_count INTEGER,
            additional_skills_count INTEGER,
            source TEXT,
            input_ref TEXT,
            status TEXT NOT NULL,
            message TEXT,
            large_repo INTEGER NOT NULL DEFAULT 0,
            duplicate_hash INTEGER NOT NULL DEFAULT 0,
            repo_hash TEXT,
            repo_head_commit TEXT,
            repo_head_commit_at TEXT,
            archive_path TEXT,
            repo_size_bytes INTEGER,
            skills_size_bytes INTEGER,
            clone_started_at TEXT,
            clone_finished_at TEXT,
            processed_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS skills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repository TEXT NOT NULL,
            skill_path TEXT NOT NULL,
            skill_key TEXT,
            skill_dir TEXT,
            slug TEXT,
            repo_root INTEGER NOT NULL DEFAULT 0,
            skill_hash TEXT,
            is_duplicate INTEGER NOT NULL DEFAULT 0,
            canonical_repository TEXT,
            canonical_skill_path TEXT,
            skill_last_modified_at TEXT,
            extracted_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            UNIQUE(repository, skill_path)
        );

        CREATE INDEX IF NOT EXISTS idx_repos_hash ON repos(repo_hash);
        CREATE INDEX IF NOT EXISTS idx_skills_hash ON skills(skill_hash);

        CREATE TABLE IF NOT EXISTS marketplaces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS repo_marketplace_links (
            repository TEXT NOT NULL,
            marketplace_id INTEGER NOT NULL,
            PRIMARY KEY(repository, marketplace_id),
            FOREIGN KEY(marketplace_id) REFERENCES marketplaces(id)
        );

        CREATE TABLE IF NOT EXISTS skill_marketplace_links (
            repository TEXT NOT NULL,
            skill_path TEXT NOT NULL,
            marketplace_id INTEGER NOT NULL,
            PRIMARY KEY(repository, skill_path, marketplace_id),
            FOREIGN KEY(marketplace_id) REFERENCES marketplaces(id)
        );

        CREATE TABLE IF NOT EXISTS skill_input_metadata (
            repository TEXT NOT NULL,
            skill_path TEXT NOT NULL,
            marketplace TEXT NOT NULL,
            slug TEXT NOT NULL,
            name TEXT,
            author TEXT,
            stars INTEGER,
            installs INTEGER,
            source TEXT,
            input_ref TEXT,
            seen_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            input_skill_id TEXT,
            input_skill_name TEXT,
            is_duplicate INTEGER NOT NULL DEFAULT 0,
            canonical_source TEXT,
            canonical_skill_id TEXT,
            canonical_repository TEXT,
            canonical_skill_path TEXT,
            repo_downloaded INTEGER NOT NULL DEFAULT 0,
            skill_downloaded INTEGER NOT NULL DEFAULT 0,
            match_status TEXT,
            match_reason TEXT,
            skill_hash TEXT,
            skill_last_modified_at TEXT,
            PRIMARY KEY(repository, skill_path, marketplace, slug)
        );

        CREATE TABLE IF NOT EXISTS skills_additional (
            repository TEXT NOT NULL,
            skill_name TEXT NOT NULL,
            skill_path TEXT NOT NULL,
            skill_hash TEXT,
            skill_last_modified_at TEXT,
            detected_at TEXT NOT NULL,
            PRIMARY KEY(repository, skill_path)
        );

        CREATE TABLE IF NOT EXISTS skills_additional_marketplace_links (
            repository TEXT NOT NULL,
            skill_path TEXT NOT NULL,
            marketplace_id INTEGER NOT NULL,
            PRIMARY KEY(repository, skill_path, marketplace_id),
            FOREIGN KEY(marketplace_id) REFERENCES marketplaces(id)
        );

        CREATE INDEX IF NOT EXISTS idx_repo_marketplace_links_repo ON repo_marketplace_links(repository);
        CREATE INDEX IF NOT EXISTS idx_skill_marketplace_links_repo ON skill_marketplace_links(repository);
        CREATE INDEX IF NOT EXISTS idx_skill_marketplace_links_market ON skill_marketplace_links(marketplace_id);
        CREATE INDEX IF NOT EXISTS idx_skill_input_metadata_repo ON skill_input_metadata(repository);
        CREATE INDEX IF NOT EXISTS idx_skill_input_metadata_marketplace ON skill_input_metadata(marketplace);
        CREATE INDEX IF NOT EXISTS idx_skills_additional_repo ON skills_additional(repository);
        CREATE INDEX IF NOT EXISTS idx_skills_additional_marketplace_links_repo ON skills_additional_marketplace_links(repository);
        """
    )
    _ensure_repo_columns(conn)
    _ensure_skills_columns(conn)
    _ensure_skill_marketplace_links_schema(conn)
    _ensure_skill_input_metadata_columns(conn)
    _ensure_skills_additional_columns(conn)
    conn.commit()


def _ensure_repo_columns(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA table_info(repos)")
    cols = {str(r[1]) for r in cur.fetchall()}
    if "stars" not in cols:
        conn.execute("ALTER TABLE repos ADD COLUMN stars INTEGER")
    if "redirected_to" not in cols:
        conn.execute("ALTER TABLE repos ADD COLUMN redirected_to TEXT")
    if "market_skills_count" not in cols:
        conn.execute("ALTER TABLE repos ADD COLUMN market_skills_count INTEGER")
    if "additional_skills_count" not in cols:
        conn.execute("ALTER TABLE repos ADD COLUMN additional_skills_count INTEGER")
    if "repo_size_bytes" not in cols:
        conn.execute("ALTER TABLE repos ADD COLUMN repo_size_bytes INTEGER")
    if "skills_size_bytes" not in cols:
        conn.execute("ALTER TABLE repos ADD COLUMN skills_size_bytes INTEGER")


def _ensure_skills_columns(conn: sqlite3.Connection) -> None:
    cur2 = conn.execute("PRAGMA table_info(skills)")
    scols = {str(r[1]) for r in cur2.fetchall()}
    skills_needed = {
        "slug": "TEXT",
        "repo_root": "INTEGER NOT NULL DEFAULT 0",
        "is_duplicate": "INTEGER NOT NULL DEFAULT 0",
        "canonical_repository": "TEXT",
        "canonical_skill_path": "TEXT",
        "updated_at": "TEXT",
        "skill_hash": "TEXT",
        "skill_last_modified_at": "TEXT",
    }
    for c, typ in skills_needed.items():
        if c not in scols:
            conn.execute(f"ALTER TABLE skills ADD COLUMN {c} {typ}")


def _ensure_skill_marketplace_links_schema(conn: sqlite3.Connection) -> None:
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(skill_marketplace_links)").fetchall()}
    if not cols:
        return
    if "repository" in cols and "skill_path" in cols:
        return

    # Migrate new shape (skill_id, marketplace_id, ...) back to legacy mapping shape.
    ts = now_iso()
    conn.execute(
        """
        INSERT OR IGNORE INTO skill_input_metadata(
            repository, skill_path, marketplace, slug, name, author, stars, installs, source, input_ref, seen_at, updated_at,
            input_skill_id, input_skill_name, is_duplicate, canonical_source, canonical_skill_id,
            canonical_repository, canonical_skill_path, repo_downloaded, skill_downloaded, match_status, match_reason,
            skill_hash, skill_last_modified_at
        )
        SELECT
            s.repository,
            s.skill_path,
            lower(m.name),
            l.slug,
            COALESCE(l.input_skill_name, l.slug),
            l.author,
            NULL,
            l.installs,
            l.source,
            l.input_ref,
            COALESCE(l.seen_at, ?),
            COALESCE(l.updated_at, ?),
            l.slug,
            l.input_skill_name,
            0,
            NULL,
            NULL,
            s.canonical_repository,
            s.canonical_skill_path,
            COALESCE(l.repo_downloaded, 0),
            COALESCE(l.downloaded, 0),
            l.match_status,
            l.match_reason,
            s.skill_hash,
            s.skill_last_modified_at
        FROM skill_marketplace_links l
        JOIN skills s ON s.id = l.skill_id
        JOIN marketplaces m ON m.id = l.marketplace_id
        WHERE l.slug IS NOT NULL AND trim(l.slug) <> ''
        """,
        (ts, ts),
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_marketplace_links_new (
            repository TEXT NOT NULL,
            skill_path TEXT NOT NULL,
            marketplace_id INTEGER NOT NULL,
            PRIMARY KEY(repository, skill_path, marketplace_id),
            FOREIGN KEY(marketplace_id) REFERENCES marketplaces(id)
        )
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO skill_marketplace_links_new(repository, skill_path, marketplace_id)
        SELECT s.repository, s.skill_path, l.marketplace_id
        FROM skill_marketplace_links l
        JOIN skills s ON s.id = l.skill_id
        """
    )
    conn.execute("DROP TABLE IF EXISTS skill_marketplace_links")
    conn.execute("ALTER TABLE skill_marketplace_links_new RENAME TO skill_marketplace_links")


def _ensure_skill_input_metadata_columns(conn: sqlite3.Connection) -> None:
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(skill_input_metadata)").fetchall()}
    if not cols:
        return
    needed = {
        "input_skill_id": "TEXT",
        "input_skill_name": "TEXT",
        "is_duplicate": "INTEGER NOT NULL DEFAULT 0",
        "canonical_source": "TEXT",
        "canonical_skill_id": "TEXT",
        "canonical_repository": "TEXT",
        "canonical_skill_path": "TEXT",
        "repo_downloaded": "INTEGER NOT NULL DEFAULT 0",
        "skill_downloaded": "INTEGER NOT NULL DEFAULT 0",
        "installs": "INTEGER",
        "match_status": "TEXT",
        "match_reason": "TEXT",
        "skill_hash": "TEXT",
        "skill_last_modified_at": "TEXT",
    }
    for c, typ in needed.items():
        if c not in cols:
            conn.execute(f"ALTER TABLE skill_input_metadata ADD COLUMN {c} {typ}")


def _ensure_skills_additional_columns(conn: sqlite3.Connection) -> None:
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(skills_additional)").fetchall()}
    if not cols:
        return
    needed = {
        "skill_hash": "TEXT",
        "skill_last_modified_at": "TEXT",
        "is_duplicate": "INTEGER NOT NULL DEFAULT 0",
        "canonical_repository": "TEXT",
        "canonical_skill_path": "TEXT",
    }
    for c, typ in needed.items():
        if c not in cols:
            conn.execute(f"ALTER TABLE skills_additional ADD COLUMN {c} {typ}")


def get_repo(conn: sqlite3.Connection, repository: str) -> Optional[Dict[str, Any]]:
    cur = conn.execute("SELECT * FROM repos WHERE repository = ?", (repository,))
    row = cur.fetchone()
    return dict(row) if row else None


def repo_hash_exists(conn: sqlite3.Connection, repo_hash: str) -> bool:
    if not repo_hash:
        return False
    cur = conn.execute(
        "SELECT 1 FROM repos WHERE repo_hash = ? AND status IN ('ok', 'duplicate_hash') LIMIT 1",
        (repo_hash,),
    )
    return cur.fetchone() is not None


def add_repo_source(
    conn: sqlite3.Connection,
    *,
    repository: str,
    source: str,
    input_ref: str,
) -> None:
    # Legacy no-op: table removed from active schema.
    return


def add_repo_marketplace(
    conn: sqlite3.Connection,
    *,
    repository: str,
    marketplace: str,
) -> None:
    marketplace_id = ensure_marketplace(conn, marketplace)
    if marketplace_id is None:
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO repo_marketplace_links(repository, marketplace_id)
        VALUES (?, ?)
        """,
        (repository, marketplace_id),
    )
    conn.commit()


def add_repo(conn: sqlite3.Connection, entry: Dict[str, Any]) -> None:
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO repos(
            repository, owner, repo, stars, redirected_to, market_skills_count, additional_skills_count, source, input_ref, status, message,
            large_repo, duplicate_hash, repo_hash, repo_head_commit, repo_head_commit_at,
            archive_path, repo_size_bytes, skills_size_bytes, clone_started_at,
            clone_finished_at, processed_at, updated_at
        ) VALUES(
            :repository, :owner, :repo, :stars, :redirected_to, :market_skills_count, :additional_skills_count, :source, :input_ref, :status, :message,
            :large_repo, :duplicate_hash, :repo_hash, :repo_head_commit, :repo_head_commit_at,
            :archive_path, :repo_size_bytes, :skills_size_bytes, :clone_started_at,
            :clone_finished_at, :processed_at, :updated_at
        )
        ON CONFLICT(repository) DO UPDATE SET
            owner=excluded.owner,
            repo=excluded.repo,
            stars=excluded.stars,
            redirected_to=excluded.redirected_to,
            market_skills_count=excluded.market_skills_count,
            additional_skills_count=excluded.additional_skills_count,
            source=excluded.source,
            input_ref=excluded.input_ref,
            status=excluded.status,
            message=excluded.message,
            large_repo=excluded.large_repo,
            duplicate_hash=excluded.duplicate_hash,
            repo_hash=excluded.repo_hash,
            repo_head_commit=excluded.repo_head_commit,
            repo_head_commit_at=excluded.repo_head_commit_at,
            archive_path=excluded.archive_path,
            repo_size_bytes=excluded.repo_size_bytes,
            skills_size_bytes=excluded.skills_size_bytes,
            clone_started_at=excluded.clone_started_at,
            clone_finished_at=excluded.clone_finished_at,
            processed_at=excluded.processed_at,
            updated_at=excluded.updated_at
        """,
        {
            "repository": entry.get("repository"),
            "owner": entry.get("owner"),
            "repo": entry.get("repo"),
            "stars": entry.get("stars"),
            "redirected_to": entry.get("redirected_to"),
            "market_skills_count": entry.get("market_skills_count"),
            "additional_skills_count": entry.get("additional_skills_count"),
            "source": entry.get("source"),
            "input_ref": entry.get("input_ref"),
            "status": entry.get("status"),
            "message": entry.get("message"),
            "large_repo": int(bool(entry.get("large_repo"))),
            "duplicate_hash": int(bool(entry.get("duplicate_hash"))),
            "repo_hash": entry.get("repo_hash"),
            "repo_head_commit": entry.get("repo_head_commit"),
            "repo_head_commit_at": entry.get("repo_head_commit_at"),
            "archive_path": entry.get("archive_path"),
            "repo_size_bytes": entry.get("repo_size_bytes"),
            "skills_size_bytes": entry.get("skills_size_bytes"),
            "clone_started_at": entry.get("clone_started_at"),
            "clone_finished_at": entry.get("clone_finished_at"),
            "processed_at": entry.get("processed_at") or ts,
            "updated_at": ts,
        },
    )
    conn.commit()


def add_skill(
    conn: sqlite3.Connection,
    *,
    repository: str,
    skill_path: str,
    skill_key: str,
    skill_dir: str,
    slug: Optional[str],
    repo_root: Optional[bool] = None,
    skill_hash: Optional[str],
    is_duplicate: Optional[bool] = None,
    canonical_repository: Optional[str] = None,
    canonical_skill_path: Optional[str] = None,
    skill_last_modified_at: Optional[str],
    extracted_at: Optional[str],
) -> None:
    conn.execute(
        """
        INSERT INTO skills(
            repository, skill_path, skill_key, skill_dir, slug, repo_root, skill_hash, is_duplicate,
            canonical_repository, canonical_skill_path, skill_last_modified_at, extracted_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repository, skill_path) DO UPDATE SET
            skill_key=excluded.skill_key,
            skill_dir=excluded.skill_dir,
            slug=excluded.slug,
            repo_root=excluded.repo_root,
            skill_hash=excluded.skill_hash,
            is_duplicate=excluded.is_duplicate,
            canonical_repository=excluded.canonical_repository,
            canonical_skill_path=excluded.canonical_skill_path,
            skill_last_modified_at=excluded.skill_last_modified_at,
            extracted_at=excluded.extracted_at
        """,
        (
            repository,
            skill_path,
            skill_key,
            skill_dir,
            slug,
            int(bool(repo_root)) if repo_root is not None else 0,
            skill_hash,
            int(bool(is_duplicate)) if is_duplicate is not None else 0,
            canonical_repository,
            canonical_skill_path,
            skill_last_modified_at,
            extracted_at,
            now_iso(),
        ),
    )
    conn.commit()


def add_skill_marketplace(
    conn: sqlite3.Connection,
    *,
    repository: str,
    skill_path: str,
    marketplace: str,
) -> None:
    marketplace_id = ensure_marketplace(conn, marketplace)
    if marketplace_id is None:
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO skill_marketplace_links(repository, skill_path, marketplace_id)
        VALUES (?, ?, ?)
        """,
        (repository, skill_path, marketplace_id),
    )
    conn.commit()


def add_skill_input_metadata(
    conn: sqlite3.Connection,
    *,
    repository: str,
    skill_path: str,
    marketplace: str,
    slug: str,
    source: Optional[str],
    input_ref: Optional[str],
    name: Optional[str] = None,
    author: Optional[str] = None,
    stars: Optional[int] = None,
    verified: Optional[bool] = None,  # legacy ignored
    tags_json: Optional[str] = None,  # legacy ignored
    installs: Optional[int] = None,
    input_skill_id: Optional[str] = None,
    input_skill_name: Optional[str] = None,
    is_duplicate: Optional[bool] = None,
    canonical_source: Optional[str] = None,
    canonical_skill_id: Optional[str] = None,
    canonical_repository: Optional[str] = None,
    canonical_skill_path: Optional[str] = None,
    repo_downloaded: Optional[bool] = None,
    skill_downloaded: Optional[bool] = None,
    match_status: Optional[str] = None,
    match_reason: Optional[str] = None,
    skill_hash: Optional[str] = None,
    skill_last_modified_at: Optional[str] = None,
) -> None:
    m = (marketplace or "").strip().lower()
    s = (slug or "").strip()
    if not repository or not skill_path or not m or not s:
        return
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO skill_input_metadata(
            repository, skill_path, marketplace, slug, name, author, stars, installs, source, input_ref, seen_at, updated_at,
            input_skill_id, input_skill_name, is_duplicate, canonical_source, canonical_skill_id,
            canonical_repository, canonical_skill_path, repo_downloaded, skill_downloaded, match_status, match_reason,
            skill_hash, skill_last_modified_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repository, skill_path, marketplace, slug) DO UPDATE SET
            name=excluded.name,
            author=excluded.author,
            stars=excluded.stars,
            installs=excluded.installs,
            source=excluded.source,
            input_ref=excluded.input_ref,
            seen_at=excluded.seen_at,
            updated_at=excluded.updated_at,
            input_skill_id=excluded.input_skill_id,
            input_skill_name=excluded.input_skill_name,
            is_duplicate=excluded.is_duplicate,
            canonical_source=excluded.canonical_source,
            canonical_skill_id=excluded.canonical_skill_id,
            canonical_repository=excluded.canonical_repository,
            canonical_skill_path=excluded.canonical_skill_path,
            repo_downloaded=excluded.repo_downloaded,
            skill_downloaded=excluded.skill_downloaded,
            match_status=excluded.match_status,
            match_reason=excluded.match_reason,
            skill_hash=COALESCE(excluded.skill_hash, skill_input_metadata.skill_hash),
            skill_last_modified_at=COALESCE(excluded.skill_last_modified_at, skill_input_metadata.skill_last_modified_at)
        """,
        (
            repository,
            skill_path,
            m,
            s,
            name or input_skill_name or s,
            author,
            stars,
            installs,
            source,
            input_ref,
            ts,
            ts,
            input_skill_id or s,
            input_skill_name or name or s,
            int(bool(is_duplicate)) if is_duplicate is not None else 0,
            canonical_source,
            canonical_skill_id,
            canonical_repository,
            canonical_skill_path,
            int(bool(repo_downloaded)) if repo_downloaded is not None else 0,
            int(bool(skill_downloaded)) if skill_downloaded is not None else 0,
            match_status,
            match_reason,
            skill_hash,
            skill_last_modified_at,
        ),
    )
    conn.commit()


def add_additional_skill(
    conn: sqlite3.Connection,
    *,
    repository: str,
    skill_name: str,
    skill_path: str,
    skill_hash: Optional[str] = None,
    skill_last_modified_at: Optional[str] = None,
) -> None:
    if not repository or not skill_path:
        return
    conn.execute(
        """
        INSERT INTO skills_additional(repository, skill_name, skill_path, skill_hash, skill_last_modified_at, detected_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(repository, skill_path) DO UPDATE SET
            skill_name=excluded.skill_name,
            skill_hash=COALESCE(excluded.skill_hash, skills_additional.skill_hash),
            skill_last_modified_at=COALESCE(excluded.skill_last_modified_at, skills_additional.skill_last_modified_at),
            detected_at=excluded.detected_at
        """,
        (repository, skill_name or "", skill_path, skill_hash, skill_last_modified_at, now_iso()),
    )
    conn.commit()


def add_additional_skill_marketplace(
    conn: sqlite3.Connection,
    *,
    repository: str,
    skill_path: str,
    marketplace: str,
) -> None:
    marketplace_id = ensure_marketplace(conn, marketplace)
    if marketplace_id is None:
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO skills_additional_marketplace_links(repository, skill_path, marketplace_id)
        VALUES (?, ?, ?)
        """,
        (repository, skill_path, marketplace_id),
    )
    conn.commit()


def ensure_marketplace(conn: sqlite3.Connection, name: str) -> Optional[int]:
    n = (name or "").strip().lower()
    if not n:
        return None
    conn.execute("INSERT OR IGNORE INTO marketplaces(name) VALUES (?)", (n,))
    cur = conn.execute("SELECT id FROM marketplaces WHERE name = ?", (n,))
    row = cur.fetchone()
    return int(row[0]) if row else None
