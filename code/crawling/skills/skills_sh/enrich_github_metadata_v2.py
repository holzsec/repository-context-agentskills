#!/usr/bin/env python3
"""Enrich repositories from sqlite.db with GitHub metadata.

v2 reads repository names from repo_marketplace_links.repository (owner/repo)
and stores results in github_repo_metadata_v2.
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import requests

DEFAULT_DB_PATH = "sqlite.db"
GITHUB_API_BASE = "https://api.github.com"
RATE_LIMIT_MESSAGE = "API rate limit exceeded"
RATE_LIMIT_WAIT_SECONDS = 61 * 60


@dataclass(frozen=True)
class RepoMeta:
    """Repository lookup result stored in github_repo_metadata_v2."""

    repository: str
    owner: Optional[str]
    repo: Optional[str]
    exists: bool
    redirected: bool
    redirected_to: Optional[str]
    stars: Optional[int]
    http_status: Optional[int]
    error: Optional[str]


class TokenProvider:
    """Small wrapper that validates and exposes the GitHub API token."""

    def __init__(self, token: str) -> None:
        cleaned = token.strip()
        if not cleaned:
            raise ValueError("No GitHub API key provided.")
        self._token = cleaned

    def get(self) -> str:
        return self._token


def parse_owner_repo(value: str) -> Optional[Tuple[str, str]]:
    """Parse an owner/repo identifier and reject ambiguous values."""

    token = (value or "").strip().strip("/")
    if not token:
        return None
    parts = [part for part in token.split("/") if part]
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


def parse_repo_from_location(location: str) -> Optional[Tuple[str, str]]:
    if not location:
        return None
    parsed = urlparse(location)
    if parsed.netloc not in ("api.github.com", "github.com"):
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.netloc == "api.github.com" and len(parts) >= 3 and parts[0] == "repos":
        return parts[1], parts[2]
    if parsed.netloc == "github.com" and len(parts) >= 2:
        return parts[0], parts[1]
    return None


def extract_full_name(payload: object) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    full_name = payload.get("full_name")
    if not isinstance(full_name, str):
        return None
    parsed = parse_owner_repo(full_name)
    if not parsed:
        return None
    return f"{parsed[0]}/{parsed[1]}"


def extract_stars(payload: object) -> Optional[int]:
    if not isinstance(payload, dict):
        return None
    stars = payload.get("stargazers_count")
    if isinstance(stars, bool):
        return None
    if isinstance(stars, int):
        return stars
    if isinstance(stars, str):
        token = stars.strip()
        if token.isdigit():
            return int(token)
    return None


def github_request(
    session: requests.Session,
    token_provider: TokenProvider,
    url: str,
    timeout: float,
) -> requests.Response:
    token = token_provider.get()
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "skills-sh-github-enricher-v2/1.0",
    }
    return session.get(url, headers=headers, timeout=timeout, allow_redirects=False)


def fetch_repo_metadata(
    session: requests.Session,
    token_provider: TokenProvider,
    repository: str,
    timeout: float,
) -> RepoMeta:
    """Query GitHub for one repository, including redirect target handling."""

    parsed = parse_owner_repo(repository)
    if not parsed:
        return RepoMeta(
            repository=repository,
            owner=None,
            repo=None,
            exists=False,
            redirected=False,
            redirected_to=None,
            stars=None,
            http_status=None,
            error="Could not parse owner/repo from repository",
        )

    owner, repo = parsed
    queried_full_name = f"{owner}/{repo}"
    api_url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}"

    try:
        response = github_request(
            session=session,
            token_provider=token_provider,
            url=api_url,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return RepoMeta(
            repository=repository,
            owner=owner,
            repo=repo,
            exists=False,
            redirected=False,
            redirected_to=None,
            stars=None,
            http_status=None,
            error=f"request_failed: {exc}",
        )

    if response.status_code == 200:
        payload = response.json()
        return RepoMeta(
            repository=repository,
            owner=owner,
            repo=repo,
            exists=True,
            redirected=False,
            redirected_to=None,
            stars=extract_stars(payload),
            http_status=200,
            error=None,
        )

    if response.status_code in (301, 302, 307, 308):
        location = response.headers.get("Location", "")
        payload = None
        try:
            payload = response.json()
        except ValueError:
            payload = None

        payload_full_name = extract_full_name(payload)
        payload_stars = extract_stars(payload)
        redirected_to: Optional[str] = None
        redirected_stars: Optional[int] = payload_stars
        if payload_full_name and payload_full_name.lower() != queried_full_name.lower():
            redirected_to = payload_full_name

        location_payload = None
        if location:
            try:
                location_response = github_request(
                    session=session,
                    token_provider=token_provider,
                    url=location,
                    timeout=timeout,
                )
                if location_response.status_code == 200:
                    try:
                        location_payload = location_response.json()
                    except ValueError:
                        location_payload = None
            except requests.RequestException:
                location_payload = None

        location_full_name = extract_full_name(location_payload)
        location_stars = extract_stars(location_payload)
        if location_full_name and location_full_name.lower() != queried_full_name.lower():
            redirected_to = location_full_name
        if location_stars is not None:
            redirected_stars = location_stars

        if not redirected_to:
            target = parse_repo_from_location(location)
            if target:
                parsed_location_full_name = f"{target[0]}/{target[1]}"
                if parsed_location_full_name.lower() != queried_full_name.lower():
                    redirected_to = parsed_location_full_name

        if not redirected_to:
            return RepoMeta(
                repository=repository,
                owner=owner,
                repo=repo,
                exists=True,
                redirected=True,
                redirected_to=None,
                stars=redirected_stars,
                http_status=response.status_code,
                error="redirect_without_different_target",
            )

        target_owner, target_repo = redirected_to.split("/", 1)
        target_url = f"{GITHUB_API_BASE}/repos/{target_owner}/{target_repo}"
        try:
            target_response = github_request(
                session=session,
                token_provider=token_provider,
                url=target_url,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            return RepoMeta(
                repository=repository,
                owner=owner,
                repo=repo,
                exists=True,
                redirected=True,
                redirected_to=redirected_to,
                stars=redirected_stars,
                http_status=response.status_code,
                error=f"redirect_target_request_failed: {exc}",
            )

        if target_response.status_code == 200:
            payload = target_response.json()
            stars = extract_stars(payload)
            return RepoMeta(
                repository=repository,
                owner=owner,
                repo=repo,
                exists=True,
                redirected=True,
                redirected_to=redirected_to,
                stars=stars if stars is not None else redirected_stars,
                http_status=response.status_code,
                error=None,
            )

        return RepoMeta(
            repository=repository,
            owner=owner,
            repo=repo,
            exists=False,
            redirected=True,
            redirected_to=redirected_to,
            stars=None,
            http_status=target_response.status_code,
            error=f"redirect_target_http_{target_response.status_code}",
        )

    if response.status_code == 404:
        return RepoMeta(
            repository=repository,
            owner=owner,
            repo=repo,
            exists=False,
            redirected=False,
            redirected_to=None,
            stars=None,
            http_status=404,
            error=None,
        )

    error_message = None
    try:
        body = response.json()
        if isinstance(body, dict):
            message = body.get("message")
            if isinstance(message, str):
                error_message = message
    except ValueError:
        pass
    if not error_message:
        error_message = f"http_{response.status_code}"

    return RepoMeta(
        repository=repository,
        owner=owner,
        repo=repo,
        exists=False,
        redirected=False,
        redirected_to=None,
        stars=None,
        http_status=response.status_code,
        error=error_message,
    )


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS github_repo_metadata_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repository TEXT NOT NULL UNIQUE,
            owner TEXT,
            repo TEXT,
            repo_exists INTEGER NOT NULL,
            redirected INTEGER NOT NULL,
            redirected_to TEXT,
            stars INTEGER,
            http_status INTEGER,
            error TEXT,
            checked_at TEXT NOT NULL
        )
        """
    )


