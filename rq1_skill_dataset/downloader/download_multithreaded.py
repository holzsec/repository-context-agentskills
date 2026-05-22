#!/usr/bin/env python3
"""
Stateful GitHub downloader with marketplace + skill mapping.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from sqlite_store import (
    add_additional_skill,
    add_additional_skill_marketplace,
    add_repo,
    add_repo_marketplace,
    add_skill,
    add_skill_input_metadata,
    add_skill_marketplace,
    connect,
    get_repo,
    repo_hash_exists,
)


RE_OWNER_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
AUTH_PROMPT_HINTS = (
    "Username for",
    "Password for",
    "could not read Username",
    "Authentication failed",
    "fatal: Authentication",
    "remote: Repository not found",
    "403",
    "401",
)
DONE_STATUSES = {"ok", "duplicate_hash", "large_repo", "skills_space_limited", "skipped_auth"}


@dataclass(frozen=True)
class RepoRow:
    owner: str
    repo: str
    source: str
    input_ref: str
    marketplace: str
    slugs: Tuple[str, ...]
    stars: Optional[int] = None
    skill_name: Optional[str] = None
    skill_description: Optional[str] = None
    skill_category: Optional[str] = None
    skill_author: Optional[str] = None
    skill_verified: Optional[bool] = None
    skill_tags_json: Optional[str] = None
    skill_installs: Optional[int] = None
    redirected_to: Optional[str] = None


@dataclass
class RepoTask:
    owner: str
    repo: str
    rows: List[RepoRow]

    @property
    def repository(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def source(self) -> str:
        return self.rows[0].source if self.rows else ""

    @property
    def input_ref(self) -> str:
        return self.rows[0].input_ref if self.rows else ""

    def marketplace_slug_map(self) -> Dict[str, Set[str]]:
        out: Dict[str, Set[str]] = {}
        for row in self.rows:
            if not row.marketplace:
                continue
            out.setdefault(row.marketplace, set()).update(row.slugs)
        return out

    def repo_stars(self) -> Optional[int]:
        vals = [int(r.stars) for r in self.rows if r.stars is not None]
        if not vals:
            return None
        return max(vals)

    def redirected_to(self) -> Optional[str]:
        for r in self.rows:
            v = (r.redirected_to or "").strip()
            if v and parse_owner_repo(v):
                return v
        return None

    def marketplace_slug_metadata_map(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        out: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for row in self.rows:
            if not row.marketplace or not row.slugs:
                continue
            meta: Dict[str, Any] = {}
            if row.skill_name:
                meta["name"] = row.skill_name
            if row.skill_description:
                meta["description"] = row.skill_description
            if row.skill_category:
                meta["category"] = row.skill_category
            if row.skill_author:
                meta["author"] = row.skill_author
            if row.stars is not None:
                meta["stars"] = int(row.stars)
            if row.skill_verified is not None:
                meta["verified"] = bool(row.skill_verified)
            if row.skill_tags_json:
                meta["tags_json"] = row.skill_tags_json
            if row.skill_installs is not None:
                meta["installs"] = int(row.skill_installs)
            if not meta:
                continue
            by_slug = out.setdefault(row.marketplace, {})
            for slug in row.slugs:
                if not slug:
                    continue
                by_slug.setdefault(slug, {}).update(meta)
        return out


# -----------------------------
# Utilities
# -----------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(
    cmd: List[str],
    *,
    cwd: Optional[Path] = None,
    verbose: bool = False,
    extra_env: Optional[Dict[str, str]] = None,
    timeout: Optional[int] = None,
) -> subprocess.CompletedProcess:
    if verbose:
        print("+", " ".join(cmd))
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        env=env,
        timeout=timeout,
    )


def ensure_git() -> None:
    cp = run(["git", "--version"])
    if cp.returncode != 0:
        raise RuntimeError("git not found in PATH")


def safe_rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def backup_and_recreate_db(db_path: Path, root: Path) -> Optional[Path]:
    if not db_path.exists():
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_dir = root / "state" / "backup" / ts
    backup_dir.mkdir(parents=True, exist_ok=True)

    moved_any = False
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(db_path) + suffix)
        if not src.exists():
            continue
        dst = backup_dir / src.name
        shutil.move(str(src), str(dst))
        moved_any = True

    if not moved_any:
        return None
    return backup_dir


def parse_owner_repo(value: str) -> Optional[Tuple[str, str]]:
    token = (value or "").strip().strip("/")
    if not token or not RE_OWNER_REPO.match(token):
        return None
    owner, repo = token.split("/", 1)
    return owner, repo


def norm_token(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def parse_slugs(value: Any) -> Tuple[str, ...]:
    if value is None:
        return tuple()
    if isinstance(value, (list, tuple, set)):
        out = [str(x).strip() for x in value if str(x).strip()]
        return tuple(dict.fromkeys(out))
    text = str(value).strip()
    if not text:
        return tuple()
    parts = [x.strip() for x in re.split(r"[,;]", text) if x.strip()]
    return tuple(dict.fromkeys(parts))


def parse_optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(s)
    except Exception:
        return None


def parse_optional_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if not s:
        return None
    if s in {"1", "true", "yes", "y"}:
        return True
    if s in {"0", "false", "no", "n"}:
        return False
    return None


def parse_tags_json(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        tags = [str(x).strip() for x in value if str(x).strip()]
        if not tags:
            return None
        return json.dumps(tags, ensure_ascii=False)
    s = str(value).strip()
    if not s:
        return None
    try:
        parsed = json.loads(s)
        if isinstance(parsed, list):
            tags = [str(x).strip() for x in parsed if str(x).strip()]
            if tags:
                return json.dumps(tags, ensure_ascii=False)
    except Exception:
        pass
    tags = [x.strip() for x in re.split(r"[,;]", s) if x.strip()]
    if not tags:
        return None
    return json.dumps(tags, ensure_ascii=False)


def input_skill_row_path(marketplace: str, slug: str) -> str:
    m = safe_fs_name((marketplace or "unknown").strip().lower())
    s = safe_fs_name((slug or "unknown").strip())
    return f"__input__/{m}/{s}"


def safe_fs_name(name: str) -> str:
    n = (name or "").strip().replace("\\", "/").strip("/")
    if not n:
        return "root"
    n = re.sub(r"[^A-Za-z0-9._-]+", "-", n)
    n = re.sub(r"-{2,}", "-", n).strip("-")
    return n or "root"


def parse_github_owner_repo_from_url(url: str) -> Optional[str]:
    u = (url or "").strip()
    if not u:
        return None
    m = re.search(r"github\.com[:/]+([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$", u)
    if not m:
        return None
    return m.group(1)


def append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def additional_skill_name_from_extracted_entry(task_repo: str, sk: Dict[str, Any], repo_skill_path: str) -> str:
    skill_dir = str(sk.get("skill_dir", "")).strip()
    if skill_dir and skill_dir not in {".", "./"}:
        base = Path(skill_dir).name
        if base:
            return task_repo if base.lower() == "root" else base
    p = Path(repo_skill_path)
    if p.name and p.name.lower() != "skill.md":
        return task_repo if p.name.lower() == "root" else p.name
    return task_repo


def additional_skill_rel_path(owner: str, repo: str, skill_name: str) -> str:
    return f"skills_additional/{owner}/{repo}/{safe_fs_name(skill_name)}"


def move_folder_merge(src: Path, dst: Path) -> None:
    if src.resolve() == dst.resolve():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        shutil.copytree(src, dst, dirs_exist_ok=True)
        safe_rmtree(src)
        return
    shutil.move(str(src), str(dst))


def extracted_entry_storage_rel_path(root: Path, sk: Dict[str, Any]) -> str:
    p = Path(str(sk.get("path", "")).strip())
    if p:
        if p.is_absolute():
            try:
                return str(p.relative_to(root)).replace("\\", "/")
            except Exception:
                pass
        s = str(p).replace("\\", "/").strip()
        if s.startswith("skills/") or s.startswith("skills_additional/"):
            return s
    # Fallbacks: older extract payloads.
    for key in ("skill_md", "skill_dir"):
        s = str(sk.get(key, "")).strip().replace("\\", "/")
        if s:
            return s
    return ""


def infer_marketplace_from_path(path: Path) -> str:
    parts = [p.lower() for p in path.parts]
    if "marketplaces" in parts:
        i = parts.index("marketplaces")
        if i + 1 < len(parts):
            return parts[i + 1]
    return ""


def hash_folder_excluding_git(folder: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(folder.rglob("*")):
        if ".git" in p.parts:
            continue
        try:
            is_file = p.is_file()
        except OSError:
            continue
        if not is_file:
            continue
        try:
            rel = str(p.relative_to(folder)).replace("\\", "/")
            with p.open("rb") as f:
                h.update(rel.encode("utf-8"))
                h.update(b"\0")
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
                h.update(b"\0")
        except OSError:
            # Keep hashing robust even if one file cannot be read.
            continue
    return h.hexdigest()


def folder_size_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                continue
    return total


def bytes_to_mb(n: int) -> float:
    return round(n / (1024 * 1024), 2)


def repo_target(root: Path, owner: str, repo: str) -> Path:
    return root / "repo" / owner / repo


def repo_zip_target(root: Path, owner: str, repo: str) -> Path:
    return root / "repo_zip" / owner / f"{repo}.zip"


def looks_like_auth_issue(stderr: str, stdout: str) -> bool:
    blob = (stderr or "") + "\n" + (stdout or "")
    return any(hint in blob for hint in AUTH_PROMPT_HINTS)


def git_out(repo_dir: Path, args: List[str], *, timeout: int = 30) -> Optional[str]:
    cp = run(
        ["git", "-C", str(repo_dir), *args],
        extra_env={"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"},
        timeout=timeout,
    )
    if cp.returncode != 0:
        return None
    return (cp.stdout or "").strip() or None


def git_head_info(repo_dir: Path) -> Dict[str, Optional[str]]:
    return {
        "repo_head_commit": git_out(repo_dir, ["rev-parse", "HEAD"]),
        "repo_head_commit_at": git_out(repo_dir, ["log", "-1", "--format=%cI"]),
    }


def zip_repo_folder(src_repo_dir: Path, zip_path: Path) -> Path:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_base = zip_path.with_name(f".{zip_path.stem}-{uuid.uuid4().hex}.tmp")
    archive_file = Path(shutil.make_archive(str(tmp_base), "zip", root_dir=str(src_repo_dir), base_dir="."))
    os.replace(archive_file, zip_path)
    return zip_path


def remove_git_dir(repo_dir: Path) -> None:
    safe_rmtree(repo_dir / ".git")


def log_step(repository: str, verbose: bool, step: str) -> None:
    if verbose:
        print(f"[{repository}] {step}")


def skill_matches_slug(skill: Dict[str, Any], owner: str, slug: str) -> bool:
    slug_norm = norm_token(slug)
    if not slug_norm:
        return False

    candidates: List[str] = []
    for k in ("slug", "slug_raw", "skill_key"):
        v = str(skill.get(k, "") or "").strip()
        if not v:
            continue
        candidates.append(v)

    owner_prefix = owner + "-"
    cleaned = []
    for c in candidates:
        if c.lower().startswith(owner_prefix.lower()):
            cleaned.append(c[len(owner_prefix):])
        cleaned.append(c)

    for c in cleaned:
        n = norm_token(c)
        if not n:
            continue
        if n == slug_norm or n in slug_norm or slug_norm in n:
            return True
    return False


# -----------------------------
# Input parsing
# -----------------------------
def _as_list(obj: Any) -> List[Any]:
    return obj if isinstance(obj, list) else [obj]


def is_json_file(path: Path) -> bool:
    return path.suffix.lower() in {".json"}


def is_jsonl_file(path: Path) -> bool:
    return path.suffix.lower() in {".jsonl"}


def is_csv_file(path: Path) -> bool:
    return path.suffix.lower() in {".csv", ".tsv"}


def is_sqlite_file(path: Path) -> bool:
    return path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}


def load_from_text(path: Path) -> List[RepoRow]:
    items: List[RepoRow] = []
    default_marketplace = infer_marketplace_from_path(path)
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split()
        repo_token = parts[0].strip()
        parsed = parse_owner_repo(repo_token)
        if not parsed:
            print(f"warn: invalid repo at {path}:{lineno}: {raw!r}", file=sys.stderr)
            continue

        owner, repo = parsed
        marketplace = parts[1].strip().lower() if len(parts) > 1 else default_marketplace
        slugs = parse_slugs(" ".join(parts[2:]) if len(parts) > 2 else "")
        source_val = marketplace or f"{path}:{lineno}"

        items.append(
            RepoRow(
                owner=owner,
                repo=repo,
                source=source_val,
                input_ref=f"{path}:{lineno}",
                marketplace=marketplace,
                slugs=slugs,
                stars=None,
            )
        )
    return items


def _from_obj(path: Path, idx_ref: str, entry: Dict[str, Any]) -> Optional[RepoRow]:
    repo_str = str(entry.get("repository", "")).strip()
    parsed = parse_owner_repo(repo_str)
    if not parsed:
        return None

    owner, repo = parsed
    marketplace = str(entry.get("marketplace", "")).strip().lower()
    if not marketplace:
        source_hint = str(entry.get("source", "")).strip()
        if source_hint and "/" not in source_hint:
            marketplace = source_hint.lower()
    if not marketplace:
        marketplace = infer_marketplace_from_path(path)

    slug_value = entry.get("slugs", None)
    if slug_value is None:
        slug_value = entry.get("slug", None)
    slugs = parse_slugs(slug_value)
    stars = parse_optional_int(entry.get("stars"))
    skill_name = str(entry.get("name", "") or "").strip() or None
    skill_description = str(entry.get("description", "") or "").strip() or None
    skill_category = str(entry.get("category", "") or "").strip() or None
    skill_author = str(entry.get("author", "") or "").strip() or None
    skill_verified = parse_optional_bool(entry.get("verified"))
    skill_tags_json = parse_tags_json(entry.get("tags"))
    skill_installs = parse_optional_int(entry.get("installs"))
    redirected_to = str(entry.get("redirected_to", "") or "").strip() or None

    source_val = str(entry.get("source", "")).strip() or marketplace or idx_ref
    return RepoRow(
        owner=owner,
        repo=repo,
        source=source_val,
        input_ref=idx_ref,
        marketplace=marketplace,
        slugs=slugs,
        stars=stars,
        skill_name=skill_name,
        skill_description=skill_description,
        skill_category=skill_category,
        skill_author=skill_author,
        skill_verified=skill_verified,
        skill_tags_json=skill_tags_json,
        skill_installs=skill_installs,
        redirected_to=redirected_to,
    )


def load_from_json(path: Path) -> List[RepoRow]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out: List[RepoRow] = []
    for idx, entry in enumerate(_as_list(data)):
        if not isinstance(entry, dict):
            continue
        row = _from_obj(path, f"{path}#{idx}", entry)
        if row is None:
            print(f"warn: JSON entry #{idx} invalid 'repository'", file=sys.stderr)
            continue
        out.append(row)
    return out


def load_from_jsonl(path: Path) -> List[RepoRow]:
    items: List[RepoRow] = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                print(f"warn: invalid JSON at {path}:{lineno}", file=sys.stderr)
                continue
            if not isinstance(entry, dict):
                continue
            row = _from_obj(path, f"{path}:{lineno}", entry)
            if row is None:
                print(f"warn: invalid repo at {path}:{lineno}", file=sys.stderr)
                continue
            items.append(row)
    return items


def load_from_csv(path: Path) -> List[RepoRow]:
    items: List[RepoRow] = []
    default_marketplace = infer_marketplace_from_path(path)
    delim = "\t" if path.suffix.lower() == ".tsv" else ","
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter=delim)
        if not reader.fieldnames or "repository" not in reader.fieldnames:
            raise RuntimeError(f"CSV/TSV missing required column 'repository': {path}")

        for rowno, row in enumerate(reader, start=2):
            repo_str = str(row.get("repository", "")).strip()
            parsed = parse_owner_repo(repo_str)
            if not parsed:
                print(f"warn: invalid repo at {path}:{rowno}: {repo_str!r}", file=sys.stderr)
                continue

            owner, repo = parsed
            marketplace = str(row.get("marketplace", "")).strip().lower() or default_marketplace
            source_val = str(row.get("source", "")).strip() or marketplace or f"{path}:{rowno}"
            slug_val = row.get("slugs", None)
            if slug_val in (None, ""):
                slug_val = row.get("slug", "")

            items.append(
                RepoRow(
                    owner=owner,
                    repo=repo,
                    source=source_val,
                    input_ref=f"{path}:{rowno}",
                    marketplace=marketplace,
                    slugs=parse_slugs(slug_val),
                    stars=parse_optional_int(row.get("stars")),
                    skill_installs=parse_optional_int(row.get("installs")),
                    redirected_to=(str(row.get("redirected_to", "") or "").strip() or None),
                )
            )
    return items


def load_from_sqlite(path: Path, *, table: str, repo_col: str, where: str) -> List[RepoRow]:
    if not path.exists():
        raise RuntimeError(f"SQLite file not found: {path}")

    items: List[RepoRow] = []
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    default_marketplace = infer_marketplace_from_path(path)
    try:
        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({table})")
        cols = {str(r[1]).lower() for r in cur.fetchall()}
        stars_expr = "stars AS _stars" if "stars" in cols else "NULL AS _stars"
        redirected_expr = "redirected_to AS _redirected_to" if "redirected_to" in cols else "NULL AS _redirected_to"
        sql = f"SELECT rowid AS _rowid, {repo_col} AS _repo, {stars_expr}, {redirected_expr} FROM {table}"
        if where.strip():
            sql += f" WHERE {where}"
        cur.execute(sql)
        repo_rows = cur.fetchall()

        # Optional enrichment from companion skills table:
        # source (repo), skill_id (optional), name (optional), installs (optional)
        skills_by_repo: Dict[str, List[Dict[str, Any]]] = {}
        tables = {
            str(x[0]).strip().lower()
            for x in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "skills" in tables:
            s_cur = conn.cursor()
            s_cols = {
                str(c[1]).strip().lower()
                for c in s_cur.execute("PRAGMA table_info(skills)").fetchall()
            }
            if "source" in s_cols:
                skill_id_expr = "skill_id AS _skill_id" if "skill_id" in s_cols else "NULL AS _skill_id"
                name_expr = "name AS _name" if "name" in s_cols else "NULL AS _name"
                installs_expr = "installs AS _installs" if "installs" in s_cols else "NULL AS _installs"
                s_cur.execute(f"SELECT rowid AS _rowid, source AS _repo, {skill_id_expr}, {name_expr}, {installs_expr} FROM skills")
                for sr in s_cur.fetchall():
                    repo_str = str(sr["_repo"] or "").strip()
                    parsed = parse_owner_repo(repo_str)
                    if not parsed:
                        continue
                    key = f"{parsed[0]}/{parsed[1]}".lower()
                    skill_id = str(sr["_skill_id"] or "").strip()
                    name = str(sr["_name"] or "").strip()
                    identifier = skill_id or name
                    if not identifier:
                        continue
                    skills_by_repo.setdefault(key, []).append(
                        {
                            "identifier": identifier,
                            "name": name or None,
                            "installs": parse_optional_int(sr["_installs"]),
                            "rowid": int(sr["_rowid"]),
                        }
                    )

        for i, r in enumerate(repo_rows, start=1):
            repo_str = str(r["_repo"] if "_repo" in r.keys() else "").strip()
            parsed = parse_owner_repo(repo_str)
            if not parsed:
                print(f"warn: invalid repo in sqlite {path}:{table}:row={i}: {repo_str!r}", file=sys.stderr)
                continue
            owner, repo = parsed
            repo_key = f"{owner}/{repo}".lower()
            base_input_ref = f"{path}:{table}:rowid={r['_rowid']}" if "_rowid" in r.keys() else f"{path}:{table}:row={i}"
            stars = parse_optional_int(r["_stars"]) if "_stars" in r.keys() else None
            redirected_to = (str(r["_redirected_to"] or "").strip() if "_redirected_to" in r.keys() else "") or None

            skill_rows = skills_by_repo.get(repo_key, [])
            if not skill_rows:
                items.append(
                    RepoRow(
                        owner=owner,
                        repo=repo,
                        source=repo_str,
                        input_ref=base_input_ref,
                        marketplace=default_marketplace,
                        slugs=tuple(),
                        stars=stars,
                        redirected_to=redirected_to,
                    )
                )
                continue

            for sk in skill_rows:
                items.append(
                    RepoRow(
                        owner=owner,
                        repo=repo,
                        source=repo_str,
                        input_ref=f"{path}:skills:rowid={sk['rowid']}",
                        marketplace=default_marketplace,
                        slugs=(str(sk["identifier"]),),
                        stars=stars,
                        skill_name=sk.get("name"),
                        skill_installs=sk.get("installs"),
                        redirected_to=redirected_to,
                    )
                )
    finally:
        conn.close()

    return items


def load_input(path: Path, *, sqlite_table: str, sqlite_repo_col: str, sqlite_where: str) -> List[RepoRow]:
    if is_sqlite_file(path):
        return load_from_sqlite(path, table=sqlite_table, repo_col=sqlite_repo_col, where=sqlite_where)
    if is_jsonl_file(path):
        return load_from_jsonl(path)
    if is_json_file(path):
        return load_from_json(path)
    if is_csv_file(path):
        return load_from_csv(path)
    return load_from_text(path)


def aggregate_tasks(rows: Sequence[RepoRow]) -> List[RepoTask]:
    grouped: Dict[Tuple[str, str], List[RepoRow]] = {}
    for row in rows:
        grouped.setdefault((row.owner.lower(), row.repo.lower()), []).append(row)

    tasks: List[RepoTask] = []
    for _, grp in grouped.items():
        first = grp[0]
        tasks.append(RepoTask(owner=first.owner, repo=first.repo, rows=grp))
    return tasks


# -----------------------------
# Clone / extract
# -----------------------------
def clone_repo_atomic(
    *,
    owner: str,
    repo: str,
    dest: Path,
    overwrite: bool,
    filter_blobs: bool,
    single_branch: bool,
    timeout_seconds: int,
    verbose: bool,
) -> Tuple[str, str, bool]:
    """
    Returns: (status, message, large_repo)
    status: ok | failed | skipped_auth | large_repo
    """
    if dest.exists():
        if overwrite:
            safe_rmtree(dest)
        else:
            return "failed", "destination exists (safe-check)", False

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{dest.name}.tmp-{uuid.uuid4().hex}"
    safe_rmtree(tmp)

    url = f"https://github.com/{owner}/{repo}.git"
    cmd = ["git", "clone", "--depth", "1"]
    if single_branch:
        cmd.append("--single-branch")
    if filter_blobs:
        cmd += ["--filter=blob:none"]
    cmd += [url, str(tmp)]

    try:
        cp = run(
            cmd,
            verbose=verbose,
            timeout=timeout_seconds,
            extra_env={"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"},
        )
    except subprocess.TimeoutExpired:
        safe_rmtree(tmp)
        return "large_repo", f"clone timed out after {timeout_seconds}s", True

    if cp.returncode != 0:
        err = (cp.stderr or "").strip()
        out = (cp.stdout or "").strip()
        safe_rmtree(tmp)

        if looks_like_auth_issue(err, out):
            return "skipped_auth", (err or out or "auth required")[:300], False

        return "failed", (err or out or f"clone failed (exit={cp.returncode})")[:300], False

    if not (tmp / ".git").is_dir():
        safe_rmtree(tmp)
        return "failed", "clone finished but .git missing", False

    try:
        os.rename(tmp, dest)
    except Exception as e:
        safe_rmtree(tmp)
        return "failed", f"rename into place failed: {type(e).__name__}: {e}", False

    return "ok", "cloned", False


def load_extracted_skills_for_repo_from_state(skills_state_path: Path, repository: str) -> List[Dict[str, Any]]:
    if not skills_state_path.exists():
        return []
    try:
        payload = json.loads(skills_state_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [
        x
        for x in (payload if isinstance(payload, list) else [])
        if isinstance(x, dict) and str(x.get("repository", "")).lower() == repository.lower()
    ]


def run_extract_for_repo_isolated(
    *,
    root: Path,
    owner: str,
    repo: str,
    repo_path: Path,
    extract_input: str,
    verbose: bool,
) -> Tuple[int, str, str, List[Dict[str, Any]]]:
    repository = f"{owner}/{repo}"
    temp_base = root / "state" / "tmp_extract_runs"
    temp_base.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="extract-", dir=str(temp_base)) as tmp_dir:
        tmp_root = Path(tmp_dir)
        cmd = [
            sys.executable,
            str(Path(__file__).with_name("extract.py")),
            "--root",
            str(tmp_root),
            "--jobs",
            "1",
            "--repo-path",
            str(repo_path),
        ]
        if extract_input:
            cmd += ["--input", extract_input]

        cp = run(cmd, verbose=verbose)
        if cp.returncode != 0:
            return cp.returncode, cp.stdout or "", cp.stderr or "", []

        extracted_skills = load_extracted_skills_for_repo_from_state(
            tmp_root / "state" / "skills.json",
            repository,
        )

        moved_entries: List[Dict[str, Any]] = []
        for sk in extracted_skills:
            src_path = Path(str(sk.get("path", "")).strip())
            if not src_path.exists():
                continue

            dest_name = src_path.name
            final_path = root / "skills" / owner / repo / dest_name
            safe_rmtree(final_path)
            final_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src_path, final_path, dirs_exist_ok=True)

            sk2 = dict(sk)
            sk2["path"] = str(final_path)
            moved_entries.append(sk2)

        return cp.returncode, cp.stdout or "", cp.stderr or "", moved_entries


def cleanup_duplicate_skill_folders(extracted_entries: List[Dict[str, Any]], verbose: bool) -> Dict[str, int]:
    by_hash: Dict[str, List[Dict[str, Any]]] = {}
    for entry in extracted_entries:
        h = str(entry.get("skill_hash", "")).strip()
        if not h:
            continue
        by_hash.setdefault(h, []).append(entry)

    hashes_with_dups = 0
    removed_folders = 0
    removed_paths: Set[str] = set()
    for h, group in by_hash.items():
        if len(group) <= 1:
            continue
        hashes_with_dups += 1
        ordered = sorted(
            group,
            key=lambda x: (
                str(x.get("repository", "")).lower(),
                str(x.get("skill_md", "")).lower(),
                str(x.get("path", "")).lower(),
            ),
        )
        for dup in ordered[1:]:
            p = str(dup.get("path", "")).strip()
            if not p or p in removed_paths:
                continue
            safe_rmtree(Path(p))
            removed_paths.add(p)
            removed_folders += 1
            if verbose:
                print(f"[cleanup] removed duplicate skill folder hash={h[:12]}... path={p}")

    return {
        "hashes_with_dups": hashes_with_dups,
        "removed_folders": removed_folders,
    }


def preseed_input_rows(conn, db_lock: threading.Lock, tasks: Sequence[RepoTask], overwrite: bool) -> None:
    for task in tasks:
        repository = task.repository
        with db_lock:
            existing = get_repo(conn, repository)
            if not existing or overwrite:
                add_repo(
                    conn,
                    {
                        "repository": repository,
                        "owner": task.owner,
                        "repo": task.repo,
                        "stars": task.repo_stars(),
                        "redirected_to": task.redirected_to(),
                        "market_skills_count": 0,
                        "additional_skills_count": 0,
                        "source": task.source,
                        "input_ref": task.input_ref,
                        "status": "input_pending",
                        "message": "queued from input",
                        "large_repo": False,
                        "duplicate_hash": False,
                        "repo_hash": None,
                        "repo_head_commit": None,
                        "repo_head_commit_at": None,
                        "archive_path": None,
                        "repo_size_bytes": None,
                        "skills_size_bytes": None,
                        "clone_started_at": None,
                        "clone_finished_at": None,
                        "processed_at": now_iso(),
                    },
                )
            for row in task.rows:
                if row.marketplace:
                    add_repo_marketplace(conn, repository=repository, marketplace=row.marketplace)
                for slug in row.slugs:
                    add_skill_input_metadata(
                        conn,
                        repository=repository,
                        skill_path=input_skill_row_path(row.marketplace or "unknown", slug),
                        marketplace=row.marketplace or "unknown",
                        slug=slug,
                        source=row.source,
                        input_ref=row.input_ref,
                        name=row.skill_name,
                        author=(row.skill_author or task.owner),
                        stars=row.stars,
                        verified=row.skill_verified,
                        tags_json=row.skill_tags_json,
                        installs=row.skill_installs,
                        input_skill_id=slug,
                        input_skill_name=row.skill_name,
                        repo_downloaded=False,
                        skill_downloaded=False,
                        match_status="pending",
                        match_reason=None,
                    )


def mark_repo_downloaded_for_task(conn, db_lock: threading.Lock, task: RepoTask, downloaded: bool) -> None:
    repository = task.repository
    with db_lock:
        for row in task.rows:
            if not row.marketplace:
                continue
            conn.execute(
                """
                UPDATE skill_input_metadata
                SET repo_downloaded = ?,
                    match_status = COALESCE(match_status, 'pending'),
                    updated_at = ?
                WHERE repository = ? AND lower(marketplace) = ?
                """,
                (1 if downloaded else 0, now_iso(), repository, row.marketplace.lower()),
            )
        conn.commit()


def repo_has_downloaded_skills(conn, repository: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM skill_input_metadata
        WHERE repository = ? AND skill_downloaded = 1
        LIMIT 1
        """,
        (repository,),
    ).fetchone()
    return row is not None


