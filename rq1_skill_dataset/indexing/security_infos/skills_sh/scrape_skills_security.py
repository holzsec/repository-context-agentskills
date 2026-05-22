#!/usr/bin/env python3
"""Scrape Skills.sh security scanner pages into SQLite.

The script reads skills from an existing local `skills` table, fetches each
public skill page, records scanner statuses, and follows Socket audit links to
extract rendered finding cards.
"""

import argparse
import html
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SKILLS_BASE = "https://skills.sh"
DEFAULT_TIMEOUT = 20
DEFAULT_SLEEP = 0.1
USER_AGENT = "Mozilla/5.0 (compatible; skills-security-scraper/1.0)"


SECURITY_LINK_RE = re.compile(
    r'<a[^>]+href="(?P<href>/[^"]+/security/(?P<slug>[^"/?#]+))"[^>]*>'
    r'.*?<span[^>]*>(?P<name>[^<]+)</span>'
    r'.*?<span[^>]*>(?P<status>Pass|Fail)</span>',
    re.IGNORECASE | re.DOTALL,
)

AUDITED_ON_RE = re.compile(
    r"Audited by.*? on(?:<!-- -->)?\s*(?P<date>[A-Za-z]{3}\s+\d{1,2},\s+\d{4})",
    re.IGNORECASE | re.DOTALL,
)

SOCKET_CARD_RE = re.compile(
    r'<div class="rounded border border-border overflow-hidden">.*?'
    r'<span[^>]*font-medium[^>]*>(?P<category>[^<]+)</span>.*?'
    r'<span[^>]*text-muted-foreground truncate[^>]*>(?P<file_path>[^<]*)</span>.*?'
    r'<span[^>]*uppercase[^>]*>(?P<severity_text>[A-Z]+)</span>.*?'
    r'<p[^>]*whitespace-pre-line[^>]*>(?P<description>.*?)</p>.*?'
    r"Confidence:\s*(?:<!-- -->)?\s*(?P<confidence>\d+)\s*(?:<!-- -->)?%.*?"
    r"Severity:\s*(?:<!-- -->)?\s*(?P<severity_percent>\d+)\s*(?:<!-- -->)?%",
    re.IGNORECASE | re.DOTALL,
)

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean_text(value: str) -> str:
    """Strip HTML tags, decode entities, and normalize whitespace."""

    text = TAG_RE.sub(" ", value)
    text = html.unescape(text)
    text = WS_RE.sub(" ", text).strip()
    return text


def fetch_html(url: str, timeout: int, retries: int = 3) -> Optional[str]:
    """Fetch a public HTML page with simple retry/backoff behavior."""

    req = Request(url, headers={"User-Agent": USER_AGENT})
    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            with urlopen(req, timeout=timeout) as res:
                data = res.read()
                return data.decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError) as exc:
            last_err = exc
            if attempt < retries:
                time.sleep(min(2.0, 0.4 * attempt))
    if last_err:
        print(f"[warn] fetch failed: {url} ({last_err})", file=sys.stderr)
    return None


def parse_scanner_links(page_html: str, skill_path: str) -> List[Dict[str, str]]:
    """Extract scanner audit links and pass/fail statuses from a skill page."""

    out: List[Dict[str, str]] = []
    seen: set[Tuple[str, str]] = set()
    for m in SECURITY_LINK_RE.finditer(page_html):
        slug = clean_text(m.group("slug")).lower()
        name = clean_text(m.group("name"))
        status_raw = clean_text(m.group("status")).lower()
        status = "pass" if status_raw == "pass" else "fail" if status_raw == "fail" else "unknown"
        href = clean_text(m.group("href"))
        if not href.startswith("/"):
            continue
        key = (slug, href)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "scanner_slug": slug,
                "scanner_name": name,
                "status": status,
                "audit_url": f"{SKILLS_BASE}{href}",
                "skill_path": skill_path,
            }
        )
    return out


def parse_audited_on(page_html: str) -> Optional[str]:
    m = AUDITED_ON_RE.search(page_html)
    if not m:
        return None
    return clean_text(m.group("date"))


