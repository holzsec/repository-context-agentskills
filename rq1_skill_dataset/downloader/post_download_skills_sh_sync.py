#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple


def norm_token(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_repo(repo: str) -> Optional[Tuple[str, str]]:
    t = (repo or "").strip().strip("/")
    if "/" not in t:
        return None
    owner, name = t.split("/", 1)
    if not owner or not name:
        return None
    return owner, name


def ensure_repos_stars_column(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA table_info(repos)")
    cols = {str(r[1]) for r in cur.fetchall()}
    if "stars" not in cols:
        conn.execute("ALTER TABLE repos ADD COLUMN stars INTEGER")


def ensure_skill_input_metadata_tracking_columns(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA table_info(skill_input_metadata)")
    cols = {str(r[1]) for r in cur.fetchall()}
    needed = {
        "input_skill_id": "TEXT",
        "input_skill_name": "TEXT",
        "is_duplicate": "INTEGER NOT NULL DEFAULT 0",
        "canonical_source": "TEXT",
        "canonical_repository": "TEXT",
        "canonical_skill_path": "TEXT",
        "repo_downloaded": "INTEGER NOT NULL DEFAULT 0",
        "skill_downloaded": "INTEGER NOT NULL DEFAULT 0",
        "installs": "INTEGER",
        "match_status": "TEXT",
        "match_reason": "TEXT",
    }
    for c, typ in needed.items():
        if c not in cols:
            conn.execute(f"ALTER TABLE skill_input_metadata ADD COLUMN {c} {typ}")


def ensure_marketplace(conn: sqlite3.Connection, name: str) -> int:
    n = (name or "").strip().lower()
    conn.execute("INSERT OR IGNORE INTO marketplaces(name) VALUES (?)", (n,))
    row = conn.execute("SELECT id FROM marketplaces WHERE name = ?", (n,)).fetchone()
    if not row:
        raise RuntimeError(f"failed to ensure marketplace row: {name}")
    return int(row[0])


def load_input_repo_stars(inp: sqlite3.Connection) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for row in inp.execute("SELECT source, stars FROM github_repo_metadata"):
        repo = str(row[0] or "").strip()
        parsed = parse_repo(repo)
        if not parsed:
            continue
        stars = row[1]
        if stars is None:
            continue
        out[f"{parsed[0]}/{parsed[1]}".lower()] = int(stars)
    return out


def load_input_repo_skill_ids(inp: sqlite3.Connection) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for row in inp.execute("SELECT source, skill_id FROM skills ORDER BY source, skill_id"):
        repo = str(row[0] or "").strip()
        skill_id = str(row[1] or "").strip()
        parsed = parse_repo(repo)
        if not parsed or not skill_id:
            continue
        key = f"{parsed[0]}/{parsed[1]}".lower()
        arr = out.setdefault(key, [])
        if skill_id not in arr:
            arr.append(skill_id)
    return out


def load_input_skill_rows(inp: sqlite3.Connection) -> List[sqlite3.Row]:
    cols = {str(r[1]) for r in inp.execute("PRAGMA table_info(skills)").fetchall()}
    installs_expr = "installs" if "installs" in cols else "NULL AS installs"
    q = f"""
        SELECT rowid AS _rowid, source, skill_id, name, {installs_expr}
        FROM skills
        ORDER BY source, skill_id
    """
    return list(inp.execute(q).fetchall())


def load_state_repos(out: sqlite3.Connection) -> Set[str]:
    repos: Set[str] = set()
    for row in out.execute("SELECT repository FROM repos"):
        repo = str(row[0] or "").strip()
        parsed = parse_repo(repo)
        if not parsed:
            continue
        repos.add(f"{parsed[0]}/{parsed[1]}".lower())
    return repos


def move_dir_merge(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        shutil.copytree(src, dst, dirs_exist_ok=True)
        shutil.rmtree(src, ignore_errors=True)
        return
    shutil.move(str(src), str(dst))


def score_skill_id_match(skill_row: sqlite3.Row, skill_id: str) -> int:
    sid = norm_token(skill_id)
    if not sid:
        return -1

    skill_key = norm_token(str(skill_row["skill_key"] or ""))
    skill_dir = norm_token(Path(str(skill_row["skill_dir"] or "")).name)
    skill_path = norm_token(Path(str(skill_row["skill_path"] or "")).name)

    score = -1
    for token in [skill_key, skill_dir, skill_path]:
        if not token:
            continue
        if sid == token:
            score = max(score, 100 + len(token))
        elif token in sid:
            score = max(score, 60 + len(token))
        elif sid in token and len(sid) >= 5:
            score = max(score, 30 + len(sid))
    return score


def assign_repo_slugs_one_to_one(
    skill_rows: Sequence[sqlite3.Row],
    repo_skill_ids: Sequence[str],
    preclaimed_ids: Set[str],
) -> Dict[int, str]:
    candidates: List[Tuple[int, int, str]] = []
    for row in skill_rows:
        row_id = int(row["id"])
        for sid in repo_skill_ids:
            if sid in preclaimed_ids:
                continue
            score = score_skill_id_match(row, sid)
            if score > 0:
                candidates.append((score, row_id, sid))

    # Highest score first, then deterministic tie-breakers.
    candidates.sort(key=lambda x: (-x[0], x[1], x[2]))

    row_taken: Set[int] = set()
    id_taken: Set[str] = set(preclaimed_ids)
    assigned: Dict[int, str] = {}
    for score, row_id, sid in candidates:
        if row_id in row_taken:
            continue
        if sid in id_taken:
            continue
        assigned[row_id] = sid
        row_taken.add(row_id)
        id_taken.add(sid)
    return assigned


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Post-download sync for skills_sh: stars, slugs, and marketplace links."
    )
    ap.add_argument("--input-db", required=True, help="skills_sh source sqlite db (contains github_repo_metadata and skills)")
    ap.add_argument("--state-db", required=True, help="output state sqlite db (e.g. <root>/state/sqlite.db)")
    ap.add_argument("--root", required=True, help="download root that contains skills/ and skills_additional/")
    ap.add_argument("--marketplace", default="skills_sh", help="marketplace name (default: skills_sh)")
    ap.add_argument(
        "--input-metadata-all-rows",
        action="store_true",
        help="Write skills rows for all input skills, not only repos present in state db.",
    )
    ap.add_argument(
        "--rewrite-slugs",
        action="store_true",
        help="Reset slug values for overlapping repos before recomputing one-to-one matches.",
    )
    ap.add_argument("--dry-run", action="store_true", help="print actions without writing")
    args = ap.parse_args()

    input_db = Path(args.input_db)
    state_db = Path(args.state_db)
    root = Path(args.root)
    marketplace = str(args.marketplace).strip().lower()

    if not input_db.exists():
        raise RuntimeError(f"input db not found: {input_db}")
    if not state_db.exists():
        raise RuntimeError(f"state db not found: {state_db}")

    inp = sqlite3.connect(str(input_db))
    inp.row_factory = sqlite3.Row

    out = sqlite3.connect(str(state_db))
    out.row_factory = sqlite3.Row
    out.execute("PRAGMA foreign_keys=ON;")
    out.execute("PRAGMA busy_timeout=5000;")

    try:
        repo_stars = load_input_repo_stars(inp)
        repo_skill_ids = load_input_repo_skill_ids(inp)
        state_repos = load_state_repos(out)
        input_repos = set(repo_skill_ids.keys()) | set(repo_stars.keys())
        target_repos = state_repos & input_repos

        if not target_repos:
            print("No overlapping repos between input db and state db.")
            return

        # Use one explicit transaction for the full sync to avoid nested BEGIN errors.
        if not out.in_transaction:
            out.execute("BEGIN IMMEDIATE")

        ensure_repos_stars_column(out)
        marketplace_id = ensure_marketplace(out, marketplace)

        stars_updates = 0
        slug_updates = 0
        repo_links_added = 0
        skill_links_added = 0
        unresolved_slug_rows = 0
        moved_from_additional = 0
        input_rows_written = 0
        input_rows_duplicate = 0
        matched_slugs_by_repo: Dict[str, Set[str]] = {}

        # 1) Sync stars into repos
        for repo_key in sorted(target_repos):
            stars = repo_stars.get(repo_key)
            if stars is None:
                continue
            cur = out.execute(
                "UPDATE repos SET stars = ? WHERE lower(repository) = ?",
                (stars, repo_key),
            )
            stars_updates += int(cur.rowcount or 0)

        # 2) Recompute/fill slug from input skills.skill_id (one-to-one per repo)
        if args.rewrite_slugs:
            for repo_key in sorted(target_repos):
                out.execute("UPDATE skills SET slug = NULL WHERE lower(repository) = ?", (repo_key,))

        for repo_key in sorted(target_repos):
            ids = repo_skill_ids.get(repo_key, [])
            if not ids:
                continue
            rows = list(
                out.execute(
                """
                SELECT id, repository, skill_path, skill_key, skill_dir, slug
                FROM skills
                WHERE lower(repository) = ?
                """,
                (repo_key,),
                ).fetchall()
            )
            preclaimed_ids = {
                str(r["slug"]).strip()
                for r in rows
                if str(r["slug"] or "").strip()
            }

            empty_rows = [r for r in rows if not str(r["slug"] or "").strip()]
            assigned = assign_repo_slugs_one_to_one(empty_rows, ids, preclaimed_ids)
            for row in rows:
                existing_slug = str(row["slug"] or "").strip()
                if existing_slug:
                    continue
                inferred = assigned.get(int(row["id"]))
                if not inferred:
                    unresolved_slug_rows += 1
                    continue
                out.execute("UPDATE skills SET slug = ? WHERE id = ?", (inferred, int(row["id"])))
                slug_updates += 1

        # 3) Rebuild marketplace links for this marketplace only
        out.execute("DELETE FROM repo_marketplace_links WHERE marketplace_id = ?", (marketplace_id,))
        out.execute("DELETE FROM skill_marketplace_links WHERE marketplace_id = ?", (marketplace_id,))

        for repo_key in sorted(target_repos):
            out.execute(
                """
                INSERT OR IGNORE INTO repo_marketplace_links(repository, marketplace_id)
                SELECT repository, ?
                FROM repos
                WHERE lower(repository) = ?
                """,
                (marketplace_id, repo_key),
            )
            repo_links_added += int(out.execute("SELECT changes()").fetchone()[0] or 0)

        for repo_key in sorted(target_repos):
            valid_ids = set(repo_skill_ids.get(repo_key, []))
            if not valid_ids:
                continue
            rows = out.execute(
                """
                SELECT repository, skill_path, slug
                FROM skills
                WHERE lower(repository) = ?
                """,
                (repo_key,),
            ).fetchall()
            for row in rows:
                slug = str(row["slug"] or "").strip()
                if not slug:
                    continue
                if slug not in valid_ids:
                    continue
                out.execute(
                    """
                    INSERT OR IGNORE INTO skill_marketplace_links(repository, skill_path, marketplace_id)
                    VALUES (?, ?, ?)
                    """,
                    (str(row["repository"]), str(row["skill_path"]), marketplace_id),
                )
                skill_links_added += int(out.execute("SELECT changes()").fetchone()[0] or 0)
                matched_slugs_by_repo.setdefault(repo_key, set()).add(slug)

        # 4) Move matched slugs from skills_additional/<owner>/<repo>/<slug> -> skills/<owner>/<repo>/<slug>
        for repo_key in sorted(target_repos):
            parsed = parse_repo(repo_key)
            if not parsed:
                continue
            owner, repo = parsed
            for slug in sorted(matched_slugs_by_repo.get(repo_key, set())):
                src = root / "skills_additional" / owner / repo / slug
                dst = root / "skills" / owner / repo / slug
                if not src.exists():
                    continue
                moved_from_additional += 1
                if not args.dry_run:
                    move_dir_merge(src, dst)

        # 5) Upsert one skills row per input skill, mark duplicates and canonical links.
        ensure_skill_input_metadata_tracking_columns(out)
        input_skill_rows = load_input_skill_rows(inp)

        # Map (repo_lower, marketplace, slug_lower) -> skill_path for linking input rows to extracted skills.
        state_skill_path_by_repo_slug: Dict[Tuple[str, str, str], str] = {}
        for row in out.execute(
            "SELECT repository, skill_path, slug, marketplace FROM skill_input_metadata WHERE lower(marketplace) = ?",
            (marketplace,),
        ):
            repo = str(row["repository"] or "").strip()
            slug = str(row["slug"] or "").strip()
            market = str(row["marketplace"] or "").strip().lower()
            if not repo or not slug:
                continue
            key = (repo.lower(), market, slug.lower())
            # keep first deterministic mapping
            state_skill_path_by_repo_slug.setdefault(key, str(row["skill_path"] or "").strip())

        # First occurrence map by normalized skill name (or skill_id fallback) per marketplace.
        canonical_by_norm: Dict[Tuple[str, str], Tuple[str, str]] = {}
        row_payloads: List[Dict[str, str]] = []
        for r in input_skill_rows:
            repo = str(r["source"] or "").strip()
            skill_id = str(r["skill_id"] or "").strip()
            name = str(r["name"] or "").strip()
            installs = r["installs"]
            if not repo or not skill_id:
                continue
            if (not args.input_metadata_all_rows) and (repo.lower() not in target_repos):
                continue
            n = norm_token(name or skill_id)
            if not n:
                n = norm_token(skill_id)
            canonical_key = (marketplace, n)
            canonical_by_norm.setdefault(canonical_key, (repo, skill_id))
            canon_repo, canon_skill_id = canonical_by_norm[canonical_key]
            is_dup = 0 if (repo == canon_repo and skill_id == canon_skill_id) else 1
            if is_dup:
                input_rows_duplicate += 1

            mapped_skill_path = state_skill_path_by_repo_slug.get((repo.lower(), marketplace, skill_id.lower()), "")
            if not mapped_skill_path:
                mapped_skill_path = f"__input__/{marketplace}/{skill_id}"

            row_payloads.append(
                {
                    "repository": repo,
                    "skill_path": mapped_skill_path,
                    "marketplace": marketplace,
                    "slug": skill_id,
                    "name": name or skill_id,
                    "source": repo,
                    "input_ref": f"input_skills_rowid={int(r['_rowid'])}",
                    "input_skill_id": skill_id,
                    "input_skill_name": name or "",
                    "installs": "" if installs is None else str(int(installs)),
                    "is_duplicate": str(is_dup),
                    "canonical_source": canon_repo,
                    "canonical_skill_id": canon_skill_id,
                }
            )

        # Build canonical path mapping for link columns.
        canonical_path_by_key: Dict[Tuple[str, str], Tuple[str, str]] = {}
        for p in row_payloads:
            if p["is_duplicate"] != "0":
                continue
            canonical_path_by_key[(p["repository"], p["input_skill_id"])] = (
                p["repository"],
                p["skill_path"],
            )

        ts = now_iso()
        for p in row_payloads:
            canon_repo, canon_id = p["canonical_source"], p["canonical_skill_id"]
            canon_rep_path = canonical_path_by_key.get((canon_repo, canon_id), (canon_repo, f"__input__/{marketplace}/{canon_id}"))
            repo_owner = (str(p["repository"]).split("/", 1)[0] if "/" in str(p["repository"]) else "")
            skill_downloaded = 0 if str(p["skill_path"]).startswith(f"__input__/{marketplace}/") else 1
            match_status = "matched" if skill_downloaded else "not_found"
            match_reason = None if skill_downloaded else "input_skill_not_present_anymore"
            out.execute(
                """
                INSERT INTO skill_input_metadata(
                    repository, skill_path, marketplace, slug, name, author, stars, installs, source, input_ref, seen_at, updated_at,
                    input_skill_id, input_skill_name, is_duplicate, canonical_source, canonical_skill_id,
                    canonical_repository, canonical_skill_path, repo_downloaded, skill_downloaded, match_status, match_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    match_reason=excluded.match_reason
                """,
                (
                    p["repository"],
                    p["skill_path"],
                    p["marketplace"],
                    p["slug"],
                    p["name"],
                    repo_owner,
                    repo_stars.get(str(p["repository"]).lower()),
                    (int(p["installs"]) if p["installs"] else None),
                    p["source"],
                    p["input_ref"],
                    ts,
                    ts,
                    p["input_skill_id"],
                    p["input_skill_name"],
                    int(p["is_duplicate"]),
                    p["canonical_source"],
                    p["canonical_skill_id"],
                    canon_rep_path[0],
                    canon_rep_path[1],
                    1,
                    skill_downloaded,
                    match_status,
                    match_reason,
                ),
            )
            input_rows_written += 1

        if args.dry_run:
            out.rollback()
            print("dry-run only: no changes committed")
        else:
            out.commit()

        print(f"target repos:              {len(target_repos)}")
        print(f"repos stars updated:       {stars_updates}")
        print(f"skills slug filled:        {slug_updates}")
        print(f"skills unresolved slugs:   {unresolved_slug_rows}")
        print(f"repo links inserted:       {repo_links_added}")
        print(f"skill links inserted:      {skill_links_added}")
        print(f"folders moved to skills/:  {moved_from_additional}")
        print(f"input rows written:        {input_rows_written}")
        print(f"input rows marked dup:     {input_rows_duplicate}")
        print(f"input rows scope:          {'all_input_rows' if args.input_metadata_all_rows else 'state_overlap_only'}")
        print(f"marketplace:               {marketplace}")
        print(f"state db:                  {state_db}")
    finally:
        inp.close()
        out.close()


if __name__ == "__main__":
    main()
