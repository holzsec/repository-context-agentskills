#!/usr/bin/env python3
"""Crawler for skills.sh all-time skills API into SQLite."""

from __future__ import annotations

import argparse
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple
import requests

DEFAULT_API_BASE = "https://skills.sh/api/skills/all-time"
DEFAULT_DB_PATH = "skills.db"


@dataclass(frozen=True)
class Skill:
    """Normalized Skills.sh listing row ready for SQLite persistence."""

    source: str
    skill_id: str
    name: str
    installs: int
    source_url: str


def fetch_json(url: str, timeout: float = 20.0) -> dict:
    """Fetch one Skills.sh API page and return its decoded JSON payload."""

    response = requests.get(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; skills-sh-crawler/1.0; +https://skills.sh/)"
            ),
            "Accept": "application/json",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def to_int(value: object) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)

    token = str(value).strip().replace(",", "")
    if not token:
        return None
    try:
        return int(float(token))
    except ValueError:
        return None


def to_skill_id(item: dict) -> str:
    candidates = (
        item.get("skillId"),
        item.get("skillid"),
        item.get("id"),
        item.get("slug"),
    )
    for value in candidates:
        if value is None:
            continue
        token = str(value).strip()
        if token:
            return token
    return ""


def normalize_skill(item: dict, source_url: str) -> Optional[Skill]:
    """Convert a raw API item into a complete Skill row, or skip invalid data."""

    source = str(item.get("source") or "").strip()
    skill_id = to_skill_id(item)
    name = str(item.get("name") or "").strip()
    installs = to_int(item.get("installs"))

    if not source or not skill_id or not name or installs is None:
        return None

    return Skill(
        source=source,
        skill_id=skill_id,
        name=name,
        installs=installs,
        source_url=source_url,
    )


def build_api_url(api_base: str, page: int) -> str:
    return f"{api_base.rstrip('/')}/{page}"


def crawl_all_skills_api(
    api_base: str,
    start_page: int,
    sleep_seconds: float,
    max_pages: int,
    verbose: bool = False,
) -> List[Skill]:
    """Page through the all-time API and return de-duplicated skills."""

    skills: Dict[Tuple[str, str], Skill] = {}
    page = max(1, start_page)
    pages_crawled = 0

    while True:
        if max_pages > 0 and pages_crawled >= max_pages:
            break

        url = build_api_url(api_base=api_base, page=page)
        payload = fetch_json(url)

        raw_items = payload.get("skills")
        if not isinstance(raw_items, list):
            raise RuntimeError("Unexpected API response: 'skills' is missing or not a list")

        added_this_page = 0
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            skill = normalize_skill(item=item, source_url=url)
            if not skill:
                continue
            skills[(skill.source, skill.skill_id)] = skill
            added_this_page += 1

        has_more = bool(payload.get("hasMore", False))

        if verbose:
            print(
                f"crawled {url} | items={len(raw_items)} | "
                f"skills_saved={added_this_page} | total_unique={len(skills)} | "
                f"has_more={has_more}"
            )

        pages_crawled += 1
        if not has_more:
            break

        page += 1
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    return list(skills.values())


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS skills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            skill_id TEXT NOT NULL,
            name TEXT NOT NULL,
            installs INTEGER NOT NULL,
            source_url TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(source, skill_id)
        )
        """
    )


def save_skills(conn: sqlite3.Connection, skills: Iterable[Skill]) -> int:
    """Upsert normalized skill rows and return the number of rows processed."""

    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (
            skill.source,
            skill.skill_id,
            skill.name,
            skill.installs,
            skill.source_url,
            now,
        )
        for skill in skills
    ]
    conn.executemany(
        """
        INSERT INTO skills (source, skill_id, name, installs, source_url, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, skill_id) DO UPDATE SET
            name = excluded.name,
            installs = excluded.installs,
            source_url = excluded.source_url,
            updated_at = excluded.updated_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Crawl skills.sh API and save skills metadata to SQLite."
    )
    parser.add_argument(
        "--api-base",
        default=DEFAULT_API_BASE,
        help="API base URL without the trailing /{page}.",
    )
    parser.add_argument(
        "--start-page",
        type=int,
        default=1,
        help="First page number used in /api/skills/all-time/{page}.",
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Output SQLite database path.")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Optional safety cap; 0 means no limit.",
    )
    parser.add_argument(
        "--sleep-ms",
        type=int,
        default=150,
        help="Delay between page requests in milliseconds.",
    )
    parser.add_argument("--verbose", action="store_true", help="Print crawl progress.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    sleep_seconds = max(0, args.sleep_ms) / 1000.0
    skills = crawl_all_skills_api(
        api_base=args.api_base,
        start_page=max(1, args.start_page),
        sleep_seconds=sleep_seconds,
        max_pages=max(0, args.max_pages),
        verbose=args.verbose,
    )

    with sqlite3.connect(args.db) as conn:
        init_db(conn)
        count = save_skills(conn, skills)

    print(f"Saved {count} skills to {args.db}")


if __name__ == "__main__":
    main()