def set_repo_input_reason(conn, db_lock: threading.Lock, task: RepoTask, *, status: str, reason: str) -> None:
    repository = task.repository
    with db_lock:
        for row in task.rows:
            marketplace = (row.marketplace or "").strip().lower()
            if not marketplace:
                continue
            conn.execute(
                """
                UPDATE skill_input_metadata
                SET skill_downloaded = 0,
                    repo_downloaded = 0,
                    match_status = ?,
                    match_reason = ?,
                    updated_at = ?
                WHERE repository = ? AND lower(marketplace) = ?
                """,
                (status, reason, now_iso(), repository, marketplace),
            )
        conn.commit()


def load_skill_hash_lookup(path: str) -> Dict[Tuple[str, str], Dict[str, Optional[str]]]:
    p = Path(path)
    if not p.exists():
        return {}
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    out: Dict[Tuple[str, str], Dict[str, Optional[str]]] = {}
    try:
        rows = conn.execute(
            """
            SELECT repository, skill_path, skill_hash, skill_last_modified_at
            FROM skills
            WHERE (skill_hash IS NOT NULL AND trim(skill_hash) <> '')
               OR (skill_last_modified_at IS NOT NULL AND trim(skill_last_modified_at) <> '')
            """
        ).fetchall()
        for r in rows:
            key = (str(r["repository"] or "").strip().lower(), str(r["skill_path"] or "").strip())
            h = str(r["skill_hash"] or "").strip()
            lm = str(r["skill_last_modified_at"] or "").strip()
            if key[0] and key[1] and h:
                out[key] = {
                    "skill_hash": h or None,
                    "skill_last_modified_at": lm or None,
                }
    finally:
        conn.close()
    return out