def parse_socket_findings(socket_html: str) -> List[Dict[str, object]]:
    """Extract Socket finding cards from the rendered audit page HTML."""

    findings: List[Dict[str, object]] = []
    for m in SOCKET_CARD_RE.finditer(socket_html):
        category = clean_text(m.group("category"))
        file_path = clean_text(m.group("file_path"))
        severity_text = clean_text(m.group("severity_text")).upper()
        description = clean_text(m.group("description"))
        confidence = int(m.group("confidence"))
        severity_percent = int(m.group("severity_percent"))
        findings.append(
            {
                "category": category or None,
                "file_path": file_path or None,
                "severity": severity_text or None,
                "severity_percent": severity_percent,
                "confidence_percent": confidence,
                "description": description or None,
            }
        )
    return findings


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


def load_skills(conn: sqlite3.Connection, limit: Optional[int], offset: int) -> List[Tuple[int, str, str]]:
    sql = "SELECT id, source, skill_id FROM skills ORDER BY id"
    params: List[object] = []
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    elif offset:
        sql += " LIMIT -1 OFFSET ?"
        params.append(offset)
    cur = conn.execute(sql, params)
    return [(int(r[0]), str(r[1]), str(r[2])) for r in cur.fetchall()]


def upsert_scanner_rows(
    conn: sqlite3.Connection,
    skill_db_id: int,
    rows: List[Dict[str, str]],
    fetched_at: str,
    audited_on: Optional[str],
) -> None:
    conn.execute("DELETE FROM skill_security_scanner_results WHERE skill_id = ?", (skill_db_id,))
    for row in rows:
        conn.execute(
            """
            INSERT INTO skill_security_scanner_results
                (skill_id, scanner_slug, scanner_name, status, audit_url, audited_on, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                skill_db_id,
                row["scanner_slug"],
                row["scanner_name"],
                row["status"],
                row["audit_url"],
                audited_on,
                fetched_at,
            ),
        )


def upsert_socket_findings(
    conn: sqlite3.Connection,
    skill_db_id: int,
    findings: List[Dict[str, object]],
    audit_url: str,
    fetched_at: str,
    audited_on: Optional[str],
) -> None:
    conn.execute("DELETE FROM skill_security_socket_findings WHERE skill_id = ?", (skill_db_id,))
    for f in findings:
        conn.execute(
            """
            INSERT OR IGNORE INTO skill_security_socket_findings
                (skill_id, audit_url, category, description, confidence_percent, severity,
                 severity_percent, file_path, audited_on, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                skill_db_id,
                audit_url,
                f["category"],
                f["description"],
                f["confidence_percent"],
                f["severity"],
                f["severity_percent"],
                f["file_path"],
                audited_on,
                fetched_at,
            ),
        )


def scrape_one_skill(
    skill_db_id: int,
    source: str,
    skill_slug: str,
    timeout: int,
) -> Dict[str, object]:
    """Fetch one skill page and return scanner rows plus optional Socket findings."""

    skill_path = f"/{source}/{skill_slug}"
    page_url = f"{SKILLS_BASE}{skill_path}"
    html_page = fetch_html(page_url, timeout=timeout)
    fetched_at = now_utc_iso()
    if not html_page:
        return {
            "skill_db_id": skill_db_id,
            "fetched_at": fetched_at,
            "html_ok": False,
            "scanners": [],
            "audited_on": None,
            "socket_audit_url": None,
            "socket_audited_on": None,
            "findings": [],
        }

    scanners = parse_scanner_links(html_page, skill_path)
    audited_on = parse_audited_on(html_page)

    socket_rows = [s for s in scanners if s["scanner_slug"] == "socket"]
    findings: List[Dict[str, object]] = []
    socket_audit_url: Optional[str] = None
    socket_audited_on: Optional[str] = audited_on
    if socket_rows:
        socket_audit_url = socket_rows[0]["audit_url"]
        socket_html = fetch_html(socket_audit_url, timeout=timeout)
        if socket_html:
            socket_audited_on = parse_audited_on(socket_html) or audited_on
            findings = parse_socket_findings(socket_html)

    return {
        "skill_db_id": skill_db_id,
        "fetched_at": fetched_at,
        "html_ok": True,
        "scanners": scanners,
        "audited_on": audited_on,
        "socket_audit_url": socket_audit_url,
        "socket_audited_on": socket_audited_on,
        "findings": findings,
    }


