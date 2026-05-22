#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import concurrent.futures
import os
import re
import runpy
import shutil
import sqlite3
import subprocess
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".kt", ".c", ".cc", ".cpp", ".h", ".hpp",
    ".cs", ".php", ".rb", ".swift", ".scala", ".sh", ".bash", ".zsh", ".ps1", ".lua", ".r", ".sql", ".yaml",
    ".yml", ".json", ".toml", ".ini", ".cfg", ".md", ".txt", ".dockerfile",
}
SKIP_DIRS = {".git", ".github", "node_modules", "vendor", "dist", "build", ".venv", "venv", "__pycache__"}


@dataclass
class SkillRow:
    skill_hash: str
    repository: str
    skill_path: str
    repo_rank: int = 0
    stars: int = 0
    priority_score: float = 0.0


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Clone flagged-skill repos and build full+stripped zip bundles")
    ap.add_argument(
        "--skills-input",
        default="skillfix/output/flagged_repo_bundles/inputs/flagged_skills_input.csv",
        help="CSV with columns: skill_hash,repository,skill_path",
    )
    ap.add_argument("--scan-db", default="", help="security_scan_with_questions.db (required if --skills-input is not provided)")
    ap.add_argument("--state-db", default="", help="sqlite.db with skills mapping (required if --skills-input is not provided)")
    ap.add_argument(
        "--repo-metadata-db",
        default="skillfix/output/flagged_repo_bundles/repo_metadata.db",
        help="repo metadata DB used to compute ranking when ranked CSV is not present",
    )
    ap.add_argument("--out-root", default="skillfix/output/flagged_repo_bundles")
    ap.add_argument("--llm-threshold", type=int, default=3)
    ap.add_argument("--run-id", type=int, default=None, help="question_run id; default latest")
    ap.add_argument("--max-code-files", type=int, default=30)
    ap.add_argument("--exclude-marketplaces", default="clawhub", help="comma-separated, matched from state skill_input_metadata.marketplace")
    ap.add_argument("--workers", type=int, default=8, help="number of parallel repo workers")
    ap.add_argument("--progress-every", type=int, default=10, help="print progress every N completed repos")
    ap.add_argument("--force-clone", action="store_true")
    ap.add_argument("--force-zip", action="store_true")
    ap.add_argument("--ranked-input", default="", help="Optional ranked CSV (e.g., flagged_skills_repo_context_ranked.csv)")
    ap.add_argument("--use-ranked-selection", action="store_true", help="Use ranked-input to keep top repos per skill and cap skills per repo")
    ap.add_argument("--rank-strategy", choices=("priority", "stars"), default="stars", help="Ranking strategy when deriving ranks from metadata DB")
    ap.add_argument("--max-repos-per-skill", type=int, default=3, help="When ranked selection is enabled, keep only top-N repos per skill_hash")
    ap.add_argument("--max-skills-per-repo", type=int, default=100, help="When ranked selection is enabled, keep at most N skill rows per repository")
    ap.add_argument(
        "--include-other-skill-folders",
        action="store_true",
        help="Include files from non-target skill folders in stripped zip fallback code files (default: excluded)",
    )
    return ap.parse_args()


def load_skills_input_csv(path: Path) -> List[SkillRow]:
    rows: List[SkillRow] = []
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            skill_hash = (row.get("skill_hash") or "").strip()
            repository = (row.get("repository") or "").strip()
            skill_path = (row.get("skill_path") or "").strip()
            if not repository or not skill_path:
                continue
            rows.append(SkillRow(skill_hash=skill_hash, repository=repository, skill_path=skill_path))
    return rows


def load_ranked_input_csv(path: Path) -> Dict[Tuple[str, str, str], Dict[str, object]]:
    out: Dict[Tuple[str, str, str], Dict[str, object]] = {}
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            skill_hash = str(row.get("skill_hash") or "").strip()
            repository = str(row.get("repository") or "").strip()
            skill_path = str(row.get("skill_path") or "").strip()
            if not skill_hash or not repository or not skill_path:
                continue
            try:
                repo_rank = int(str(row.get("repo_rank") or "0").strip() or "0")
            except Exception:
                repo_rank = 0
            try:
                stars = int(float(str(row.get("stars") or "0").strip() or "0"))
            except Exception:
                stars = 0
            try:
                priority_score = float(str(row.get("priority_score") or "0").strip() or "0")
            except Exception:
                priority_score = 0.0
            out[(skill_hash, repository, skill_path)] = {
                "repo_rank": repo_rank,
                "stars": stars,
                "priority_score": priority_score,
            }
    return out


