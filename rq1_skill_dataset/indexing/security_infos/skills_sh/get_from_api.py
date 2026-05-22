#!/usr/bin/env python3
"""Load Skills.sh security audit API pages into SQLite.

This API-based collector maps audit payloads back to rows in the local `skills`
table and stores scanner result JSON for downstream analysis.
"""

import argparse
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


AUDITS_API_BASE = "https://skills.sh/api/audits"
DEFAULT_TIMEOUT = 20
DEFAULT_SLEEP = 0.1
USER_AGENT = "Mozilla/5.0 (compatible; skills-security-scraper/2.0)"


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def fetch_json(url: str, timeout: int, retries: int = 3) -> Optional[Dict[str, Any]]:
    """Fetch and decode one JSON endpoint with simple retry/backoff behavior."""

    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            with urlopen(req, timeout=timeout) as res:
                data = res.read()
                payload = json.loads(data.decode("utf-8", errors="replace"))
                if isinstance(payload, dict):
                    return payload
                return None
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_err = exc
            if attempt < retries:
                time.sleep(min(2.0, 0.4 * attempt))

    if last_err:
        print(f"[warn] fetch failed: {url} ({last_err})", file=sys.stderr)
    return None


def fetch_audits_page(page: int, timeout: int) -> Tuple[int, Optional[Dict[str, Any]]]:
    return page, fetch_json(f"{AUDITS_API_BASE}/{page}", timeout=timeout)


def ensure_column(conn: sqlite3.Connection, table: str, column: str, col_type: str) -> None:
    """Add a column to an existing table if the database is from an older schema."""

    rows = conn.execute(f"PRAGMA table_info({table});").fetchall()
    existing = {str(r[1]) for r in rows}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type};")


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_security_scanner_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_id INTEGER NOT NULL,
            scanner_slug TEXT NOT NULL,
            scanner_name TEXT,
            status TEXT NOT NULL,
            audit_url TEXT NOT NULL,
            audited_on TEXT,
            fetched_at TEXT NOT NULL,
            UNIQUE(skill_id, scanner_slug),
            FOREIGN KEY(skill_id) REFERENCES skills(id) ON DELETE CASCADE
        );
        """
    )
    ensure_column(conn, "skill_security_scanner_results", "result_json", "TEXT")
    ensure_column(conn, "skill_security_scanner_results", "scanner_json", "TEXT")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_security_socket_findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_id INTEGER NOT NULL,
            audit_url TEXT NOT NULL,
            category TEXT,
            description TEXT,
            confidence_percent INTEGER,
            severity TEXT,
            severity_percent INTEGER,
            file_path TEXT,
            audited_on TEXT,
            fetched_at TEXT NOT NULL,
            UNIQUE(skill_id, audit_url, category, severity, description),
            FOREIGN KEY(skill_id) REFERENCES skills(id) ON DELETE CASCADE
        );
        """
    )

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_security_results_skill_id "
        "ON skill_security_scanner_results(skill_id);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_security_socket_findings_skill_id "
        "ON skill_security_socket_findings(skill_id);"
    )
    conn.commit()


def load_skills_map(conn: sqlite3.Connection) -> Dict[Tuple[str, str], int]:
    """Build a `(source, skill_id) -> local database id` lookup."""

    cur = conn.execute("SELECT id, source, skill_id FROM skills")
    out: Dict[Tuple[str, str], int] = {}
    for row in cur.fetchall():
        out[(str(row[1]), str(row[2]))] = int(row[0])
    return out