def load_repositories(conn: sqlite3.Connection, limit: int, only_missing: bool) -> List[str]:
    """Load repository identifiers from repo_marketplace_links for enrichment."""

    if only_missing:
        query = """
            SELECT DISTINCT rml.repository
            FROM repo_marketplace_links rml
            LEFT JOIN github_repo_metadata_v2 g ON g.repository = rml.repository
            WHERE g.repository IS NULL
            ORDER BY rml.repository
        """
    else:
        query = "SELECT DISTINCT repository FROM repo_marketplace_links ORDER BY repository"

    if limit > 0:
        query = f"{query} LIMIT ?"
        rows = conn.execute(query, (limit,)).fetchall()
    else:
        rows = conn.execute(query).fetchall()
    return [str(row[0]).strip() for row in rows if str(row[0] or "").strip()]


def save_repo_metadata(conn: sqlite3.Connection, metas: Iterable[RepoMeta]) -> int:
    """Upsert GitHub metadata rows and return the number of rows processed."""

    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (
            meta.repository,
            meta.owner,
            meta.repo,
            1 if meta.exists else 0,
            1 if meta.redirected else 0,
            meta.redirected_to,
            meta.stars,
            meta.http_status,
            meta.error,
            now,
        )
        for meta in metas
    ]
    conn.executemany(
        """
        INSERT INTO github_repo_metadata_v2 (
            repository, owner, repo, repo_exists, redirected, redirected_to,
            stars, http_status, error, checked_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repository) DO UPDATE SET
            owner = excluded.owner,
            repo = excluded.repo,
            repo_exists = excluded.repo_exists,
            redirected = excluded.redirected,
            redirected_to = excluded.redirected_to,
            stars = excluded.stars,
            http_status = excluded.http_status,
            error = excluded.error,
            checked_at = excluded.checked_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Enrich repositories from repo_marketplace_links with GitHub metadata "
            "into github_repo_metadata_v2."
        )
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="SQLite database path.")
    parser.add_argument(
        "--token",
        required=True,
        help="Single GitHub API key.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="Request timeout in seconds.",
    )
    parser.add_argument(
        "--sleep-ms",
        type=int,
        default=120,
        help="Delay between repository lookups in milliseconds.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional max number of repos to process. 0 means all.",
    )
    only_missing_group = parser.add_mutually_exclusive_group()
    only_missing_group.add_argument(
        "--only-missing",
        dest="only_missing",
        action="store_true",
        help="Only process repos that are missing in github_repo_metadata_v2 (default).",
    )
    only_missing_group.add_argument(
        "--all",
        dest="only_missing",
        action="store_false",
        help="Process all repos, including ones already present in github_repo_metadata_v2.",
    )
    parser.set_defaults(only_missing=True)
    parser.add_argument("--verbose", action="store_true", help="Print progress.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    token_provider = TokenProvider(args.token)
    sleep_seconds = max(0, args.sleep_ms) / 1000.0

    with sqlite3.connect(args.db) as conn:
        init_db(conn)
        repositories = load_repositories(
            conn,
            limit=max(0, args.limit),
            only_missing=args.only_missing,
        )

        session = requests.Session()
        saved = 0
        batch: List[RepoMeta] = []
        for i, repository in enumerate(repositories, start=1):
            while True:
                meta = fetch_repo_metadata(
                    session=session,
                    token_provider=token_provider,
                    repository=repository,
                    timeout=args.timeout,
                )
                if RATE_LIMIT_MESSAGE.lower() in (meta.error or "").lower():
                    if args.verbose:
                        print(
                            f"[{i}/{len(repositories)}] {repository} | rate limit hit, "
                            f"waiting {RATE_LIMIT_WAIT_SECONDS} seconds before retry"
                        )
                    time.sleep(RATE_LIMIT_WAIT_SECONDS)
                    continue
                break

            batch.append(meta)

            if len(batch) >= 100:
                saved += save_repo_metadata(conn, batch)
                batch = []

            if args.verbose:
                print(
                    f"[{i}/{len(repositories)}] {repository} | exists={meta.exists} "
                    f"| redirected={meta.redirected} | stars={meta.stars}"
                )

            if sleep_seconds > 0 and i < len(repositories):
                time.sleep(sleep_seconds)

        if batch:
            saved += save_repo_metadata(conn, batch)

    print(f"Saved/updated {saved} rows in github_repo_metadata_v2")


if __name__ == "__main__":
    main()