def recreate_marketplace_state(conn, db_lock: threading.Lock, marketplace: str, tasks: Sequence[RepoTask]) -> None:
    m = (marketplace or "").strip().lower()
    if not m:
        return
    repo_keys = sorted(
        {
            t.repository
            for t in tasks
            if any((r.marketplace or "").strip().lower() == m for r in t.rows)
        }
    )
    if not repo_keys:
        return

    with db_lock:
        row = conn.execute("SELECT id FROM marketplaces WHERE name = ?", (m,)).fetchone()
        marketplace_id = int(row[0]) if row else None

        placeholders = ",".join(["?"] * len(repo_keys))
        conn.execute(
            f"DELETE FROM skill_input_metadata WHERE lower(marketplace) = ? AND repository IN ({placeholders})",
            (m, *repo_keys),
        )

        if marketplace_id is not None:
            conn.execute(
                f"DELETE FROM skill_marketplace_links WHERE marketplace_id = ? AND repository IN ({placeholders})",
                (marketplace_id, *repo_keys),
            )
            conn.execute(
                f"DELETE FROM repo_marketplace_links WHERE marketplace_id = ? AND repository IN ({placeholders})",
                (marketplace_id, *repo_keys),
            )

        # Reset repo processing state so run is deterministic for this marketplace scope.
        ts = now_iso()
        conn.execute(
            f"""
            UPDATE repos
            SET status = 'input_pending',
                message = 'recreate_marketplace reset',
                updated_at = ?,
                processed_at = ?
            WHERE repository IN ({placeholders})
            """,
            (ts, ts, *repo_keys),
        )
        conn.commit()