def load_ranked_index_from_ranker(
    skills_input: Path,
    repo_metadata_db: Path,
    rank_strategy: str,
) -> Dict[Tuple[str, str, str], Dict[str, object]]:
    ranker_path = Path(__file__).resolve().parents[1] / "analyzers" / "rank_repo_context_inputs.py"
    mod = runpy.run_path(str(ranker_path))
    read_skills = mod.get("read_skills")
    load_repo_meta = mod.get("load_repo_meta")
    build_ranked = mod.get("build_ranked")
    if not callable(read_skills) or not callable(load_repo_meta) or not callable(build_ranked):
        raise RuntimeError("Could not load ranker module functions")

    skills = read_skills(skills_input)
    repo_meta = load_repo_meta(repo_metadata_db)
    ranked = build_ranked(skills, repo_meta, rank_strategy=rank_strategy)
    out: Dict[Tuple[str, str, str], Dict[str, object]] = {}
    for row in ranked:
        key = (
            str(row.get("skill_hash") or "").strip(),
            str(row.get("repository") or "").strip(),
            str(row.get("skill_path") or "").strip(),
        )
        if not all(key):
            continue
        out[key] = {
            "repo_rank": int(row.get("repo_rank") or 0),
            "stars": int(row.get("stars") or 0),
            "priority_score": float(row.get("priority_score") or 0.0),
        }
    return out


def apply_ranked_selection(
    rows: List[SkillRow],
    ranked_index: Dict[Tuple[str, str, str], Dict[str, object]],
    max_repos_per_skill: int,
    max_skills_per_repo: int,
) -> Tuple[List[SkillRow], Dict[str, int]]:
    selected: List[SkillRow] = []
    missing_rank = 0
    over_repo_rank = 0
    for r in rows:
        md = ranked_index.get((r.skill_hash, r.repository, r.skill_path))
        if not md:
            missing_rank += 1
            continue
        repo_rank = int(md.get("repo_rank", 0) or 0)
        if repo_rank <= 0 or repo_rank > max(1, max_repos_per_skill):
            over_repo_rank += 1
            continue
        selected.append(
            SkillRow(
                skill_hash=r.skill_hash,
                repository=r.repository,
                skill_path=r.skill_path,
                repo_rank=repo_rank,
                stars=int(md.get("stars", 0) or 0),
                priority_score=float(md.get("priority_score", 0.0) or 0.0),
            )
        )

    by_repo: Dict[str, List[SkillRow]] = {}
    for r in selected:
        by_repo.setdefault(r.repository, []).append(r)

    capped: List[SkillRow] = []
    repo_capped_out = 0
    for repo, items in by_repo.items():
        items.sort(key=lambda x: (x.repo_rank, -x.stars, -x.priority_score, x.skill_hash, x.skill_path))
        kept = items[: max(1, max_skills_per_repo)]
        capped.extend(kept)
        if len(items) > len(kept):
            repo_capped_out += len(items) - len(kept)

    stats = {
        "missing_rank_rows": missing_rank,
        "over_repo_rank_rows": over_repo_rank,
        "repo_cap_dropped_rows": repo_capped_out,
    }
    return capped, stats


def normalize_hash(hash_with_prefix: str) -> str:
    # Expected forms: 01_<hash>, 010_<hash>, <hash>
    m = re.match(r"^\d{2,3}_(.+)$", hash_with_prefix or "")
    return m.group(1) if m else (hash_with_prefix or "")


def latest_run_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(run_id) FROM question_skill_results").fetchone()
    if not row or row[0] is None:
        raise RuntimeError("question_skill_results is empty")
    return int(row[0])


def load_strict_flagged_hashes(scan_db: Path, run_id: Optional[int], llm_threshold: int) -> Set[str]:
    conn = sqlite3.connect(str(scan_db))
    conn.row_factory = sqlite3.Row
    try:
        rid = run_id if run_id is not None else latest_run_id(conn)
        q = """
            SELECT q.hash_with_prefix
            FROM question_skill_results q
            JOIN scan_results s ON s.hash_with_prefix = q.hash_with_prefix
            WHERE q.run_id = ?
              AND COALESCE(q.overal_malicousness_rating, 0) > ?
              AND s.is_safe = 0
        """
        hashes = {normalize_hash(r["hash_with_prefix"]) for r in conn.execute(q, (rid, llm_threshold))}
        return {h for h in hashes if h}
    finally:
        conn.close()


