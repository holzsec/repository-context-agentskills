#!/usr/bin/env python3
"""
Marketplace-aware skill extractor + copier with hashing + global deduplication.

Changes implemented per your latest request
------------------------------------------
- If *any* skill in a repo causes `git log` to hit the 60s fallback (or times out at 60s),
  we assume the repo creation date for **all** skills in that repo for `skill_last_modified_at`.
- Repo creation date is taken as the first commit date: `git log --reverse -1 --format=%cI`
  (best effort; may be None).
- If SKILL.md is at repo root, we copy the full repo excluding .git/ (and hash excluding .git/).
- Prints to stdout the "input slug" (raw from JSONL if available; otherwise derived).

Stdout line format
------------------
SLUG<TAB>owner/repo<TAB>raw_slug_or_derived<TAB>category<TAB>skill_dir<TAB>skill_key
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

GIT = shutil.which("git")
if not GIT:
    raise RuntimeError("git not found in PATH (from Python process). Try exporting PATH or set GIT=/usr/bin/git.")

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
    timeout: int = 300,
) -> subprocess.CompletedProcess:
    if verbose:
        print("+", " ".join(cmd))
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def ensure_git() -> None:
    cp = run([GIT, "--version"])
    if cp.returncode != 0:
        raise RuntimeError("git not found in PATH")


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def safe_rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def is_git_repo_dir(path: Path) -> bool:
    return (path / ".git").is_dir()


def is_jsonl_file(path: Path) -> bool:
    return path.suffix.lower() == ".jsonl"


def norm_token(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def safe_fs_name(name: str) -> str:
    name = (name or "").strip().replace("\\", "/").strip("/")
    if not name:
        return "root"
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name)
    name = re.sub(r"-{2,}", "-", name).strip("-")
    return name or "root"


def print_slug_line(
    repo_key: str,
    slug_raw_or_derived: str,
    category: str,
    skill_dir: str,
    skill_key: str,
) -> None:
    print(
        "SLUG\t"
        + (repo_key or "")
        + "\t"
        + (slug_raw_or_derived or "")
        + "\t"
        + (category or "")
        + "\t"
        + (skill_dir or "")
        + "\t"
        + (skill_key or ""),
        flush=True,
    )


# -----------------------------
# Git helpers
# -----------------------------
def git_out(repo_dir: Path, args: List[str], *, timeout: int = 300) -> Optional[str]:
    cp = run([GIT, "-C", str(repo_dir), *args], timeout=timeout)
    if cp.returncode != 0:
        return None
    return (cp.stdout or "").strip() or None


def git_head_info(repo_dir: Path) -> Dict[str, Any]:
    return {
        "repo_head_commit": git_out(repo_dir, ["rev-parse", "HEAD"]) or None,
        "repo_head_commit_at": git_out(repo_dir, ["log", "-1", "--format=%cI"]) or None,
    }


def git_repo_creation_date(repo_dir: Path) -> Optional[str]:
    """
    Best-effort repo creation date = first commit date (committer date, ISO 8601).
    """
    # keep timeouts conservative
    try:
        out = git_out(repo_dir, ["log", "--reverse", "-1", "--format=%cI"], timeout=60)
        return out or None
    except subprocess.TimeoutExpired:
        return None


def git_last_change_of_path(repo_dir: Path, rel_path: str) -> Tuple[Optional[str], bool]:
    """
    Returns (date_iso_or_None, used_60s_fallback_or_timed_out_60s)
    - Try normally (300s). If it fails/returns empty or times out, retry with 60s.
    - If we ever need the 60s retry (or 60s times out), flag=True.
    """
    rel = (rel_path or "").strip().lstrip("/")
    if not rel:
        return None, False

    try:
        out = git_out(repo_dir, ["log", "-1", "--format=%cI", "--", rel], timeout=300)
        if out:
            return out, False
    except subprocess.TimeoutExpired:
        pass

    # Fallback: 60 seconds
    try:
        out2 = git_out(repo_dir, ["log", "-1", "--format=%cI", "--", rel], timeout=60)
        return (out2 or None), True
    except subprocess.TimeoutExpired:
        return None, True


def find_skill_md_paths(repo_dir: Path) -> List[str]:
    cp = run([GIT, "-C", str(repo_dir), "ls-files"])
    if cp.returncode != 0:
        return []
    out: List[str] = []
    for line in (cp.stdout or "").splitlines():
        p = line.strip()
        if not p:
            continue
        if p == "SKILL.md" or p.endswith("/SKILL.md"):
            out.append(p)
    return sorted(set(out))


# -----------------------------
# Marketplace JSONL parsing
# -----------------------------
def load_market_repo_slugs_from_jsonl(path: Path) -> Dict[str, List[str]]:
    """
    JSONL lines: {repository: "owner/repo", slug: "username-skill-name"}
    Returns: repo_lower -> list of raw slugs (deduped, order preserved)
    """
    repo_to_slugs: Dict[str, List[str]] = {}
    seen = set()

    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                print(f"warn: invalid JSON at {path}:{lineno}", file=sys.stderr)
                continue
            if not isinstance(obj, dict):
                continue

            repo = str(obj.get("repository", "")).strip()
            slug = str(obj.get("slug", "")).strip()
            if not repo or not slug:
                continue

            key = repo.lower()
            pair = (key, slug)
            if pair in seen:
                continue
            seen.add(pair)

            repo_to_slugs.setdefault(key, []).append(slug)

    return repo_to_slugs

def normalize_slug_for_repo(raw_slug: str, owner: str) -> str:
    slug = (raw_slug or "").strip()
    owner = (owner or "").strip()
    prefix = owner + "-"
    if slug.lower().startswith(prefix.lower()):
        return slug[len(prefix):]
    return slug

# -----------------------------
# Skill naming / matching
# -----------------------------
def skill_dir_from_skill_md(skill_md: str) -> str:
    rel_dir = str(Path(skill_md).parent).replace("\\", "/")
    return "." if rel_dir == "." else rel_dir


def skill_key_from_dir(rel_dir: str) -> str:
    rel_dir = (rel_dir or "").strip().replace("\\", "/")
    if rel_dir in (".", "", "./"):
        return "root"
    return Path(rel_dir).name
def match_slug_to_skill_key(owner: str, raw_slug: str, skill_keys: List[str]) -> Optional[str]:
    """
    Returns the best-matching skill_key for a given slug (or None).
    Rule: slug_clean contains skill_key (normalized). Prefer the longest skill_key match (more specific).
    """
    clean = normalize_slug_for_repo(raw_slug, owner)
    ns = norm_token(clean)
    best: Optional[str] = None
    best_len = -1
    for k in skill_keys:
        nk = norm_token(k)
        if nk and nk in ns:
            if len(nk) > best_len:
                best = k
                best_len = len(nk)
    return best

def choose_matching_slug_for_skill_with_raw(owner: str, slugs_raw: List[str], skill_key: str) -> Optional[Tuple[str, str]]:
    sk = norm_token(skill_key)
    if not sk:
        return None

    matches: List[Tuple[str, str]] = []
    for raw in slugs_raw:
        clean = normalize_slug_for_repo(raw, owner)
        if sk in norm_token(clean):
            matches.append((raw, clean))

    if not matches:
        return None

    matches.sort(key=lambda rc: (len(rc[1]), rc[1]))
    return matches[0]


# -----------------------------
# Hashing
# -----------------------------
def hash_folder_excluding_git(folder: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(folder.rglob("*")):
        if ".git" in p.parts:
            continue
        if p.is_file():
            rel = str(p.relative_to(folder)).replace("\\", "/")
            h.update(rel.encode("utf-8"))
            h.update(b"\0")
            with p.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            h.update(b"\0")
    return h.hexdigest()


# -----------------------------
# Copy helpers
# -----------------------------
def copy_tree_excluding_git(src_dir: Path, dst_dir: Path) -> None:
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        src_dir,
        dst_dir,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".git"),
    )


# -----------------------------
# Paths
# -----------------------------
def market_dest_dir(root: Path, owner: str, repo: str, name: str) -> Path:
    return root / "skills" / owner / repo / name


def additional_dest_dir(root: Path, owner: str, repo: str, name: str) -> Path:
    return root / "skills_additional" / owner / repo / name


# -----------------------------
# Repo listing
# -----------------------------
def list_repos_under_root(root: Path) -> List[Tuple[str, str, Path]]:
    rr = root / "repo"
    repos: List[Tuple[str, str, Path]] = []
    if not rr.exists():
        return repos
    for owner_dir in sorted([p for p in rr.iterdir() if p.is_dir()]):
        for repo_dir in sorted([p for p in owner_dir.iterdir() if p.is_dir()]):
            if not is_git_repo_dir(repo_dir):
                continue
            repos.append((owner_dir.name, repo_dir.name, repo_dir))
    return repos


# -----------------------------
# Models
# -----------------------------
@dataclass
class MarketCandidate:
    repository: str
    owner: str
    repo: str
    repo_path: str
    skill_md: str
    skill_dir: str
    skill_key: str
    slug_raw: str
    slug_clean: str
    dest_name: str
    skill_hash: str
    skill_last_modified_at: Optional[str]
    repo_head_commit: Optional[str]
    repo_head_commit_at: Optional[str]
    extracted_at: str


# -----------------------------
# Worker
# -----------------------------
def process_repo(
    root: Path,
    owner: str,
    repo: str,
    repo_dir: Path,
    *,
    market_mode: bool,
    slugs_raw: Optional[List[str]],
    verbose: bool,
) -> Tuple[List[MarketCandidate], List[Dict[str, Any]], int, int]:
    """
    Simplified behavior:
    - COPY immediately (in parallel worker), do NOT suppress duplicates.
    - Still compute skill_hash so duplicates can be flagged later in a report.
    - No "additional" handling: additional_entries is always [] and additional_count is 0.

    Marketplace validation (market_mode=True):
    - For each slug in slugs_raw, try to find a SKILL.md skill_key whose normalized token is contained
      in the cleaned slug token.
    - If found, we copy that skill folder immediately to skills/<owner>/<repo>/<clean_slug>/.

    Non-market mode (market_mode=False):
    - Treat all SKILL.md as market; copy immediately to skills/<owner>/<repo>/<skill_key>/.
    """

    def match_slug_to_best_skill_key(raw_slug: str, skill_keys: List[str]) -> Optional[str]:
        clean = normalize_slug_for_repo(raw_slug, owner)
        ns = norm_token(clean)
        best_key: Optional[str] = None
        best_len = -1
        for k in skill_keys:
            nk = norm_token(k)
            if nk and nk in ns:
                if len(nk) > best_len:
                    best_key = k
                    best_len = len(nk)
        return best_key

    repo_key = f"{owner}/{repo}"
    if verbose:
        print(f"[scan] {repo_key}")

    head = git_head_info(repo_dir)
    repo_created_at = git_repo_creation_date(repo_dir)
    skill_md_paths = find_skill_md_paths(repo_dir)

    # Build skill_key -> (skill_md, rel_dir)
    skill_by_key: Dict[str, Tuple[str, str]] = {}
    skill_keys: List[str] = []
    for skill_md in skill_md_paths:
        rel_dir = skill_dir_from_skill_md(skill_md)
        skill_key = skill_key_from_dir(rel_dir)
        if skill_key not in skill_by_key:
            skill_by_key[skill_key] = (skill_md, rel_dir)
            skill_keys.append(skill_key)

    # Determine if any SKILL.md path triggers 60s fallback; if yes use repo_created_at for all skills in this repo
    repo_slow_gitlog = False
    skill_last_by_md: Dict[str, Optional[str]] = {}
    for skill_md in skill_md_paths:
        skill_last, used_60s = git_last_change_of_path(repo_dir, skill_md)
        skill_last_by_md[skill_md] = skill_last
        if used_60s:
            repo_slow_gitlog = True

    market_candidates: List[MarketCandidate] = []
    additional_entries: List[Dict[str, Any]] = []  # intentionally unused
    used_skill_keys: set[str] = set()  # prevent copying same skill twice if multiple slugs map to it

    if not market_mode:
        # Copy ALL SKILL.md as market immediately
        for skill_key in skill_keys:
            skill_md, rel_dir = skill_by_key[skill_key]
            skill_last = skill_last_by_md.get(skill_md)
            if repo_slow_gitlog:
                skill_last = repo_created_at

            # Hash excluding .git
            try:
                if rel_dir == ".":
                    skill_hash = hash_folder_excluding_git(repo_dir)
                else:
                    skill_hash = hash_folder_excluding_git(repo_dir / rel_dir)
            except Exception as e:
                skill_hash = f"NOHASH:{repo_key.lower()}:{skill_md}:{type(e).__name__}"

            dest_name = safe_fs_name(skill_key) or "root"
            out_dir = market_dest_dir(root, owner, repo, dest_name)

            entry = MarketCandidate(
                repository=repo_key,
                owner=owner,
                repo=repo,
                repo_path=str(repo_dir),
                skill_md=skill_md,
                skill_dir=rel_dir,
                skill_key=skill_key,
                slug_raw=f"{owner}-{skill_key}",
                slug_clean="",
                dest_name=dest_name,
                skill_hash=skill_hash,
                skill_last_modified_at=skill_last,
                repo_head_commit=head.get("repo_head_commit"),
                repo_head_commit_at=head.get("repo_head_commit_at"),
                extracted_at=now_iso(),
            )

            try:
                if rel_dir == ".":
                    copy_tree_excluding_git(repo_dir, out_dir)
                else:
                    copy_tree_excluding_git(repo_dir / rel_dir, out_dir)
                print_slug_line(repo_key, entry.slug_raw, "market", rel_dir, skill_key)
            except Exception as e:
                # store copy error in a way main() can include later (if you serialize dataclass)
                # easiest: attach attribute dynamically
                setattr(entry, "copy_error", f"{type(e).__name__}: {e}")

            market_candidates.append(entry)

        return market_candidates, additional_entries, len(market_candidates), 0

    # --- marketplace mode: copy only "found/valid" market skills ---
    for raw_slug in (slugs_raw or []):
        best_skill_key = match_slug_to_best_skill_key(raw_slug, skill_keys)
        if not best_skill_key:
            continue  # marketplace slug missing in repo (invalid)
        if best_skill_key in used_skill_keys:
            continue
        used_skill_keys.add(best_skill_key)

        skill_md, rel_dir = skill_by_key[best_skill_key]
        skill_last = skill_last_by_md.get(skill_md)
        if repo_slow_gitlog:
            skill_last = repo_created_at

        clean_slug = normalize_slug_for_repo(raw_slug, owner)

        # Hash excluding .git
        try:
            if rel_dir == ".":
                skill_hash = hash_folder_excluding_git(repo_dir)
            else:
                skill_hash = hash_folder_excluding_git(repo_dir / rel_dir)
        except Exception as e:
            skill_hash = f"NOHASH:{repo_key.lower()}:{skill_md}:{type(e).__name__}"

        dest_name = safe_fs_name(clean_slug) or "root"
        out_dir = market_dest_dir(root, owner, repo, dest_name)

        entry = MarketCandidate(
            repository=repo_key,
            owner=owner,
            repo=repo,
            repo_path=str(repo_dir),
            skill_md=skill_md,
            skill_dir=rel_dir,
            skill_key=best_skill_key,
            slug_raw=raw_slug,
            slug_clean=clean_slug,
            dest_name=dest_name,
            skill_hash=skill_hash,
            skill_last_modified_at=skill_last,
            repo_head_commit=head.get("repo_head_commit"),
            repo_head_commit_at=head.get("repo_head_commit_at"),
            extracted_at=now_iso(),
        )

        try:
            if rel_dir == ".":
                copy_tree_excluding_git(repo_dir, out_dir)
            else:
                copy_tree_excluding_git(repo_dir / rel_dir, out_dir)
            print_slug_line(repo_key, raw_slug, "market", rel_dir, best_skill_key)
        except Exception as e:
            setattr(entry, "copy_error", f"{type(e).__name__}: {e}")

        market_candidates.append(entry)

    return market_candidates, additional_entries, len(market_candidates), 0

# -----------------------------
# Main
# -----------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Extract marketplace skills and flag duplicates."
    )
    ap.add_argument(
        "--root",
        default="github",
        help="Root folder (default: github).",
    )
    ap.add_argument(
        "--input",
        default="",
        help="Marketplace JSONL input file.",
    )
    ap.add_argument(
        "--recreate",
        action="store_true",
        help="Delete and recreate output folders before extraction.",
    )
    ap.add_argument(
        "--jobs",
        type=int,
        default=8,
        help="Parallel repo workers (default: 8).",
    )
    ap.add_argument(
        "--repo-path",
        default="",
        help="Optional single repo path to process (e.g. <root>/repo/<owner>/<repo>).",
    )
    ap.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose output.",
    )
    return ap

def main() -> None:
    args = build_parser().parse_args()
    ensure_git()

    root = Path(args.root)

    # Determine marketplace mode
    market_mode = False
    market_repo_to_slugs: Dict[str, List[str]] = {}
    if args.input:
        inp = Path(args.input)
        if inp.exists() and is_jsonl_file(inp):
            market_mode = True
            market_repo_to_slugs = load_market_repo_slugs_from_jsonl(inp)

    skills_dir = root / "skills"
    skills_add_dir = root / "skills_additional"  # kept for compatibility; not used in this simplified flow
    if args.recreate:
        safe_rmtree(skills_dir)
        safe_rmtree(skills_add_dir)

    skills_dir.mkdir(parents=True, exist_ok=True)
    skills_add_dir.mkdir(parents=True, exist_ok=True)

    repos = list_repos_under_root(root)
    if args.repo_path:
        rp = Path(args.repo_path)
        owner = rp.parent.name
        repo = rp.name
        if not rp.exists() or not is_git_repo_dir(rp):
            print(f"warn: --repo-path is not a git repo dir: {rp}", file=sys.stderr)
            return
        repos = [(owner, repo, rp)]
    if not repos:
        print(f"No repos found under {root / 'repo'}")
        return

    # Parallel scan + COPY happens inside process_repo now
    max_workers = max(1, int(args.jobs))
    all_candidates: List[MarketCandidate] = []
    additional_all: List[Dict[str, Any]] = []  # will remain empty in simplified process_repo

    repo_skill_count: Dict[str, int] = {}
    repo_skill_count_additional: Dict[str, int] = {}

    print(f"Scanning {len(repos)} repos (jobs={max_workers}) market_mode={market_mode}")
    print("Note: copying happens during scan; duplicates are only flagged later.\n")

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {}
        for owner, repo, repo_dir in repos:
            repo_key = f"{owner}/{repo}".lower()
            slugs_raw = market_repo_to_slugs.get(repo_key) if market_mode else None
            fut = ex.submit(
                process_repo,
                root,
                owner,
                repo,
                repo_dir,
                market_mode=market_mode,
                slugs_raw=slugs_raw,
                verbose=args.verbose,
            )
            futs[fut] = (owner, repo)

        done = 0
        for fut in as_completed(futs):
            owner, repo = futs[fut]
            repo_key = f"{owner}/{repo}".lower()
            done += 1
            try:
                candidates, additional_entries, mc, ac = fut.result()
            except Exception as e:
                print(f"warn: failed {owner}/{repo}: {type(e).__name__}: {e}", file=sys.stderr)
                candidates, additional_entries, mc, ac = [], [], 0, 0

            all_candidates.extend(candidates)
            additional_all.extend(additional_entries)
            repo_skill_count[repo_key] = mc
            repo_skill_count_additional[repo_key] = ac

            if not args.verbose:
                pct = done / len(repos) * 100.0
                print(f"[{done}/{len(repos)}] {pct:6.2f}% {owner}/{repo} market_found={mc} additional={ac}")

    # ---- Build state: skills.json = ALL extracted market candidates (not canonical-only) ----
    skills_state: List[Dict[str, Any]] = []
    repo_skill_unique_count: Dict[str, int] = {}  # here: unique by hash per repo (informational)
    for c in all_candidates:
        repo_key_lower = c.repository.lower()
        out_dir = market_dest_dir(root, c.owner, c.repo, c.dest_name)

        entry: Dict[str, Any] = {
            "repository": c.repository,
            "repo_path": c.repo_path,
            "skill_md": c.skill_md,
            "skill_dir": c.skill_dir,
            "skill_key": c.skill_key,
            "slug": c.slug_clean if market_mode else None,
            "slug_raw": c.slug_raw,
            "path": str(out_dir),
            "skill_hash": c.skill_hash,
            "skill_last_modified_at": c.skill_last_modified_at,
            "repo_head_commit": c.repo_head_commit,
            "repo_head_commit_at": c.repo_head_commit_at,
            "extracted_at": c.extracted_at,
        }

        # process_repo may attach a dynamic copy_error attribute
        if hasattr(c, "copy_error"):
            entry["copy_error"] = getattr(c, "copy_error")

        skills_state.append(entry)

    # ---- Duplicates report (flag later) ----
    by_hash: Dict[str, List[MarketCandidate]] = {}
    for c in all_candidates:
        by_hash.setdefault(c.skill_hash, []).append(c)

    duplicates_json: List[Dict[str, Any]] = []
    dup_total = 0

    for h in sorted(by_hash.keys()):
        group = by_hash[h]
        if len(group) <= 1:
            continue
        dup_total += (len(group) - 1)

        # canonical for reporting: deterministic (repo/name order)
        group_sorted = sorted(group, key=lambda x: (x.repository.lower(), x.dest_name, x.skill_dir, x.skill_md))
        canon = group_sorted[0]
        dups = group_sorted[1:]

        # unique repo list
        seen_repo = set()
        repos_unique: List[str] = []
        for x in group_sorted:
            rl = x.repository.lower()
            if rl in seen_repo:
                continue
            seen_repo.add(rl)
            repos_unique.append(x.repository)

        duplicates_json.append(
            {
                "skill_hash": h,
                "skill": canon.dest_name,
                "repositories": repos_unique,
                "canonical": {
                    "repository": canon.repository,
                    "repo_path": canon.repo_path,
                    "skill_md": canon.skill_md,
                    "skill_dir": canon.skill_dir,
                    "slug": canon.slug_clean if market_mode else None,
                    "slug_raw": canon.slug_raw,
                    "path": str(market_dest_dir(root, canon.owner, canon.repo, canon.dest_name)),
                },
                "duplicate_count": len(dups),
                "duplicates": [
                    {
                        "repository": d.repository,
                        "repo_path": d.repo_path,
                        "skill_md": d.skill_md,
                        "skill_dir": d.skill_dir,
                        "slug": d.slug_clean if market_mode else None,
                        "slug_raw": d.slug_raw,
                        "path": str(market_dest_dir(root, d.owner, d.repo, d.dest_name)),
                    }
                    for d in dups
                ],
            }
        )

    # ---- Per-repo "unique by hash" counts (after-the-fact) ----
    # informational only; does NOT affect copying
    seen_by_repo_hash: Dict[str, set[str]] = {}
    for c in all_candidates:
        rk = c.repository.lower()
        seen_by_repo_hash.setdefault(rk, set()).add(c.skill_hash)
    for rk, hs in seen_by_repo_hash.items():
        repo_skill_unique_count[rk] = len(hs)

    # ---- Write state ----
    state_dir = root / "state"
    atomic_write_json(state_dir / "skills.json", skills_state)
    atomic_write_json(state_dir / "skills_additional.json", [])  # unused now
    atomic_write_json(state_dir / "skills_duplicate.json", duplicates_json)
    atomic_write_json(state_dir / "repo_skill_count.json", repo_skill_count)
    atomic_write_json(state_dir / "repo_skill_unique_count.json", repo_skill_unique_count)
    atomic_write_json(state_dir / "repo_skill_count_additional.json", {})

    print("\nDone.")
    print(f"market_mode: {market_mode} ({'jsonl input used' if market_mode else 'no input/ignored'})")
    print(f"market skills extracted+copied: {len(all_candidates)}")
    print(f"duplicate copies flagged later: {dup_total} (see state/skills_duplicate.json)")
    print(f"skills/ dir: {skills_dir}")
    print(f"state/ dir: {state_dir}")

main()