# -----------------------------
# Processing
# -----------------------------
def process_repo(
    *,
    conn,
    db_lock: threading.Lock,
    root: Path,
    task: RepoTask,
    overwrite: bool,
    filter_blobs: bool,
    single_branch: bool,
    clone_timeout_minutes: int,
    do_extract: bool,
    extract_input: str,
    skip_store_repo: bool,
    from_extracted_skills: bool,
    retry_no_skills: bool,
    hash_lookup: Dict[Tuple[str, str], Dict[str, Optional[str]]],
    debug_no_match: bool,
    zip_max_repo_mb: int,
    max_extracted_skills_mb: int,
    verbose: bool,
) -> Dict[str, Any]:
    repository = task.repository
    redirected_to_value = task.redirected_to()
    dest = repo_target(root, task.owner, task.repo)
    zip_path = repo_zip_target(root, task.owner, task.repo)

    log_step(repository, verbose, "register marketplace mapping")
    for row in task.rows:
        if row.marketplace:
            with db_lock:
                add_repo_marketplace(
                    conn,
                    repository=repository,
                    marketplace=row.marketplace,
                )

    with db_lock:
        existing = get_repo(conn, repository)
    skip_done = (
        (not from_extracted_skills)
        and existing
        and str(existing.get("status", "")).lower() in DONE_STATUSES
        and not overwrite
    )
    if skip_done and retry_no_skills:
        with db_lock:
            if not repo_has_downloaded_skills(conn, repository):
                skip_done = False
    if skip_done:
        mark_repo_downloaded_for_task(conn, db_lock, task, True)
        log_step(repository, verbose, "skip: repo already done in sqlite")
        return {
            "repository": repository,
            "status": "skipped_done",
            "message": "already in sqlite with done status",
        }

    if from_extracted_skills:
        skills_repo_dir = root / "skills" / task.owner / task.repo
        matched = 0
        additional_count = 0
        if skills_repo_dir.exists() and skills_repo_dir.is_dir():
            existing_dirs = sorted([p.name for p in skills_repo_dir.iterdir() if p.is_dir()])
            norm_to_dir: Dict[str, str] = {}
            for d in existing_dirs:
                n = norm_token(d)
                if n and n not in norm_to_dir:
                    norm_to_dir[n] = d
            matched_dirs: Set[str] = set()
            dir_hashes: Dict[str, str] = {}

            # Build/refresh skills rows from existing extracted directories with hash.
            for d in existing_dirs:
                dir_path = skills_repo_dir / d
                skill_rel = f"skills/{task.owner}/{task.repo}/{d}"
                skill_hash = ""
                prev = hash_lookup.get((repository.lower(), skill_rel), {})
                prev_hash = str(prev.get("skill_hash") or "").strip()
                prev_last = str(prev.get("skill_last_modified_at") or "").strip() or None
                try:
                    skill_hash = hash_folder_excluding_git(dir_path)
                except Exception:
                    skill_hash = prev_hash
                if skill_hash:
                    dir_hashes[d] = skill_hash
                with db_lock:
                    add_skill(
                        conn,
                        repository=repository,
                        skill_path=skill_rel,
                        skill_key=d,
                        skill_dir=skill_rel,
                        slug=d,
                        repo_root=(d.strip().lower() == "root"),
                        skill_hash=(skill_hash or None),
                        skill_last_modified_at=prev_last,
                        extracted_at=now_iso(),
                    )

            for row in task.rows:
                marketplace = row.marketplace or "unknown"
                for slug in row.slugs:
                    n_slug = norm_token(slug)
                    dir_name = norm_to_dir.get(n_slug, "")
                    is_found = bool(dir_name)
                    if is_found:
                        matched += 1
                        matched_dirs.add(dir_name)
                        skill_rel = f"skills/{task.owner}/{task.repo}/{dir_name}"
                        with db_lock:
                            add_skill_marketplace(
                                conn,
                                repository=repository,
                                skill_path=skill_rel,
                                marketplace=marketplace,
                            )
                    with db_lock:
                        add_skill_input_metadata(
                            conn,
                            repository=repository,
                            skill_path=input_skill_row_path(marketplace, slug),
                            marketplace=marketplace,
                            slug=slug,
                            source=row.source,
                            input_ref=row.input_ref,
                            name=row.skill_name,
                            author=(row.skill_author or task.owner),
                            stars=row.stars,
                            verified=row.skill_verified,
                            tags_json=row.skill_tags_json,
                            installs=row.skill_installs,
                            input_skill_id=slug,
                            input_skill_name=row.skill_name,
                            repo_downloaded=True,
                            skill_downloaded=is_found,
                            canonical_source=(repository if is_found else None),
                            canonical_skill_id=(slug if is_found else None),
                            canonical_repository=(repository if is_found else None),
                            canonical_skill_path=(f"skills/{task.owner}/{task.repo}/{dir_name}" if is_found else None),
                            match_status=("matched" if is_found else "not_found"),
                            match_reason=(None if is_found else "input_skill_not_present_anymore"),
                            skill_hash=(dir_hashes.get(dir_name) if is_found else None),
                            skill_last_modified_at=(hash_lookup.get((repository.lower(), skill_rel), {}).get("skill_last_modified_at") if is_found else None),
                        )
            # Record additional extracted skills that were not present in input list.
            additional = [d for d in existing_dirs if d not in matched_dirs]
            if additional:
                additional_count = len(additional)
                with db_lock:
                    for d in additional:
                        old_sp = f"skills/{task.owner}/{task.repo}/{d}"
                        add_name = task.repo if d.strip().lower() == "root" else d
                        new_sp = additional_skill_rel_path(task.owner, task.repo, add_name)
                        old_abs = root / old_sp
                        new_abs = root / new_sp
                        if old_abs.exists() and old_abs.is_dir():
                            move_folder_merge(old_abs, new_abs)
                        add_additional_skill(
                            conn,
                            repository=repository,
                            skill_name=add_name,
                            skill_path=new_sp,
                            skill_hash=(dir_hashes.get(d) or None),
                            skill_last_modified_at=(hash_lookup.get((repository.lower(), old_sp), {}).get("skill_last_modified_at") or None),
                        )
                        conn.execute(
                            "DELETE FROM skills WHERE repository = ? AND skill_path = ?",
                            (repository, old_sp),
                        )
                        conn.execute(
                            "DELETE FROM skill_marketplace_links WHERE repository = ? AND skill_path = ?",
                            (repository, old_sp),
                        )
                        for row in task.rows:
                            if row.marketplace:
                                add_additional_skill_marketplace(
                                    conn,
                                    repository=repository,
                                    skill_path=new_sp,
                                    marketplace=row.marketplace,
                                )
            status = "ok_existing_skills" if matched > 0 else "no_skills_matched"
            message = f"matched input skills in extracted folder: {matched}; additional: {additional_count}"
            mark_repo_downloaded_for_task(conn, db_lock, task, True)
            if matched == 0:
                if debug_no_match:
                    sample_input: List[str] = []
                    for row in task.rows:
                        sample_input.extend([s for s in row.slugs if s])
                    sample_input = sample_input[:15]
                    sample_dirs = existing_dirs[:15]
                    with db_lock:
                        append_jsonl(
                            root / "state" / "no_match_debug.jsonl",
                            {
                                "repository": repository,
                                "reason": "no_input_skill_name_matched_extracted_folder",
                                "input_count": len(sample_input),
                                "extracted_dir_count": len(existing_dirs),
                                "input_samples": sample_input,
                                "extracted_dir_samples": sample_dirs,
                            },
                        )
                set_repo_input_reason(
                    conn,
                    db_lock,
                    task,
                    status="not_found",
                    reason="input_skill_not_present_anymore",
                )
        else:
            status = "missing_existing_skills"
            message = "skills folder for repo not found"
            mark_repo_downloaded_for_task(conn, db_lock, task, False)
            set_repo_input_reason(
                conn,
                db_lock,
                task,
                status="repo_missing",
                reason="repo_skills_folder_missing",
            )

        with db_lock:
            add_repo(
                conn,
                {
                    "repository": repository,
                    "owner": task.owner,
                    "repo": task.repo,
                    "stars": task.repo_stars(),
                    "redirected_to": redirected_to_value,
                    "market_skills_count": matched,
                    "additional_skills_count": additional_count if status != "missing_existing_skills" else 0,
                    "source": task.source,
                    "input_ref": task.input_ref,
                    "status": status,
                    "message": message,
                    "large_repo": False,
                    "duplicate_hash": False,
                    "repo_hash": None,
                    "repo_head_commit": None,
                    "repo_head_commit_at": None,
                    "archive_path": None,
                    "repo_size_bytes": None,
                    "skills_size_bytes": None,
                    "clone_started_at": None,
                    "clone_finished_at": None,
                    "processed_at": now_iso(),
                },
            )
        return {
            "repository": repository,
            "status": status,
            "message": message,
            "large_repo": False,
            "duplicate_hash": False,
            "repo_size_bytes": None,
            "skills_size_bytes": None,
            "extracted_entries": [],
            "market_found": matched,
            "additional_found": additional_count if status != "missing_existing_skills" else 0,
        }

    if dest.exists() and not overwrite:
        log_step(repository, verbose, "safe-clone check: existing workspace detected, deleting stale folder")
        safe_rmtree(dest)

    clone_owner = task.owner
    clone_repo = task.repo
    rt = task.redirected_to()
    if rt:
        parsed_rt = parse_owner_repo(rt)
        if parsed_rt:
            clone_owner, clone_repo = parsed_rt

    clone_started = now_iso()
    log_step(
        repository,
        verbose,
        f"clone start (timeout={clone_timeout_minutes}min, single_branch={single_branch}, from={clone_owner}/{clone_repo})",
    )
    status, message, large_repo = clone_repo_atomic(
        owner=clone_owner,
        repo=clone_repo,
        dest=dest,
        overwrite=overwrite,
        filter_blobs=filter_blobs,
        single_branch=single_branch,
        timeout_seconds=max(1, int(clone_timeout_minutes)) * 60,
        verbose=verbose,
    )
    clone_finished = now_iso()

    if status in {"skipped_auth", "failed", "large_repo"}:
        reason_map = {
            "skipped_auth": "auth_required_or_private_repo",
            "failed": "repo_clone_failed_or_not_found",
            "large_repo": "clone_timeout_or_repo_too_large",
        }
        set_repo_input_reason(
            conn,
            db_lock,
            task,
            status="repo_failed",
            reason=reason_map.get(status, "repo_failed"),
        )

    repo_head_commit = None
    repo_head_commit_at = None
    repo_hash = None
    duplicate_hash = False
    archive_path = None
    repo_size_bytes: Optional[int] = None
    skills_size_bytes: Optional[int] = None
    extracted_entries: List[Dict[str, Any]] = []
    market_found_count = 0
    additional_found_count = 0

    if status == "ok" and dest.exists():
        origin_url = git_out(dest, ["config", "--get", "remote.origin.url"]) or ""
        observed_repo = parse_github_owner_repo_from_url(origin_url)
        if observed_repo and observed_repo.lower() != repository.lower():
            redirected_to_value = observed_repo
            log_step(repository, verbose, f"clone redirected to {observed_repo}")

        if not skip_store_repo:
            repo_size_bytes = folder_size_bytes(dest)
            log_step(repository, verbose, f"repo size: {bytes_to_mb(repo_size_bytes)} MB")

        head = git_head_info(dest)
        repo_head_commit = head.get("repo_head_commit")
        repo_head_commit_at = head.get("repo_head_commit_at")
        log_step(repository, verbose, "read git head metadata")

        try:
            repo_hash = hash_folder_excluding_git(dest)
            log_step(repository, verbose, f"computed repo hash: {repo_hash[:12]}...")
        except Exception as e:
            status = "failed"
            message = f"repo hash failed: {type(e).__name__}: {e}"
            log_step(repository, verbose, message)
            repo_hash = None

        if repo_hash:
            with db_lock:
                duplicate_hash = repo_hash_exists(conn, repo_hash)
            if duplicate_hash:
                status = "duplicate_hash"
                message = "repo hash already present in sqlite (extract still executed)"
                log_step(repository, verbose, "duplicate repo hash found, extraction continues")
        if do_extract:
            log_step(repository, verbose, "extract skills (isolated state)")
            rc, out, err, extracted_skills = run_extract_for_repo_isolated(
                root=root,
                owner=task.owner,
                repo=task.repo,
                repo_path=dest,
                extract_input=extract_input,
                verbose=verbose,
            )
            if rc != 0:
                status = "failed"
                message = f"extract failed: {(err or out or 'extract.py failed').strip()[:220]}"
                log_step(repository, verbose, "extract failed")
            else:
                extracted_entries = extracted_skills
                unique_skill_paths = sorted({str(x.get("path", "")) for x in extracted_skills if str(x.get("path", ""))})
                skills_size_bytes = sum(folder_size_bytes(Path(p)) for p in unique_skill_paths)
                log_step(
                    repository,
                    verbose,
                    f"extracted {len(extracted_skills)} skills, total skills size {bytes_to_mb(skills_size_bytes)} MB",
                )

                max_skills_bytes = max(0, int(max_extracted_skills_mb)) * 1024 * 1024
                if max_skills_bytes > 0 and skills_size_bytes > max_skills_bytes:
                    for p in unique_skill_paths:
                        safe_rmtree(Path(p))
                    extracted_entries = []
                    status = "skills_space_limited"
                    message = (
                        f"skills size {bytes_to_mb(skills_size_bytes)}MB exceeded limit "
                        f"{max_extracted_skills_mb}MB; extracted folders deleted"
                    )
                    log_step(repository, verbose, "skills size limit exceeded; deleted extracted skill folders")
                else:
                    mp_map = task.marketplace_slug_map()
                    mp_meta_map = task.marketplace_slug_metadata_map()
                    matched_by_market: Dict[str, Set[str]] = {}
                    extracted_additional = 0
                    for sk in extracted_skills:
                        repo_skill_path = extracted_entry_storage_rel_path(root, sk)
                        if not repo_skill_path:
                            continue

                        matched_any_input = False
                        skill_registered = False
                        for marketplace, slugs in mp_map.items():
                            if not slugs:
                                # no slug claim from marketplace input -> keep only repo-level mapping
                                continue

                            for slug in sorted(slugs):
                                if skill_matches_slug(sk, task.owner, slug):
                                    matched_any_input = True
                                    matched_by_market.setdefault(marketplace, set()).add(slug)
                                    with db_lock:
                                        if not skill_registered:
                                            add_skill(
                                                conn,
                                                repository=repository,
                                                skill_path=repo_skill_path,
                                                skill_key=str(sk.get("skill_key", "") or Path(repo_skill_path).name),
                                                skill_dir=str(sk.get("skill_dir", "") or repo_skill_path),
                                                slug=str(sk.get("slug", "") or Path(repo_skill_path).name),
                                                repo_root=(str(sk.get("skill_dir", "")).strip() in {".", "./"} or str(sk.get("skill_key", "")).strip().lower() == "root"),
                                                skill_hash=(str(sk.get("skill_hash", "")).strip() or None),
                                                skill_last_modified_at=(str(sk.get("skill_last_modified_at", "")).strip() or None),
                                                extracted_at=now_iso(),
                                            )
                                            skill_registered = True
                                        add_skill_marketplace(
                                            conn,
                                            repository=repository,
                                            skill_path=repo_skill_path,
                                            marketplace=marketplace,
                                        )
                                        meta = mp_meta_map.get(marketplace, {}).get(slug, {})
                                        add_skill_input_metadata(
                                            conn,
                                            repository=repository,
                                            skill_path=input_skill_row_path(marketplace, slug),
                                            marketplace=marketplace,
                                            slug=slug,
                                            source=task.source,
                                            input_ref=task.input_ref,
                                            name=meta.get("name") if meta else None,
                                            author=((meta.get("author") if meta else None) or task.owner),
                                            stars=meta.get("stars") if meta else None,
                                            verified=meta.get("verified") if meta else None,
                                            tags_json=meta.get("tags_json") if meta else None,
                                            installs=meta.get("installs") if meta else None,
                                            input_skill_id=slug,
                                            input_skill_name=(meta.get("name") if meta else None),
                                            repo_downloaded=True,
                                            skill_downloaded=True,
                                            canonical_source=repository,
                                            canonical_skill_id=slug,
                                            canonical_repository=repository,
                                            canonical_skill_path=repo_skill_path,
                                            match_status="matched",
                                            match_reason=None,
                                            skill_hash=sk.get("skill_hash"),
                                            skill_last_modified_at=sk.get("skill_last_modified_at"),
                                        )
                        if not matched_any_input:
                            extracted_additional += 1
                            with db_lock:
                                add_name = additional_skill_name_from_extracted_entry(task.repo, sk, repo_skill_path)
                                add_rel = additional_skill_rel_path(task.owner, task.repo, add_name)
                                src_abs = Path(str(sk.get("path", "")).strip())
                                if not src_abs.is_absolute():
                                    src_abs = root / repo_skill_path
                                dst_abs = root / add_rel
                                if src_abs.exists() and src_abs.is_dir():
                                    move_folder_merge(src_abs, dst_abs)
                                add_additional_skill(
                                    conn,
                                    repository=repository,
                                    skill_name=add_name,
                                    skill_path=add_rel,
                                    skill_hash=(str(sk.get("skill_hash", "")).strip() or None),
                                    skill_last_modified_at=(str(sk.get("skill_last_modified_at", "")).strip() or None),
                                )
                                for row in task.rows:
                                    if row.marketplace:
                                        add_additional_skill_marketplace(
                                            conn,
                                            repository=repository,
                                            skill_path=add_rel,
                                            marketplace=row.marketplace,
                                        )
                    # Mark still-pending input rows for this repo as not found in extracted repo.
                    for marketplace, slugs in mp_map.items():
                        market_found_count += len(matched_by_market.get(marketplace, set()))
                        matched_slugs = matched_by_market.get(marketplace, set())
                        for slug in sorted(slugs):
                            if slug in matched_slugs:
                                continue
                            with db_lock:
                                add_skill_input_metadata(
                                    conn,
                                    repository=repository,
                                    skill_path=input_skill_row_path(marketplace, slug),
                                    marketplace=marketplace,
                                    slug=slug,
                                    source=task.source,
                                    input_ref=task.input_ref,
                                    author=task.owner,
                                    input_skill_id=slug,
                                    repo_downloaded=True,
                                    skill_downloaded=False,
                                    match_status="not_found",
                                    match_reason="input_skill_not_present_anymore",
                                )
                    if extracted_additional:
                        additional_found_count = extracted_additional
                        message = (message + " | " if message else "") + f"additional={extracted_additional}"
                    log_step(repository, verbose, "persisted extracted skills + marketplace mapping")

        log_step(repository, verbose, "remove .git directory")
        remove_git_dir(dest)

        if skip_store_repo:
            log_step(repository, verbose, "skip repo archive (performance mode)")
        else:
            max_zip_bytes = max(0, int(zip_max_repo_mb)) * 1024 * 1024
            zip_allowed = (max_zip_bytes == 0) or (repo_size_bytes <= max_zip_bytes)

            if status in {"ok", "duplicate_hash", "skills_space_limited"} and zip_allowed:
                try:
                    archive_path = str(zip_repo_folder(dest, zip_path))
                    log_step(repository, verbose, f"zipped repo -> {archive_path}")
                except Exception as e:
                    status = "failed"
                    message = f"zip failed: {type(e).__name__}: {e}"
                    log_step(repository, verbose, "zip failed")
            else:
                if status in {"ok", "duplicate_hash", "skills_space_limited"} and not zip_allowed:
                    large_repo = True
                    message = (
                        (message + " | ") if message else ""
                    ) + f"zip skipped: repo {bytes_to_mb(repo_size_bytes)}MB > limit {zip_max_repo_mb}MB"
                    log_step(repository, verbose, "zip skipped due repo size limit (large repo)")

        safe_rmtree(dest)
        log_step(repository, verbose, "deleted repo workspace folder")

    with db_lock:
        add_repo(
            conn,
            {
                "repository": repository,
                "owner": task.owner,
                "repo": task.repo,
                "stars": task.repo_stars(),
                "redirected_to": redirected_to_value,
                "market_skills_count": market_found_count,
                "additional_skills_count": additional_found_count,
                "source": task.source,
                "input_ref": None,
                "status": status,
                "message": message,
                "large_repo": large_repo,
                "duplicate_hash": duplicate_hash,
                "repo_hash": repo_hash,
                "repo_head_commit": repo_head_commit,
                "repo_head_commit_at": repo_head_commit_at,
                "archive_path": archive_path,
                "repo_size_bytes": repo_size_bytes,
                "skills_size_bytes": skills_size_bytes,
                "clone_started_at": clone_started,
                "clone_finished_at": clone_finished,
                "processed_at": now_iso(),
            },
        )
    mark_repo_downloaded_for_task(
        conn,
        db_lock,
        task,
        status in {"ok", "duplicate_hash", "skills_space_limited", "skipped_done", "ok_existing_skills"},
    )
    log_step(repository, verbose, f"db write complete (status={status})")

    return {
        "repository": repository,
        "status": status,
        "message": message,
        "large_repo": large_repo,
        "duplicate_hash": duplicate_hash,
        "repo_size_bytes": repo_size_bytes,
        "skills_size_bytes": skills_size_bytes,
        "extracted_entries": extracted_entries,
        "market_found": market_found_count,
        "additional_found": additional_found_count,
    }


