#!/usr/bin/env python3
"""Crawl ClawHub security scan data into a SQLite database.

Reads all skills from pipeline.jsonl (latest record per slug), then queries
ClawHub Convex API (skills:getBySlug) to collect VirusTotal/OpenClaw scan data.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

API_URL = "https://wry-manatee-359.convex.cloud/api/query"


@dataclass
class SkillRow:
    """Latest ClawHub skill listing row loaded from pipeline.jsonl."""

    slug: str
    latest: str | None
    display_name: str | None
    summary: str | None
    created_at: int | None
    updated_at: int | None
    ts: int | None
    comments: int
    downloads: int
    installs_all_time: int
    installs_current: int
    stars: int
    versions: int
    source_line: int


def load_latest_skills(jsonl_path: Path) -> Dict[str, SkillRow]:
    """Load one latest skill row per slug from a JSONL pipeline export."""

    by_slug: Dict[str, SkillRow] = {}
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("type") != "skill":
                continue
            slug = obj.get("slug")
            if not slug:
                continue
            stats = obj.get("stats") or {}
            row = SkillRow(
                slug=slug,
                latest=obj.get("latest"),
                display_name=obj.get("displayName"),
                summary=obj.get("summary"),
                created_at=obj.get("createdAt"),
                updated_at=obj.get("updatedAt"),
                ts=obj.get("ts"),
                comments=int(stats.get("comments") or 0),
                downloads=int(stats.get("downloads") or 0),
                installs_all_time=int(stats.get("installsAllTime") or 0),
                installs_current=int(stats.get("installsCurrent") or 0),
                stars=int(stats.get("stars") or 0),
                versions=int(stats.get("versions") or 0),
                source_line=line_no,
            )
            prev = by_slug.get(slug)
            if prev is None:
                by_slug[slug] = row
                continue
            prev_key = prev.ts if prev.ts is not None else prev.source_line
            row_key = row.ts if row.ts is not None else row.source_line
            if row_key >= prev_key:
                by_slug[slug] = row
    return by_slug


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;

        CREATE TABLE IF NOT EXISTS skills (
          slug TEXT PRIMARY KEY,
          latest_version TEXT,
          display_name TEXT,
          summary TEXT,
          created_at_ms INTEGER,
          updated_at_ms INTEGER,
          ts_ms INTEGER,
          comments INTEGER NOT NULL,
          downloads INTEGER NOT NULL,
          installs_all_time INTEGER NOT NULL,
          installs_current INTEGER NOT NULL,
          stars INTEGER NOT NULL,
          versions INTEGER NOT NULL,
          source_line INTEGER NOT NULL,
          crawled_at_ms INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS security_scans (
          slug TEXT PRIMARY KEY,
          owner_handle TEXT,
          owner_user_id TEXT,
          skill_id TEXT,
          latest_version_id TEXT,
          latest_version TEXT,
          sha256hash TEXT,
          vt_status TEXT,
          vt_verdict TEXT,
          vt_source TEXT,
          vt_analysis TEXT,
          vt_checked_at_ms INTEGER,
          vt_report_url TEXT,
          openclaw_status TEXT,
          openclaw_verdict TEXT,
          openclaw_confidence TEXT,
          openclaw_summary TEXT,
          openclaw_findings TEXT,
          openclaw_guidance TEXT,
          openclaw_checked_at_ms INTEGER,
          openclaw_raw_json TEXT,
          moderation_json TEXT,
          crawl_status TEXT NOT NULL,
          error TEXT,
          crawled_at_ms INTEGER NOT NULL,
          raw_json TEXT,
          FOREIGN KEY(slug) REFERENCES skills(slug)
        );

        CREATE INDEX IF NOT EXISTS idx_security_vt_status ON security_scans(vt_status);
        CREATE INDEX IF NOT EXISTS idx_security_openclaw_verdict ON security_scans(openclaw_verdict);
        """
    )


def upsert_skills(conn: sqlite3.Connection, rows: Iterable[SkillRow], crawled_at_ms: int) -> None:
    conn.executemany(
        """
        INSERT INTO skills (
          slug, latest_version, display_name, summary, created_at_ms, updated_at_ms, ts_ms,
          comments, downloads, installs_all_time, installs_current, stars, versions,
          source_line, crawled_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(slug) DO UPDATE SET
          latest_version=excluded.latest_version,
          display_name=excluded.display_name,
          summary=excluded.summary,
          created_at_ms=excluded.created_at_ms,
          updated_at_ms=excluded.updated_at_ms,
          ts_ms=excluded.ts_ms,
          comments=excluded.comments,
          downloads=excluded.downloads,
          installs_all_time=excluded.installs_all_time,
          installs_current=excluded.installs_current,
          stars=excluded.stars,
          versions=excluded.versions,
          source_line=excluded.source_line,
          crawled_at_ms=excluded.crawled_at_ms
        """,
        [
            (
                r.slug,
                r.latest,
                r.display_name,
                r.summary,
                r.created_at,
                r.updated_at,
                r.ts,
                r.comments,
                r.downloads,
                r.installs_all_time,
                r.installs_current,
                r.stars,
                r.versions,
                r.source_line,
                crawled_at_ms,
            )
            for r in rows
        ],
    )