def infer_status(scanner_payload: Dict[str, Any]) -> str:
    """Derive a compact status from scanner-specific result payloads."""

    result = scanner_payload.get("result")
    if isinstance(result, dict):
        for key in ("overall_risk_level", "verdict"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
    return "unknown"


def scanner_rows_from_skill(skill_obj: Dict[str, Any], page: int) -> List[Dict[str, str]]:
    """Flatten all scanner payloads for one skill object into database rows."""

    rows: List[Dict[str, str]] = []

    for scanner_key, payload in skill_obj.items():
        if scanner_key in {"rank", "source", "skillId", "name"}:
            continue
        if not isinstance(payload, dict):
            continue

        result_payload = payload.get("result")
        rows.append(
            {
                "scanner_slug": str(scanner_key).lower(),
                "scanner_name": str(payload.get("partner") or scanner_key),
                "status": infer_status(payload),
                "audit_url": f"{AUDITS_API_BASE}/{page}",
                "audited_on": str(payload.get("analyzedAt") or "") or None,
                "scanner_json": json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
                "result_json": json.dumps(result_payload, ensure_ascii=True, separators=(",", ":")),
            }
        )

    return rows


def replace_skill_scanners(
    conn: sqlite3.Connection,
    skill_db_id: int,
    rows: List[Dict[str, str]],
    fetched_at: str,
) -> int:
    conn.execute("DELETE FROM skill_security_scanner_results WHERE skill_id = ?", (skill_db_id,))

    for row in rows:
        conn.execute(
            """
            INSERT INTO skill_security_scanner_results
                (skill_id, scanner_slug, scanner_name, status, audit_url, audited_on,
                 fetched_at, scanner_json, result_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                skill_db_id,
                row["scanner_slug"],
                row["scanner_name"],
                row["status"],
                row["audit_url"],
                row["audited_on"],
                fetched_at,
                row["scanner_json"],
                row["result_json"],
            ),
        )

    conn.execute("DELETE FROM skill_security_socket_findings WHERE skill_id = ?", (skill_db_id,))
    return len(rows)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Load skills.sh security scanner data from /api/audits/{page} into SQLite."
    )
    p.add_argument("--db", default="04_03_2026.db", help="Path to SQLite DB.")
    p.add_argument("--start-page", type=int, default=1, help="First audits page id to fetch.")
    p.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Optional max number of pages to fetch.",
    )
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP timeout seconds.")
    p.add_argument("--sleep", type=float, default=DEFAULT_SLEEP, help="Sleep between pages.")
    p.add_argument(
        "--workers",
        type=int,
        default=100,
        help="Number of concurrent workers used to fetch audit pages.",
    )
    p.add_argument(
        "--commit-every",
        type=int,
        default=5,
        help="Commit after this many pages.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.workers < 1:
        print("[error] --workers must be >= 1", file=sys.stderr)
        return 2

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    ensure_tables(conn)

    skills_map = load_skills_map(conn)
    print(f"[info] loaded skills mapping: {len(skills_map)}")

    conn.execute("DELETE FROM skill_security_scanner_results")
    conn.execute("DELETE FROM skill_security_socket_findings")
    conn.commit()

    start_page = max(1, args.start_page)
    pages_processed = 0
    scanner_rows = 0
    matched_skills = 0
    unmatched_skills = 0
    started = time.time()
    next_page = start_page
    submitted_pages = 0
    stop_page: Optional[int] = None

    def can_submit(page: int) -> bool:
        if args.max_pages is not None and submitted_pages >= args.max_pages:
            return False
        if stop_page is not None and page > stop_page:
            return False
        return True

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        in_flight: Dict[Future[Tuple[int, Optional[Dict[str, Any]]]], int] = {}

        while len(in_flight) < args.workers and can_submit(next_page):
            fut = executor.submit(fetch_audits_page, next_page, args.timeout)
            in_flight[fut] = next_page
            next_page += 1
            submitted_pages += 1
            if args.sleep > 0:
                time.sleep(args.sleep)

        while in_flight:
            done, _ = wait(in_flight.keys(), return_when=FIRST_COMPLETED)
            for fut in done:
                page = in_flight.pop(fut)
                _, payload = fut.result()
                if not payload:
                    print(f"[warn] skipping page {page}: empty or invalid response")
                    continue

                skills = payload.get("skills")
                if not isinstance(skills, list) or not skills:
                    print(f"[info] page {page}: no skills in payload")
                    candidate = page - 1
                    if stop_page is None or candidate < stop_page:
                        stop_page = candidate
                    continue

                fetched_at = now_utc_iso()
                page_rows = 0

                for skill_obj in skills:
                    if not isinstance(skill_obj, dict):
                        continue

                    source = skill_obj.get("source")
                    skill_id = skill_obj.get("skillId")
                    if not isinstance(source, str) or not isinstance(skill_id, str):
                        continue

                    skill_db_id = skills_map.get((source, skill_id))
                    if skill_db_id is None:
                        unmatched_skills += 1
                        continue

                    rows = scanner_rows_from_skill(skill_obj, page)
                    page_rows += replace_skill_scanners(
                        conn=conn,
                        skill_db_id=skill_db_id,
                        rows=rows,
                        fetched_at=fetched_at,
                    )
                    matched_skills += 1

                pages_processed += 1
                scanner_rows += page_rows

                if pages_processed % args.commit_every == 0:
                    conn.commit()
                    elapsed = time.time() - started
                    rate = pages_processed / elapsed if elapsed > 0 else 0.0
                    print(
                        f"[info] pages={pages_processed} current_page={page} matched={matched_skills} "
                        f"unmatched={unmatched_skills} scanner_rows={scanner_rows} "
                        f"rate={rate:.2f} pages/s"
                    )

                has_more = bool(payload.get("hasMore"))
                if not has_more:
                    if stop_page is None or page < stop_page:
                        stop_page = page

            while len(in_flight) < args.workers and can_submit(next_page):
                fut = executor.submit(fetch_audits_page, next_page, args.timeout)
                in_flight[fut] = next_page
                next_page += 1
                submitted_pages += 1
                if args.sleep > 0:
                    time.sleep(args.sleep)

    conn.commit()
    elapsed = time.time() - started
    rate = pages_processed / elapsed if elapsed > 0 else 0.0
    print(
        f"[done] pages={pages_processed} matched={matched_skills} unmatched={unmatched_skills} "
        f"scanner_rows={scanner_rows} elapsed={elapsed:.1f}s rate={rate:.2f} pages/s"
    )
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