def load_skill_rows(state_db: Path, flagged_hashes: Set[str], exclude_marketplaces: Set[str]) -> List[SkillRow]:
    if not flagged_hashes:
        return []
    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    try:
        rows: List[SkillRow] = []
        chunk = 900
        hashes = list(flagged_hashes)
        for i in range(0, len(hashes), chunk):
            part = hashes[i:i + chunk]
            placeholders = ",".join("?" for _ in part)
            if exclude_marketplaces:
                q = f"""
                    SELECT DISTINCT s.skill_hash, s.repository, s.skill_path
                    FROM skills s
                    LEFT JOIN skill_input_metadata sim
                      ON sim.repository = s.repository AND sim.skill_path = s.skill_path
                    WHERE s.skill_hash IN ({placeholders})
                      AND (
                        sim.marketplace IS NULL
                        OR lower(sim.marketplace) NOT IN ({','.join('?' for _ in exclude_marketplaces)})
                      )
                """
                params: Sequence[str] = [*part, *sorted(exclude_marketplaces)]
            else:
                q = f"""
                    SELECT DISTINCT s.skill_hash, s.repository, s.skill_path
                    FROM skills s
                    WHERE s.skill_hash IN ({placeholders})
                """
                params = part
            for r in conn.execute(q, params):
                rows.append(SkillRow(skill_hash=r["skill_hash"], repository=r["repository"], skill_path=r["skill_path"]))
        return rows
    finally:
        conn.close()


def looks_like_auth_issue(stderr: str, stdout: str) -> bool:
    text = f"{stderr or ''}\n{stdout or ''}".lower()
    patterns = [
        "username for 'https://github.com'",
        "could not read username for",
        "authentication failed",
        "support for password authentication was removed",
        "repository not found",
        "fatal: could not read from remote repository",
    ]
    return any(p in text for p in patterns)