# -----------------------------
# Main
# -----------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Stateful GitHub downloader with sqlite + marketplace mapping.")
    ap.add_argument("input", help="Input file (.txt, .json, .jsonl, .csv, .tsv, .db/.sqlite/.sqlite3)")
    ap.add_argument("--root", default="github", help="Root output folder (default: github)")
    ap.add_argument("--db", default="", help="SQLite path (default: <root>/state/sqlite.db)")
    ap.add_argument(
        "--recreate-db",
        action="store_true",
        help="Backup current DB to <root>/state/backup/<utc_timestamp>/ and create a fresh DB.",
    )
    ap.add_argument("--limit", type=int, default=0, help="Max repos to process (0 = all)")
    ap.add_argument("--jobs", type=int, default=1, help="Parallel workers (default: 1)")
    ap.add_argument("--overwrite", action="store_true", help="Re-clone even if already done")
    ap.add_argument("--filter-blobs", action="store_true", help="Use --filter=blob:none for clone")
    ap.add_argument("--single-branch", dest="single_branch", action="store_true", default=True, help="Use --single-branch for git clone (default: on)")
    ap.add_argument("--no-single-branch", dest="single_branch", action="store_false", help="Disable --single-branch")
    ap.add_argument("--clone-timeout-min", type=int, default=8, help="Clone timeout in minutes (default: 8)")
    ap.add_argument("--extract", action="store_true", help="Call extract.py and persist extracted skills")
    ap.add_argument(
        "--from-extracted-skills",
        action="store_true",
        help="Do not clone; reconcile input against existing <root>/skills/<owner>/<repo> folders.",
    )
    ap.add_argument(
        "--retry-no-skills",
        action="store_true",
        help="Reprocess done repos that currently have zero skill_downloaded=1 rows in input metadata.",
    )
    ap.add_argument(
        "--hash-source-db",
        default="",
        help="Optional previous sqlite db path used as fallback source for skill_hash in --from-extracted-skills mode.",
    )
    ap.add_argument(
        "--debug-no-match",
        action="store_true",
        help="Write per-repo debug rows for no_skills_matched into <root>/state/no_match_debug.jsonl.",
    )
    ap.add_argument(
        "--skip-store-repo",
        action="store_true",
        help="Performance mode: skip repo size calculation and zip archive storage.",
    )
    ap.add_argument("--zip-max-repo-mb", type=int, default=500, help="Zip repos only if repo size <= limit MB (0 = no limit)")
    ap.add_argument("--max-extracted-skills-mb", type=int, default=200, help="Max extracted skills size per repo in MB (0 = no limit)")
    ap.add_argument(
        "--recreate-marketplace",
        default="",
        help="Reset marketplace-specific state in DB for repos from current input, then re-add/reprocess.",
    )
    ap.add_argument("--testrun", action="store_true", help="Enable test run defaults (verbose + small limit if unset)")
    ap.add_argument("--verbose", action="store_true", help="Verbose logical step logging")

    ap.add_argument("--sqlite-table", default="github_repo_metadata", help="Input sqlite table name")
    ap.add_argument("--sqlite-repo-col", default="source", help="Input sqlite column containing owner/repo")
    ap.add_argument("--sqlite-where", default="", help="Optional WHERE clause for input sqlite")
    return ap