def existing_scanned_slugs(conn: sqlite3.Connection) -> set[str]:
    cur = conn.execute("SELECT slug FROM security_scans")
    return {row[0] for row in cur.fetchall()}


def post_convex_query(path: str, args: List[Any], timeout_s: float, retries: int) -> Dict[str, Any]:
    """POST a Convex query with bounded retries and return the decoded response."""

    payload = json.dumps(
        {
            "path": path,
            "format": "convex_encoded_json",
            "args": args,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as err:
            last_err = err
            if attempt < retries:
                time.sleep(min(1.0 + 0.5 * attempt, 3.0))
    raise RuntimeError(f"request failed after retries: {last_err}")


def crawl_slug(slug: str, timeout_s: float, retries: int) -> Dict[str, Any]:
    """Fetch and normalize VirusTotal/OpenClaw security metadata for one slug."""

    now_ms = int(time.time() * 1000)
    try:
        raw = post_convex_query("skills:getBySlug", [{"slug": slug}], timeout_s=timeout_s, retries=retries)
        if raw.get("status") != "success":
            return {
                "slug": slug,
                "crawl_status": "api_error",
                "error": f"non-success status: {raw.get('status')}",
                "crawled_at_ms": now_ms,
                "raw_json": json.dumps(raw, ensure_ascii=False),
            }

        value = raw.get("value")
        if not value:
            return {
                "slug": slug,
                "crawl_status": "not_found",
                "error": None,
                "crawled_at_ms": now_ms,
                "raw_json": json.dumps(raw, ensure_ascii=False),
            }

        latest = value.get("latestVersion") or {}
        vt = latest.get("vtAnalysis") or {}
        llm = latest.get("llmAnalysis") or {}
        sha = latest.get("sha256hash")

        return {
            "slug": slug,
            "owner_handle": (value.get("owner") or {}).get("handle"),
            "owner_user_id": (value.get("owner") or {}).get("userId"),
            "skill_id": (value.get("skill") or {}).get("_id"),
            "latest_version_id": latest.get("_id"),
            "latest_version": latest.get("version"),
            "sha256hash": sha,
            "vt_status": vt.get("status"),
            "vt_verdict": vt.get("verdict"),
            "vt_source": vt.get("source"),
            "vt_analysis": vt.get("analysis"),
            "vt_checked_at_ms": vt.get("checkedAt"),
            "vt_report_url": f"https://www.virustotal.com/gui/file/{sha}" if sha else None,
            "openclaw_status": llm.get("status"),
            "openclaw_verdict": llm.get("verdict"),
            "openclaw_confidence": llm.get("confidence"),
            "openclaw_summary": llm.get("summary"),
            "openclaw_findings": llm.get("findings"),
            "openclaw_guidance": llm.get("guidance"),
            "openclaw_checked_at_ms": llm.get("checkedAt"),
            "openclaw_raw_json": json.dumps(llm, ensure_ascii=False) if llm else None,
            "moderation_json": json.dumps(value.get("moderationInfo") or value.get("moderation") or {}, ensure_ascii=False),
            "crawl_status": "ok",
            "error": None,
            "crawled_at_ms": now_ms,
            "raw_json": json.dumps(raw, ensure_ascii=False),
        }
    except Exception as err:
        return {
            "slug": slug,
            "crawl_status": "error",
            "error": str(err),
            "crawled_at_ms": now_ms,
            "raw_json": None,
        }


def upsert_security_scan(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    """Insert or replace the security scan row for a slug."""

    conn.execute(
        """
        INSERT INTO security_scans (
          slug, owner_handle, owner_user_id, skill_id, latest_version_id, latest_version,
          sha256hash, vt_status, vt_verdict, vt_source, vt_analysis, vt_checked_at_ms, vt_report_url,
          openclaw_status, openclaw_verdict, openclaw_confidence, openclaw_summary, openclaw_findings,
          openclaw_guidance, openclaw_checked_at_ms, openclaw_raw_json, moderation_json,
          crawl_status, error, crawled_at_ms, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(slug) DO UPDATE SET
          owner_handle=excluded.owner_handle,
          owner_user_id=excluded.owner_user_id,
          skill_id=excluded.skill_id,
          latest_version_id=excluded.latest_version_id,
          latest_version=excluded.latest_version,
          sha256hash=excluded.sha256hash,
          vt_status=excluded.vt_status,
          vt_verdict=excluded.vt_verdict,
          vt_source=excluded.vt_source,
          vt_analysis=excluded.vt_analysis,
          vt_checked_at_ms=excluded.vt_checked_at_ms,
          vt_report_url=excluded.vt_report_url,
          openclaw_status=excluded.openclaw_status,
          openclaw_verdict=excluded.openclaw_verdict,
          openclaw_confidence=excluded.openclaw_confidence,
          openclaw_summary=excluded.openclaw_summary,
          openclaw_findings=excluded.openclaw_findings,
          openclaw_guidance=excluded.openclaw_guidance,
          openclaw_checked_at_ms=excluded.openclaw_checked_at_ms,
          openclaw_raw_json=excluded.openclaw_raw_json,
          moderation_json=excluded.moderation_json,
          crawl_status=excluded.crawl_status,
          error=excluded.error,
          crawled_at_ms=excluded.crawled_at_ms,
          raw_json=excluded.raw_json
        """,
        (
            row.get("slug"),
            row.get("owner_handle"),
            row.get("owner_user_id"),
            row.get("skill_id"),
            row.get("latest_version_id"),
            row.get("latest_version"),
            row.get("sha256hash"),
            row.get("vt_status"),
            row.get("vt_verdict"),
            row.get("vt_source"),
            row.get("vt_analysis"),
            row.get("vt_checked_at_ms"),
            row.get("vt_report_url"),
            row.get("openclaw_status"),
            row.get("openclaw_verdict"),
            row.get("openclaw_confidence"),
            row.get("openclaw_summary"),
            row.get("openclaw_findings"),
            row.get("openclaw_guidance"),
            row.get("openclaw_checked_at_ms"),
            row.get("openclaw_raw_json"),
            row.get("moderation_json"),
            row.get("crawl_status"),
            row.get("error"),
            row.get("crawled_at_ms"),
            row.get("raw_json"),
        ),
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Crawl ClawHub security scans into SQLite.")
    p.add_argument("--input", default="pipeline.jsonl", help="Path to pipeline.jsonl")
    p.add_argument("--db", default="clawhub_security_scans.sqlite", help="Output SQLite DB path")
    p.add_argument("--workers", type=int, default=16, help="Concurrent requests")
    p.add_argument("--timeout", type=float, default=25.0, help="HTTP timeout seconds")
    p.add_argument("--retries", type=int, default=2, help="Retries per slug")
    p.add_argument("--limit", type=int, default=0, help="Optional max number of slugs to crawl (0 = all)")
    p.add_argument("--resume", action="store_true", help="Skip slugs already present in security_scans")
    p.add_argument("--commit-every", type=int, default=200, help="Commit interval")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    db_path = Path(args.db)

    skills = load_latest_skills(input_path)
    print(f"Loaded {len(skills):,} unique skills from {input_path}")

    conn = sqlite3.connect(db_path)
    try:
        init_db(conn)
        now_ms = int(time.time() * 1000)
        upsert_skills(conn, skills.values(), now_ms)
        conn.commit()
        print(f"Upserted skills table into {db_path}")

        slugs = sorted(skills.keys())
        if args.resume:
            done = existing_scanned_slugs(conn)
            slugs = [s for s in slugs if s not in done]
            print(f"Resume mode: {len(done):,} already scanned, {len(slugs):,} remaining")

        if args.limit and args.limit > 0:
            slugs = slugs[: args.limit]
            print(f"Applying limit: {len(slugs):,} slugs")

        total = len(slugs)
        if total == 0:
            print("No slugs to crawl.")
            return

        ok = 0
        err = 0
        not_found = 0
        api_error = 0

        start = time.time()
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            futures = {
                ex.submit(crawl_slug, slug, args.timeout, args.retries): slug for slug in slugs
            }
            for i, fut in enumerate(as_completed(futures), start=1):
                row = fut.result()
                upsert_security_scan(conn, row)

                status = row.get("crawl_status")
                if status == "ok":
                    ok += 1
                elif status == "not_found":
                    not_found += 1
                elif status == "api_error":
                    api_error += 1
                else:
                    err += 1

                if i % args.commit_every == 0:
                    conn.commit()
                    elapsed = time.time() - start
                    rate = i / elapsed if elapsed > 0 else 0
                    print(
                        f"[{i:,}/{total:,}] ok={ok:,} not_found={not_found:,} api_error={api_error:,} error={err:,} rate={rate:.1f}/s"
                    )

        conn.commit()
        elapsed = time.time() - start
        print(
            f"Done. total={total:,} ok={ok:,} not_found={not_found:,} api_error={api_error:,} error={err:,} elapsed={elapsed:.1f}s"
        )
        print(f"SQLite DB written to: {db_path}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