def ensure_repo(repo: str, repos_dir: Path, force: bool) -> Tuple[bool, str, Path]:
    repo_key = repo.replace("/", "__")
    target = repos_dir / repo_key
    if target.exists() and (target / ".git").exists() and not force:
        return True, "existing", target
    if target.exists() and force:
        subprocess.run(["rm", "-rf", str(target)], check=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{repo}.git"
    cmd = ["git", "clone", "--depth", "1", url, str(target)]
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "echo"
    cp = subprocess.run(cmd, text=True, capture_output=True, stdin=subprocess.DEVNULL, env=env)
    if cp.returncode != 0:
        if looks_like_auth_issue(cp.stderr, cp.stdout):
            return False, "skipped_auth", target
        return False, (cp.stderr or cp.stdout or "clone_failed").strip()[:400], target
    return True, "cloned", target


def safe_is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def safe_is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def safe_is_symlink(path: Path) -> bool:
    try:
        return path.is_symlink()
    except OSError:
        return False


def zip_repo(repo_dir: Path, zip_path: Path, force: bool) -> str:
    if zip_path.exists() and not force:
        return "existing"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in repo_dir.rglob("*"):
            if safe_is_symlink(p):
                continue
            if not safe_is_file(p):
                continue
            try:
                rel = p.relative_to(repo_dir).as_posix()
            except Exception:
                continue
            if rel == ".git" or rel.startswith(".git/"):
                continue
            try:
                zf.write(p, arcname=rel)
            except OSError:
                continue
    return "created"


def find_main_readme(repo_dir: Path) -> Optional[Path]:
    candidates = [p for p in repo_dir.iterdir() if p.is_file() and p.name.lower().startswith("readme")]
    if not candidates:
        return None
    candidates.sort(key=lambda p: len(p.name))
    return candidates[0]


def is_text_file(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            sample = f.read(4096)
        return b"\x00" not in sample
    except Exception:
        return False


def iter_code_files(repo_dir: Path, skip_prefixes: Set[str], max_files: int) -> List[Path]:
    found: List[Path] = []
    for p in repo_dir.rglob("*"):
        if len(found) >= max_files:
            break
        if safe_is_symlink(p):
            continue
        if not safe_is_file(p):
            continue
        try:
            rel = p.relative_to(repo_dir).as_posix()
        except Exception:
            continue
        parts = rel.split("/")
        if any(part in SKIP_DIRS for part in parts):
            continue
        if any(rel == s or rel.startswith(s + "/") for s in skip_prefixes):
            continue
        ext = p.suffix.lower()
        if ext not in CODE_EXTENSIONS and p.name.lower() != "dockerfile":
            continue
        if not is_text_file(p):
            continue
        found.append(p)
    return found


def safe_name(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    return s[:180]


def iter_child_dirs(parent: Path) -> List[Path]:
    out: List[Path] = []
    try:
        with os.scandir(parent) as it:
            for ent in it:
                try:
                    if ent.is_dir(follow_symlinks=False):
                        out.append(parent / ent.name)
                except OSError:
                    continue
    except OSError:
        return []
    return out


def is_skill_directory(path: Path) -> bool:
    if not safe_is_dir(path):
        return False
    return safe_is_file(path / "SKILL.md") or safe_is_file(path / "skill.md")


def find_skill_file_in_dir(path: Path) -> Optional[Path]:
    preferred = ("SKILL.md", "skill.md", "README.md", "readme.md")
    for name in preferred:
        p = path / name
        if safe_is_file(p):
            return p
    try:
        md_files = [p for p in path.iterdir() if safe_is_file(p) and p.suffix.lower() == ".md"]
    except OSError:
        return None
    if not md_files:
        return None
    md_files.sort(key=lambda p: len(p.name))
    return md_files[0]


def resolve_skill_file(repo_dir: Path, repository: str, skill_path: str) -> Optional[Path]:
    raw = (skill_path or "").strip().strip("/")
    if not raw:
        return None

    def maybe_file(rel: str) -> Optional[Path]:
        p = repo_dir / rel
        if safe_is_file(p):
            return p
        if p.exists() and p.is_dir():
            return find_skill_file_in_dir(p)
        return None

    candidates: List[str] = []
    candidates.append(raw)
    candidates.append(raw.lstrip("./"))

    parts = [p for p in raw.split("/") if p]
    repo_parts = repository.split("/", 1)
    if len(repo_parts) == 2:
        owner, repo_name = repo_parts
        if len(parts) >= 4 and parts[0] in {"skills", "skills_additional"} and parts[1] == owner and parts[2] == repo_name:
            tail = "/".join(parts[3:])
            if tail:
                candidates.append(tail)
                for root in ("skills", ".claude/skills", ".codex/skills", ".cursor/skills", ".agents/skills"):
                    candidates.append(f"{root}/{tail}")

    seen: Set[str] = set()
    ordered_candidates: List[str] = []
    for c in candidates:
        c = c.strip().strip("/")
        if not c or c in seen:
            continue
        seen.add(c)
        ordered_candidates.append(c)

    for rel in ordered_candidates:
        resolved = maybe_file(rel)
        if resolved is not None:
            return resolved
    return None


def stripped_zip_for_skill(
    repo_dir: Path,
    repository: str,
    skill_hash: str,
    skill_path: str,
    stripped_dir: Path,
    max_code_files: int,
    force: bool,
    include_other_skill_folders: bool,
) -> Tuple[str, Optional[Path], str, str]:
    skill_file = resolve_skill_file(repo_dir=repo_dir, repository=repository, skill_path=skill_path)
    if skill_file is None:
        return "missing_skill_file", None, f"skill not found: {skill_path}", ""
    try:
        skill_rel = skill_file.relative_to(repo_dir).as_posix()
    except Exception:
        return "missing_skill_file", None, f"skill not under repo root: {skill_path}", ""
    repo_key = repository.replace("/", "__")
    hash_slug = safe_name(skill_hash or "nohash")
    path_slug = safe_name(skill_path)
    out_zip = stripped_dir / f"{repo_key}__{hash_slug}__{path_slug}.zip"
    if out_zip.exists() and not force:
        return "existing", out_zip, "", skill_rel

    out_zip.parent.mkdir(parents=True, exist_ok=True)
    if out_zip.exists():
        out_zip.unlink()

    skill_dir_rel = Path(skill_rel).parent.as_posix()

    include_paths: Set[Path] = set()
    include_dir_entries: Set[str] = set()
    readme = find_main_readme(repo_dir)
    if readme:
        include_paths.add(readme)
    include_paths.add(skill_file)

    raw = (skill_path or "").strip().strip("/")
    repo_parts = repository.split("/", 1)
    candidate_skill_dirs: Set[str] = {skill_dir_rel}
    if raw:
        raw_dir = Path(raw).parent.as_posix() if raw.lower().endswith(".md") else Path(raw).as_posix()
        if raw_dir and raw_dir != ".":
            candidate_skill_dirs.add(raw_dir)
        if len(repo_parts) == 2:
            owner, repo_name = repo_parts
            parts = [p for p in raw.split("/") if p]
            if len(parts) >= 4 and parts[0] in {"skills", "skills_additional"} and parts[1] == owner and parts[2] == repo_name:
                tail = "/".join(parts[3:])
                if tail:
                    candidate_skill_dirs.add(tail)
                    for root in ("skills", ".claude/skills", ".codex/skills", ".cursor/skills", ".agents/skills"):
                        candidate_skill_dirs.add(f"{root}/{tail}")

    scripts_rels: Set[str] = set()
    for base in candidate_skill_dirs:
        scripts_rel = (Path(base) / "scripts").as_posix() if base != "." else "scripts"
        scripts_dir = repo_dir / scripts_rel
        if scripts_dir.exists() and scripts_dir.is_dir():
            scripts_rels.add(scripts_rel)
            include_dir_entries.add(scripts_rel.rstrip("/") + "/")
            for p in scripts_dir.rglob("*"):
                if safe_is_symlink(p):
                    continue
                if safe_is_file(p) and is_text_file(p):
                    include_paths.add(p)

    skip_prefixes = {skill_rel}
    for scripts_rel in scripts_rels:
        skip_prefixes.add(scripts_rel)

    forbidden_skill_prefixes: Set[str] = set()
    if not include_other_skill_folders:
        # Exclude sibling skill directories that look like independent skills.
        target_dir = repo_dir / skill_dir_rel
        parent_dir = target_dir.parent if safe_is_dir(target_dir.parent) else repo_dir
        siblings = iter_child_dirs(parent_dir)
        normalized_targets = {d.strip().strip("/") for d in candidate_skill_dirs if d.strip().strip("/")}
        for sib in siblings:
            try:
                sib_rel = sib.relative_to(repo_dir).as_posix().strip("/")
            except Exception:
                continue
            if sib_rel in normalized_targets:
                continue
            if any(t.startswith(sib_rel + "/") for t in normalized_targets):
                continue
            if is_skill_directory(sib):
                skip_prefixes.add(sib_rel)
                forbidden_skill_prefixes.add(sib_rel)

        known_roots = ("skills", ".claude/skills", ".codex/skills", ".cursor/skills", ".agents/skills")
        for root in known_roots:
            root_dir = repo_dir / root
            if not safe_is_dir(root_dir):
                continue
            for child in iter_child_dirs(root_dir):
                try:
                    child_rel = child.relative_to(repo_dir).as_posix().strip("/")
                except Exception:
                    continue
                # Keep target skill folder(s); exclude other skill folders.
                if child_rel in normalized_targets:
                    continue
                if any(t.startswith(child_rel + "/") for t in normalized_targets):
                    continue
                skip_prefixes.add(child_rel)
                forbidden_skill_prefixes.add(child_rel)

    for p in iter_code_files(repo_dir, skip_prefixes=skip_prefixes, max_files=max_code_files):
        include_paths.add(p)

    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for d in sorted(include_dir_entries):
            if not include_other_skill_folders and any(
                d.rstrip("/") == fp or d.rstrip("/").startswith(fp + "/") for fp in forbidden_skill_prefixes
            ):
                continue
            zf.writestr(d, "")
        for p in sorted(include_paths):
            try:
                rel = p.relative_to(repo_dir).as_posix()
            except Exception:
                continue
            if not include_other_skill_folders and rel.lower().endswith("/skill.md") and rel != skill_rel:
                continue
            if not include_other_skill_folders and any(rel == fp or rel.startswith(fp + "/") for fp in forbidden_skill_prefixes):
                continue
            if safe_is_symlink(p):
                continue
            if not safe_is_file(p):
                continue
            try:
                zf.write(p, arcname=rel)
            except OSError:
                continue

    return "created", out_zip, "", skill_rel


def write_csv(path: Path, rows: Iterable[Dict[str, object]], header: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(header))
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main() -> None:
    args = parse_args()
    out_root = Path(args.out_root).resolve()
    repos_dir = out_root / "repos"
    repo_zips_dir = out_root / "repo_zips"
    stripped_dir = out_root / "stripped_skill_zips"
    manifests_dir = out_root / "manifests"

    t0 = time.time()
    if args.skills_input:
        skills_input = Path(args.skills_input).resolve()
        if not skills_input.exists():
            raise SystemExit(f"skills input not found: {skills_input}")
        skill_rows = load_skills_input_csv(skills_input)
        flagged_hashes = {s.skill_hash for s in skill_rows if s.skill_hash}
        print(f"Using precomputed skills input: {skills_input}")
    else:
        if not args.scan_db or not args.state_db:
            raise SystemExit("Provide either --skills-input or both --scan-db and --state-db")
        scan_db = Path(args.scan_db).resolve()
        state_db = Path(args.state_db).resolve()
        exclude_markets = {m.strip().lower() for m in args.exclude_marketplaces.split(",") if m.strip()}
        flagged_hashes = load_strict_flagged_hashes(scan_db, args.run_id, args.llm_threshold)
        skill_rows = load_skill_rows(state_db, flagged_hashes, exclude_markets)

    if args.use_ranked_selection:
        ranked_index: Dict[Tuple[str, str, str], Dict[str, object]]
        ranked_input = Path(args.ranked_input).resolve() if args.ranked_input else None
        if ranked_input is not None and ranked_input.exists():
            ranked_index = load_ranked_input_csv(ranked_input)
            print(f"Ranked selection input: {ranked_input}")
        else:
            repo_metadata_db = Path(args.repo_metadata_db).resolve()
            if not repo_metadata_db.exists():
                if ranked_input is not None:
                    raise SystemExit(
                        f"ranked input not found: {ranked_input} and repo metadata DB not found: {repo_metadata_db}"
                    )
                raise SystemExit(f"--use-ranked-selection needs ranked CSV or repo metadata DB: {repo_metadata_db}")
            ranked_index = load_ranked_index_from_ranker(
                skills_input=skills_input,
                repo_metadata_db=repo_metadata_db,
                rank_strategy=args.rank_strategy,
            )
            print(f"Ranked selection derived from metadata DB: {repo_metadata_db} (strategy={args.rank_strategy})")
        original_n = len(skill_rows)
        skill_rows, rank_stats = apply_ranked_selection(
            skill_rows,
            ranked_index=ranked_index,
            max_repos_per_skill=max(1, args.max_repos_per_skill),
            max_skills_per_repo=max(1, args.max_skills_per_repo),
        )
        print(f"Ranked selection kept rows: {len(skill_rows)}/{original_n}")
        print(f"Ranked missing rows: {rank_stats.get('missing_rank_rows', 0)}")
        print(f"Ranked over max-repos-per-skill rows: {rank_stats.get('over_repo_rank_rows', 0)}")
        print(f"Ranked dropped by max-skills-per-repo: {rank_stats.get('repo_cap_dropped_rows', 0)}")
        print(f"Ranked caps -> max_repos_per_skill={max(1, args.max_repos_per_skill)}, max_skills_per_repo={max(1, args.max_skills_per_repo)}")

    by_repo: Dict[str, List[SkillRow]] = {}
    for s in skill_rows:
        by_repo.setdefault(s.repository, []).append(s)

    print(f"Strict flagged hashes: {len(flagged_hashes)}")
    print(f"Mapped flagged skill rows: {len(skill_rows)}")
    print(f"Unique repositories: {len(by_repo)}")
    print(f"Workers: {max(1, args.workers)} | Progress every: {max(1, args.progress_every)}")

    repo_manifest: List[Dict[str, object]] = []
    skill_manifest: List[Dict[str, object]] = []

    def process_repo(item: Tuple[str, List[SkillRow]]) -> Tuple[Dict[str, object], List[Dict[str, object]]]:
        repo, skills = item
        ok, clone_status, repo_dir = ensure_repo(repo, repos_dir, force=args.force_clone)
        repo_key = repo.replace("/", "__")
        full_zip = repo_zips_dir / f"{repo_key}.zip"
        zip_status = "skipped"
        if ok:
            zip_status = zip_repo(repo_dir, full_zip, force=args.force_zip)

        repo_row = {
            "repository": repo,
            "repo_dir": str(repo_dir),
            "clone_ok": int(ok),
            "clone_status": clone_status,
            "full_zip": str(full_zip) if ok else "",
            "full_zip_status": zip_status,
            "flagged_skill_count": len(skills),
        }
        skill_rows_out: List[Dict[str, object]] = []

        for s in skills:
            stripped_status = "skipped"
            stripped_zip = ""
            note = ""
            resolved_skill_path = ""
            if ok:
                st, zp, nt, resolved = stripped_zip_for_skill(
                    repo_dir=repo_dir,
                    repository=s.repository,
                    skill_hash=s.skill_hash,
                    skill_path=s.skill_path,
                    stripped_dir=stripped_dir,
                    max_code_files=args.max_code_files,
                    force=args.force_zip,
                    include_other_skill_folders=args.include_other_skill_folders,
                )
                stripped_status, note = st, nt
                stripped_zip = str(zp) if zp else ""
                resolved_skill_path = resolved or ""
                if stripped_zip and s.repo_rank == 1:
                    top_dir = out_root / "top_ranked"
                    top_dir.mkdir(parents=True, exist_ok=True)
                    dst = top_dir / Path(stripped_zip).name
                    if not dst.exists() or args.force_zip:
                        shutil.copy2(stripped_zip, dst)
                elif stripped_zip and 2 <= s.repo_rank <= 3:
                    two_three_dir = out_root / "2_3_ranked"
                    two_three_dir.mkdir(parents=True, exist_ok=True)
                    dst = two_three_dir / Path(stripped_zip).name
                    if not dst.exists() or args.force_zip:
                        shutil.copy2(stripped_zip, dst)

            skill_rows_out.append(
                {
                    "skill_hash": s.skill_hash,
                    "repository": s.repository,
                    "skill_path": s.skill_path,
                    "repo_rank": s.repo_rank,
                    "stars": s.stars,
                    "priority_score": s.priority_score,
                    "resolved_skill_path": resolved_skill_path,
                    "clone_ok": int(ok),
                    "clone_status": clone_status,
                    "full_zip": str(full_zip) if ok else "",
                    "full_zip_status": zip_status,
                    "stripped_zip": stripped_zip,
                    "stripped_zip_status": stripped_status,
                    "note": note,
                }
            )
        return repo_row, skill_rows_out

    items = sorted(by_repo.items())
    processed = 0
    progress_every = max(1, args.progress_every)
    worker_count = max(1, args.workers)
    if worker_count == 1:
        for item in items:
            repo_row, skill_rows_out = process_repo(item)
            repo_manifest.append(repo_row)
            skill_manifest.extend(skill_rows_out)
            processed += 1
            if processed % progress_every == 0 or processed == len(items):
                print(f"Progress repos: {processed}/{len(items)}")
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as ex:
            futures = [ex.submit(process_repo, item) for item in items]
            for fut in concurrent.futures.as_completed(futures):
                repo_row, skill_rows_out = fut.result()
                repo_manifest.append(repo_row)
                skill_manifest.extend(skill_rows_out)
                processed += 1
                if processed % progress_every == 0 or processed == len(items):
                    print(f"Progress repos: {processed}/{len(items)}")

    write_csv(
        manifests_dir / "flagged_repo_manifest.csv",
        repo_manifest,
        [
            "repository", "repo_dir", "clone_ok", "clone_status", "full_zip", "full_zip_status", "flagged_skill_count",
        ],
    )
    write_csv(
        manifests_dir / "flagged_skill_manifest.csv",
        skill_manifest,
        [
            "skill_hash", "repository", "skill_path", "repo_rank", "stars", "priority_score", "resolved_skill_path", "clone_ok", "clone_status", "full_zip", "full_zip_status",
            "stripped_zip", "stripped_zip_status", "note",
        ],
    )

    elapsed = round(time.time() - t0, 2)
    print(f"Done in {elapsed}s")
    print(f"Repo manifest: {manifests_dir / 'flagged_repo_manifest.csv'}")
    print(f"Skill manifest: {manifests_dir / 'flagged_skill_manifest.csv'}")


if __name__ == "__main__":
    main()