def persist_one_skill(conn: sqlite3.Connection, result: Dict[str, object]) -> Tuple[int, int]:
    """Persist one scrape result and return scanner/finding row counts."""

    skill_db_id = int(result["skill_db_id"])
    fetched_at = str(result["fetched_at"])

    if not bool(result["html_ok"]):
        conn.execute("DELETE FROM skill_security_scanner_results WHERE skill_id = ?", (skill_db_id,))
        conn.execute("DELETE FROM skill_security_socket_findings WHERE skill_id = ?", (skill_db_id,))
        return (0, 0)

    scanners = result["scanners"]
    audited_on = result["audited_on"]
    upsert_scanner_rows(
        conn=conn,
        skill_db_id=skill_db_id,
        rows=scanners,  # type: ignore[arg-type]
        fetched_at=fetched_at,
        audited_on=audited_on,  # type: ignore[arg-type]
    )

    socket_audit_url = result["socket_audit_url"]
    findings = result["findings"]
    if socket_audit_url:
        upsert_socket_findings(
            conn=conn,
            skill_db_id=skill_db_id,
            findings=findings,  # type: ignore[arg-type]
            audit_url=str(socket_audit_url),
            fetched_at=fetched_at,
            audited_on=result["socket_audited_on"],  # type: ignore[arg-type]
        )
    else:
        conn.execute("DELETE FROM skill_security_socket_findings WHERE skill_id = ?", (skill_db_id,))

    return (len(scanners), len(findings))  # type: ignore[arg-type]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Scrape skills.sh security scanner status and socket finding details into SQLite."
    )
    p.add_argument("--db", default="04_03_2026.db", help="Path to SQLite DB.")
    p.add_argument("--limit", type=int, default=None, help="Optional number of skills to process.")
    p.add_argument("--offset", type=int, default=0, help="Optional offset into skills table.")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP timeout seconds.")
    p.add_argument("--sleep", type=float, default=DEFAULT_SLEEP, help="Sleep between skills.")
    p.add_argument(
        "--commit-every",
        type=int,
        default=100,
        help="Commit after this many skills.",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=100,
        help="Number of concurrent workers to fetch skills pages.",
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

    skills = load_skills(conn, limit=args.limit, offset=args.offset)
    total = len(skills)
    print(f"[info] loaded skills: {total}")

    processed = 0
    scanner_rows = 0
    finding_rows = 0
    started = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_map = {
            executor.submit(
                scrape_one_skill,
                skill_db_id=skill_db_id,
                source=source,
                skill_slug=skill_slug,
                timeout=args.timeout,
            ): (skill_db_id, source, skill_slug)
            for skill_db_id, source, skill_slug in skills
        }

        for future in as_completed(future_map):
            skill_db_id, source, skill_slug = future_map[future]
            processed += 1
            try:
                result = future.result()
                s_count, f_count = persist_one_skill(conn, result)
                scanner_rows += s_count
                finding_rows += f_count
            except Exception as exc:
                print(
                    f"[warn] skill_id={skill_db_id} source={source} skill={skill_slug} failed: {exc}",
                    file=sys.stderr,
                )

            if processed % args.commit_every == 0:
                conn.commit()
                elapsed = time.time() - started
                rate = processed / elapsed if elapsed > 0 else 0.0
                print(
                    f"[info] {processed}/{total} skills, scanner_rows={scanner_rows}, "
                    f"socket_findings={finding_rows}, rate={rate:.2f} skills/s"
                )

            if args.sleep > 0:
                time.sleep(args.sleep)

    conn.commit()
    elapsed = time.time() - started
    rate = processed / elapsed if elapsed > 0 else 0.0
    print(
        f"[done] processed={processed} scanner_rows={scanner_rows} "
        f"socket_findings={finding_rows} elapsed={elapsed:.1f}s rate={rate:.2f} skills/s"
    )
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