def main() -> None:
    args = build_parser().parse_args()
    ensure_git()

    if args.testrun:
        if args.limit <= 0:
            args.limit = 3
        args.verbose = True
        if args.jobs <= 0:
            args.jobs = 1

    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    db_path = Path(args.db) if args.db else root / "state" / "sqlite.db"
    if args.recreate_db:
        backup_dir = backup_and_recreate_db(db_path, root)
        if backup_dir:
            print(f"recreate_db: backed up previous db into {backup_dir}")
        else:
            print("recreate_db: no previous db found, creating fresh db")
    # Initialize schema once.
    init_conn = connect(db_path)
    init_conn.close()

    input_path = Path(args.input)
    rows = load_input(
        input_path,
        sqlite_table=args.sqlite_table,
        sqlite_repo_col=args.sqlite_repo_col,
        sqlite_where=args.sqlite_where,
    )
    tasks = aggregate_tasks(rows)

    if args.limit > 0:
        tasks = tasks[: max(0, args.limit)]

    total = len(tasks)
    if total == 0:
        print("Nothing to do (no valid repos found).")
        return

    if args.recreate_marketplace:
        print(f"recreate_marketplace={args.recreate_marketplace} (forcing overwrite=on)")
        args.overwrite = True

    extract_input = args.input if (args.extract and is_jsonl_file(input_path)) else ""
    jobs = max(1, int(args.jobs))

    print(f"Processing {total} repos with jobs={jobs}")
    print(f"root={root}")
    print(f"db={db_path}")
    print(f"extract={'on' if args.extract else 'off'}")
    print(f"from_extracted_skills={'on' if args.from_extracted_skills else 'off'}")
    print(f"retry_no_skills={'on' if args.retry_no_skills else 'off'}")
    print(f"hash_source_db={args.hash_source_db or '-'}")
    print(f"debug_no_match={'on' if args.debug_no_match else 'off'}")
    print(f"single_branch={'on' if args.single_branch else 'off'}")
    print(f"skip_store_repo={'on' if args.skip_store_repo else 'off'}")
    print(f"zip_max_repo_mb={args.zip_max_repo_mb}")
    print(f"max_extracted_skills_mb={args.max_extracted_skills_mb}")
    if args.testrun:
        print("testrun=on")

    counts = {
        "ok": 0,
        "duplicate_hash": 0,
        "large_repo": 0,
        "failed": 0,
        "skipped_auth": 0,
        "skipped_done": 0,
        "skills_space_limited": 0,
        "ok_existing_skills": 0,
        "missing_existing_skills": 0,
        "no_skills_matched": 0,
    }
    all_extracted_entries: List[Dict[str, Any]] = []
    db_lock = threading.Lock()
    hash_lookup = load_skill_hash_lookup(args.hash_source_db) if args.hash_source_db else {}

    init_seed_conn = connect(db_path)
    try:
        if args.recreate_marketplace:
            recreate_marketplace_state(init_seed_conn, db_lock, args.recreate_marketplace, tasks)
        preseed_input_rows(init_seed_conn, db_lock, tasks, args.overwrite)
    finally:
        init_seed_conn.close()

    def run_task(task: RepoTask) -> Dict[str, Any]:
        conn = connect(db_path)
        try:
            return process_repo(
                conn=conn,
                db_lock=db_lock,
                root=root,
                task=task,
                overwrite=args.overwrite,
                filter_blobs=args.filter_blobs,
                single_branch=args.single_branch,
                clone_timeout_minutes=args.clone_timeout_min,
                do_extract=args.extract,
                extract_input=extract_input,
                skip_store_repo=args.skip_store_repo,
                from_extracted_skills=args.from_extracted_skills,
                retry_no_skills=args.retry_no_skills,
                hash_lookup=hash_lookup,
                debug_no_match=args.debug_no_match,
                zip_max_repo_mb=args.zip_max_repo_mb,
                max_extracted_skills_mb=args.max_extracted_skills_mb,
                verbose=args.verbose,
            )
        finally:
            conn.close()

    done = 0
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        futs = [ex.submit(run_task, task) for task in tasks]
        for fut in as_completed(futs):
            try:
                result = fut.result()
            except Exception as e:
                done += 1
                counts["failed"] = counts.get("failed", 0) + 1
                print(f"[{done}/{total}] worker_failed -> failed | msg={type(e).__name__}: {e}")
                continue
            done += 1
            st = str(result.get("status", "failed"))
            counts[st] = counts.get(st, 0) + 1
            if args.extract:
                all_extracted_entries.extend(result.get("extracted_entries", []) or [])
            repo_mb_text = "n/a" if args.skip_store_repo else str(bytes_to_mb(int(result.get("repo_size_bytes") or 0)))
            print(
                f"[{done}/{total}] {result.get('repository')} -> {st} "
                f"| repo_mb={repo_mb_text} "
                f"| skills_mb={bytes_to_mb(int(result.get('skills_size_bytes') or 0))} "
                f"| market_found={int(result.get('market_found') or 0)} "
                f"| additional={int(result.get('additional_found') or 0)} "
                f"| msg={result.get('message', '')}"
            )

    cleanup_stats = {"hashes_with_dups": 0, "removed_folders": 0}
    if args.extract and all_extracted_entries:
        cleanup_stats = cleanup_duplicate_skill_folders(all_extracted_entries, args.verbose)
        print(
            "final_cleanup: "
            f"hash_groups_with_duplicates={cleanup_stats['hashes_with_dups']} "
            f"removed_folders={cleanup_stats['removed_folders']}"
        )

    print("\nDone.")
    print(f"ok:                   {counts.get('ok', 0)}")
    print(f"duplicate_hash:       {counts.get('duplicate_hash', 0)}")
    print(f"skills_space_limited: {counts.get('skills_space_limited', 0)}")
    print(f"large_repo:           {counts.get('large_repo', 0)}")
    print(f"skipped_auth:         {counts.get('skipped_auth', 0)}")
    print(f"skipped_done:         {counts.get('skipped_done', 0)}")
    print(f"ok_existing_skills:   {counts.get('ok_existing_skills', 0)}")
    print(f"missing_existing:     {counts.get('missing_existing_skills', 0)}")
    print(f"no_skills_matched:    {counts.get('no_skills_matched', 0)}")
    print(f"failed:               {counts.get('failed', 0)}")
    print(f"cleanup_removed:      {cleanup_stats.get('removed_folders', 0)}")
    print(f"sqlite:               {db_path}")


if __name__ == "__main__":
    main()
