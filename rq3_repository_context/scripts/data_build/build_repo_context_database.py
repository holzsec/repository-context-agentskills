#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shutil
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_repo(repo: str) -> str:
    return str(repo or "").strip().strip("/").lower()


def normalize_skill_hash(skill_hash: str) -> str:
    return str(skill_hash or "").strip().lower()


def normalize_match_label(v: object) -> str:
    s = str(v or "").strip().lower()
    if s in {"high", "h"}:
        return "high"
    if s in {"medium", "med", "m"}:
        return "medium"
    if s in {"low", "l"}:
        return "low"
    return ""


def label_to_points(label: str) -> float:
    if label == "high":
        return 1.0
    if label == "medium":
        return 0.5
    return 0.0


def calc_weighted_context_score(domain: str, code: str, readme: str) -> Optional[float]:
    d = normalize_match_label(domain)
    c = normalize_match_label(code)
    r = normalize_match_label(readme)
    if not d or not c or not r:
        return None
    return round(100.0 * (0.40 * label_to_points(d) + 0.35 * label_to_points(c) + 0.25 * label_to_points(r)), 2)


def to_mb_from_size_fields(gh_size_kb: Optional[int], state_repo_size_bytes: Optional[int]) -> Optional[float]:
    if state_repo_size_bytes is not None and int(state_repo_size_bytes) > 0:
        return round(float(state_repo_size_bytes) / (1024.0 * 1024.0), 3)
    if gh_size_kb is not None and int(gh_size_kb) > 0:
        return round(float(gh_size_kb) / 1024.0, 3)
    return None


def bucket_size_mb(mb: Optional[float]) -> str:
    if mb is None:
        return "unknown"
    if mb < 10:
        return "small"
    if mb < 100:
        return "medium"
    if mb < 500:
        return "large"
    return "very_large"


def bucket_age(created_at: Optional[str]) -> str:
    if not created_at:
        return "unknown"
    try:
        s = str(created_at).replace("Z", "+00:00")
        created = datetime.fromisoformat(s)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
    except Exception:
        return "unknown"
    age_days = (datetime.now(timezone.utc) - created.astimezone(timezone.utc)).days
    if age_days < 30:
        return "<1_month"
    if age_days <= 365:
        return "1_month_to_1_year"
    return ">1_year"


def bucket_activity(pushed_at: Optional[str]) -> str:
    if not pushed_at:
        return "unknown"
    try:
        s = str(pushed_at).replace("Z", "+00:00")
        pushed = datetime.fromisoformat(s)
        if pushed.tzinfo is None:
            pushed = pushed.replace(tzinfo=timezone.utc)
    except Exception:
        return "unknown"
    days = (datetime.now(timezone.utc) - pushed.astimezone(timezone.utc)).days
    if days <= 30:
        return "<=30d"
    if days <= 180:
        return "31d_to_180d"
    return ">180d"


def bucket_popularity(v: Optional[int]) -> str:
    x = int(v or 0)
    if x <= 0:
        return "0"
    if x < 100:
        return "gt_0_to_99"
    if x < 1000:
        return "gt_100_to_999"
    return ">=1000"


def metadata_trust_score(
    age_bucket: str,
    stars_b: str,
    forks_b: str,
    issues_b: str,
    repo_size_bucket: str,
    activity_bucket: str,
    skill_repo_count: int,
) -> float:
    # Weighted metadata scoring:
    # - ordinal bucket -> unit interval [0,1]
    # - weighted mean with missing-feature renormalization
    # - optional prevalence regularizer for skills that appear in multiple repositories
    weights = {
        "stars": 0.35,
        "age": 0.20,
        "size": 0.20,
        "forks": 0.10,
        "issues": 0.10,
        "activity": 0.05,
    }

    def four_level(b: str) -> Optional[float]:
        m = {
            "0": 0.0,
            "small": 0.0,
            "gt_0_to_99": 1.0 / 3.0,
            "medium": 1.0 / 3.0,
            "gt_100_to_999": 2.0 / 3.0,
            "large": 2.0 / 3.0,
            ">=1000": 1.0,
            "very_large": 1.0,
        }
        return m.get(str(b or "").strip())

    def three_level(b: str) -> Optional[float]:
        m = {
            "<1_month": 0.0,
            ">180d": 0.0,
            "1_month_to_1_year": 0.5,
            "31d_to_180d": 0.5,
            ">1_year": 1.0,
            "<=30d": 1.0,
        }
        return m.get(str(b or "").strip())

    vals = {
        "stars": four_level(stars_b),
        "age": three_level(age_bucket),
        "size": four_level(repo_size_bucket),
        "forks": four_level(forks_b),
        "issues": four_level(issues_b),
        "activity": three_level(activity_bucket),
    }

    w_sum = 0.0
    s_sum = 0.0
    for k, w in weights.items():
        v = vals.get(k)
        if v is None or not math.isfinite(v):
            continue
        s_sum += w * float(v)
        w_sum += w
    if w_sum <= 0.0:
        base = 0.0
    else:
        base = s_sum / w_sum

    # Smooth prevalence contribution in [0,1), capped by design (no linear point inflation).
    k = max(0, int(skill_repo_count or 0))
    prevalence = 1.0 - math.exp(-float(k) / 3.0)
    score_unit = 0.90 * base + 0.10 * prevalence
    return round(100.0 * min(1.0, max(0.0, score_unit)), 2)


def verdict_from_score(score: Optional[float]) -> str:
    if score is None:
        return "unknown"
    if score >= 70:
        return "likely_false_positive"
    if score >= 40:
        return "uncertain"
    return "likely_valid_flag"


def suspicious_repo_penalty(codex_final_verdict: Optional[str]) -> float:
    s = str(codex_final_verdict or "").strip().lower()
    if s in {
        "aligned_but_repo_suspicious",
        "align_but_repo_suspicous",
        "aligned_but_repo_suspicous",
        "align_but_repo_suspicious",
    }:
        return 50.0
    return 0.0


def parse_int(v: object) -> Optional[int]:
    s = str(v or "").strip()
    if s == "":
        return None
    try:
        return int(float(s))
    except Exception:
        return None


def parse_float(v: object) -> Optional[float]:
    s = str(v or "").strip()
    if s == "":
        return None
    try:
        return float(s)
    except Exception:
        return None


def parse_repo_skillname(value: str) -> Tuple[str, str]:
    s = str(value or "").strip()
    if not s:
        return "", ""
    m = re.match(r"^(.*?)__(.*?)__([0-9a-fA-F]{64})__", s)
    if not m:
        return "", ""
    owner = m.group(1).strip()
    repo = m.group(2).strip()
    skill_hash = m.group(3).strip().lower()
    repository = f"{owner}/{repo}" if owner and repo else ""
    return normalize_repo(repository), normalize_skill_hash(skill_hash)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Build final DB with separate repo-metadata and codex tables, plus merged repo_context and dashboard"
    )
    ap.add_argument("--skills-input", default="skillfix/output/flagged_repo_bundles/inputs/flagged_skills_input.csv")
    ap.add_argument("--repo-metadata-db", default="skillfix/output/flagged_repo_bundles/repo_metadata.db")
    ap.add_argument("--codex-jsonl", default="skillfix/output/flagged_repo_bundles/codex_repo_check/repository_context_checks.jsonl")
    ap.add_argument(
        "--out-merged-csv",
        default="skillfix/output/flagged_repo_bundles/skillfix_scores.csv",
        help="Optional merged CSV output for inspection",
    )
    ap.add_argument("--base-db", default="data/security_scan_with_questions.db")
    ap.add_argument("--out-db", default="data/security_scan_with_questions_repo_context.db")
    ap.add_argument("--dashboard-dir", default="skillfix/dashboard")
    ap.add_argument("--overwrite", action="store_true")
    return ap.parse_args()


def prepare_db(base_db: Path, out_db: Path, overwrite: bool) -> sqlite3.Connection:
    out_db.parent.mkdir(parents=True, exist_ok=True)
    if out_db.exists():
        if not overwrite:
            raise SystemExit(f"Output DB already exists: {out_db}. Use --overwrite.")
        out_db.unlink()
    if base_db.exists():
        shutil.copy2(base_db, out_db)
    conn = sqlite3.connect(str(out_db))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def load_skill_paths(skills_input: Path) -> Dict[Tuple[str, str], str]:
    out: Dict[Tuple[str, str], str] = {}
    with skills_input.open("r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            h = normalize_skill_hash(row.get("skill_hash", ""))
            repo = normalize_repo(row.get("repository", ""))
            p = str(row.get("skill_path", "")).strip()
            if not h or not repo:
                continue
            key = (h, repo)
            if key not in out:
                out[key] = p
    return out


def load_repo_metadata(repo_metadata_db: Path) -> Dict[str, Dict[str, object]]:
    conn = sqlite3.connect(str(repo_metadata_db))
    conn.row_factory = sqlite3.Row
    out: Dict[str, Dict[str, object]] = {}
    try:
        for r in conn.execute(
            """
            SELECT repository, gh_created_at, gh_pushed_at, gh_size_kb,
                   gh_stars, gh_forks, gh_open_issues, state_repo_size_bytes
            FROM repo_metadata
            """
        ):
            repo = normalize_repo(str(r["repository"] or ""))
            if not repo:
                continue
            gh_size_kb = parse_int(r["gh_size_kb"])
            gh_stars = parse_int(r["gh_stars"]) or 0
            gh_forks = parse_int(r["gh_forks"]) or 0
            gh_open_issues = parse_int(r["gh_open_issues"]) or 0
            state_repo_size_bytes = parse_int(r["state_repo_size_bytes"])
            repo_size_mb = to_mb_from_size_fields(gh_size_kb, state_repo_size_bytes)
            repo_size_bucket = bucket_size_mb(repo_size_mb)
            repo_age_bucket = bucket_age(r["gh_created_at"])
            repo_activity_bucket = bucket_activity(r["gh_pushed_at"])
            stars_bucket = bucket_popularity(gh_stars)
            forks_bucket = bucket_popularity(gh_forks)
            issues_bucket = bucket_popularity(gh_open_issues)
            repo_metadata_score = metadata_trust_score(
                repo_age_bucket,
                stars_bucket,
                forks_bucket,
                issues_bucket,
                repo_size_bucket,
                repo_activity_bucket,
                skill_repo_count=0,
            )
            out[repo] = {
                "repository": repo,
                "gh_created_at": str(r["gh_created_at"] or "").strip() or None,
                "gh_pushed_at": str(r["gh_pushed_at"] or "").strip() or None,
                "gh_size_kb": gh_size_kb,
                "gh_stars": gh_stars,
                "gh_forks": gh_forks,
                "gh_open_issues": gh_open_issues,
                "state_repo_size_bytes": state_repo_size_bytes,
                "repo_size_mb": repo_size_mb,
                "repo_size_bucket": repo_size_bucket,
                "repo_age_bucket": repo_age_bucket,
                "repo_activity_bucket": repo_activity_bucket,
                "stars_bucket": stars_bucket,
                "forks_bucket": forks_bucket,
                "gh_open_issues_bucket": issues_bucket,
                "repo_metadata_score": repo_metadata_score,
            }
    finally:
        conn.close()
    return out


def load_codex_rows(codex_jsonl: Path, skill_paths: Dict[Tuple[str, str], str]) -> List[Dict[str, object]]:
    out: List[Dict[str, object]] = []
    with codex_jsonl.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            s = line.strip()
            if not s:
                continue
            try:
                row = json.loads(s)
            except Exception:
                continue

            repo = normalize_repo(str(row.get("repository") or ""))
            skill_hash = normalize_skill_hash(str(row.get("skill_hash") or ""))
            if not repo or not skill_hash:
                repo2, hash2 = parse_repo_skillname(str(row.get("repo_skillname") or ""))
                repo = repo or repo2
                skill_hash = skill_hash or hash2
            if not repo or not skill_hash:
                continue

            domain = normalize_match_label(row.get("domain_match"))
            code = normalize_match_label(row.get("code_match"))
            readme = normalize_match_label(row.get("readme_match"))
            codex_score = parse_float(row.get("codex_score"))
            if codex_score is None:
                codex_score = calc_weighted_context_score(domain, code, readme)
            if codex_score is None:
                continue

            out.append(
                {
                    "source_line_no": line_no,
                    "repo_skillname": str(row.get("repo_skillname") or "").strip() or None,
                    "skill_hash": skill_hash,
                    "repository": repo,
                    "skill_path": skill_paths.get((skill_hash, repo), ""),
                    "codex_domain_match": domain or None,
                    "codex_domain_reason": str(row.get("domain_reason") or "").strip() or None,
                    "codex_code_match": code or None,
                    "codex_code_reason": str(row.get("code_reason") or "").strip() or None,
                    "codex_readme_match": readme or None,
                    "codex_readme_reason": str(row.get("readme_reason") or "").strip() or None,
                    "codex_repo_maliciousness": str(row.get("repo_maliciousness") or "").strip() or None,
                    "codex_repo_maliciousness_reason": str(row.get("repo_maliciousness_reason") or "").strip() or None,
                    "codex_final_verdict": str(row.get("final_verdict") or "").strip() or None,
                    "codex_confidence": str(row.get("confidence") or "").strip() or None,
                    "codex_risk_note": str(row.get("risk_note") or "").strip() or None,
                    "codex_score": float(codex_score),
                }
            )
    return out


def merge_rows(
    codex_rows: List[Dict[str, object]],
    repo_meta: Dict[str, Dict[str, object]],
    skill_marketplaces: Dict[str, Set[str]],
) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    ts = now_utc()
    repos_per_skill: Dict[str, Set[str]] = defaultdict(set)
    for c in codex_rows:
        h = normalize_skill_hash(str(c.get("skill_hash") or ""))
        r = normalize_repo(str(c.get("repository") or ""))
        if h and r:
            repos_per_skill[h].add(r)

    for c in codex_rows:
        repo = str(c["repository"])
        skill_hash = str(c["skill_hash"])
        skill_repo_count = len(repos_per_skill.get(normalize_skill_hash(skill_hash), set()))
        m = repo_meta.get(repo)
        markets = sorted(list(skill_marketplaces.get(normalize_skill_hash(skill_hash), set())))
        market_count = len(markets)
        final_score = None
        penalty_applied = 0.0
        final_verdict = "unknown"
        is_fp = 0
        fp_status = "not_false_positive"

        if m:
            repo_metadata_score = metadata_trust_score(
                str(m.get("repo_age_bucket") or "unknown"),
                str(m.get("stars_bucket") or "0"),
                str(m.get("forks_bucket") or "0"),
                str(m.get("gh_open_issues_bucket") or "0"),
                str(m.get("repo_size_bucket") or "unknown"),
                str(m.get("repo_activity_bucket") or "unknown"),
                skill_repo_count=skill_repo_count,
            )
            # Use metadata adjusted with skill repository count contribution.
            base_score = 0.7 * float(c["codex_score"]) + 0.3 * float(repo_metadata_score)
            penalty_applied = suspicious_repo_penalty(c.get("codex_final_verdict"))
            final_score = round(max(0.0, base_score - penalty_applied), 2)
            final_verdict = verdict_from_score(final_score)
            is_fp = 1 if final_verdict == "likely_false_positive" else 0
            fp_status = "false_positive" if is_fp else "not_false_positive"

        rows.append(
            {
                "loaded_at": ts,
                "source_line_no": c["source_line_no"],
                "repo_skillname": c["repo_skillname"],
                "skill_hash": skill_hash,
                "repository": repo,
                "skill_path": c.get("skill_path") or "",
                "skill_present_on_marketplaces": 1 if market_count > 0 else 0,
                "skill_marketplaces_count": market_count,
                "skill_marketplaces_csv": ",".join(markets) if markets else None,
                "skill_present_on_skills_sh": 1 if "skills_sh" in markets else 0,
                "codex_domain_match": c.get("codex_domain_match"),
                "codex_domain_reason": c.get("codex_domain_reason"),
                "codex_code_match": c.get("codex_code_match"),
                "codex_code_reason": c.get("codex_code_reason"),
                "codex_readme_match": c.get("codex_readme_match"),
                "codex_readme_reason": c.get("codex_readme_reason"),
                "codex_repo_maliciousness": c.get("codex_repo_maliciousness"),
                "codex_repo_maliciousness_reason": c.get("codex_repo_maliciousness_reason"),
                "codex_final_verdict": c.get("codex_final_verdict"),
                "codex_confidence": c.get("codex_confidence"),
                "codex_risk_note": c.get("codex_risk_note"),
                "codex_score": c["codex_score"],
                "gh_created_at": m.get("gh_created_at") if m else None,
                "gh_pushed_at": m.get("gh_pushed_at") if m else None,
                "gh_size_kb": m.get("gh_size_kb") if m else None,
                "gh_stars": m.get("gh_stars") if m else None,
                "gh_forks": m.get("gh_forks") if m else None,
                "gh_open_issues": m.get("gh_open_issues") if m else None,
                "state_repo_size_bytes": m.get("state_repo_size_bytes") if m else None,
                "repo_size_mb": m.get("repo_size_mb") if m else None,
                "repo_size_bucket": m.get("repo_size_bucket") if m else None,
                "repo_age_bucket": m.get("repo_age_bucket") if m else None,
                "repo_activity_bucket": m.get("repo_activity_bucket") if m else None,
                "stars_bucket": m.get("stars_bucket") if m else None,
                "forks_bucket": m.get("forks_bucket") if m else None,
                "gh_open_issues_bucket": m.get("gh_open_issues_bucket") if m else None,
                "skill_repository_count": skill_repo_count,
                "repo_metadata_score": repo_metadata_score if m else None,
                "penalty_applied": penalty_applied,
                "repository_context_score": final_score,
                "final_score": final_score,
                "final_verdict": final_verdict,
                "is_false_positive": is_fp,
                "false_positive_status": fp_status,
            }
        )
    return rows


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS repo_context;
        DROP TABLE IF EXISTS repo_context_repo_metadata;
        DROP TABLE IF EXISTS repo_context_skill_codex;

        CREATE TABLE repo_context_repo_metadata (
            repository TEXT PRIMARY KEY,
            loaded_at TEXT NOT NULL,
            gh_created_at TEXT,
            gh_pushed_at TEXT,
            gh_size_kb INTEGER,
            gh_stars INTEGER,
            gh_forks INTEGER,
            gh_open_issues INTEGER,
            state_repo_size_bytes INTEGER,
            repo_size_mb REAL,
            repo_size_bucket TEXT,
            repo_age_bucket TEXT,
            repo_activity_bucket TEXT,
            stars_bucket TEXT,
            forks_bucket TEXT,
            gh_open_issues_bucket TEXT,
            repo_metadata_score REAL
        );

        CREATE TABLE repo_context_skill_codex (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            loaded_at TEXT NOT NULL,
            source_line_no INTEGER,
            repo_skillname TEXT,
            skill_hash TEXT NOT NULL,
            repository TEXT NOT NULL,
            skill_path TEXT,
            codex_domain_match TEXT,
            codex_domain_reason TEXT,
            codex_code_match TEXT,
            codex_code_reason TEXT,
            codex_readme_match TEXT,
            codex_readme_reason TEXT,
            codex_repo_maliciousness TEXT,
            codex_repo_maliciousness_reason TEXT,
            codex_final_verdict TEXT,
            codex_confidence TEXT,
            codex_risk_note TEXT,
            codex_score REAL NOT NULL
        );

        CREATE INDEX idx_rctx_codex_skill_repo ON repo_context_skill_codex(skill_hash, repository);
        CREATE INDEX idx_rctx_codex_repo ON repo_context_skill_codex(repository);

        CREATE TABLE repo_context (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            loaded_at TEXT NOT NULL,
            source_line_no INTEGER,
            repo_skillname TEXT,
            skill_hash TEXT NOT NULL,
            repository TEXT NOT NULL,
            skill_path TEXT,
            skill_present_on_marketplaces INTEGER NOT NULL DEFAULT 0,
            skill_marketplaces_count INTEGER NOT NULL DEFAULT 0,
            skill_marketplaces_csv TEXT,
            skill_present_on_skills_sh INTEGER NOT NULL DEFAULT 0,
            codex_domain_match TEXT,
            codex_domain_reason TEXT,
            codex_code_match TEXT,
            codex_code_reason TEXT,
            codex_readme_match TEXT,
            codex_readme_reason TEXT,
            codex_repo_maliciousness TEXT,
            codex_repo_maliciousness_reason TEXT,
            codex_final_verdict TEXT,
            codex_confidence TEXT,
            codex_risk_note TEXT,
            codex_score REAL NOT NULL,
            gh_created_at TEXT,
            gh_pushed_at TEXT,
            gh_size_kb INTEGER,
            gh_stars INTEGER,
            gh_forks INTEGER,
            gh_open_issues INTEGER,
            state_repo_size_bytes INTEGER,
            repo_size_mb REAL,
            repo_size_bucket TEXT,
            repo_age_bucket TEXT,
            repo_activity_bucket TEXT,
            stars_bucket TEXT,
            forks_bucket TEXT,
            gh_open_issues_bucket TEXT,
            skill_repository_count INTEGER NOT NULL DEFAULT 0,
            repo_metadata_score REAL,
            penalty_applied REAL NOT NULL DEFAULT 0,
            repository_context_score REAL,
            final_score REAL,
            final_verdict TEXT,
            is_false_positive INTEGER NOT NULL,
            false_positive_status TEXT NOT NULL
        );

        CREATE INDEX idx_repo_context_skill_hash ON repo_context(skill_hash);
        CREATE INDEX idx_repo_context_repo ON repo_context(repository);
        CREATE INDEX idx_repo_context_final_verdict ON repo_context(final_verdict);
        CREATE INDEX idx_repo_context_fp ON repo_context(is_false_positive);
        CREATE INDEX idx_repo_context_final_score ON repo_context(final_score);
        """
    )


def insert_repo_metadata(conn: sqlite3.Connection, repo_meta: Dict[str, Dict[str, object]]) -> None:
    rows = []
    ts = now_utc()
    for repo, m in repo_meta.items():
        rows.append(
            {
                "repository": repo,
                "loaded_at": ts,
                **m,
            }
        )
    conn.executemany(
        """
        INSERT INTO repo_context_repo_metadata(
            repository, loaded_at,
            gh_created_at, gh_pushed_at, gh_size_kb, gh_stars, gh_forks, gh_open_issues,
            state_repo_size_bytes, repo_size_mb, repo_size_bucket, repo_age_bucket,
            repo_activity_bucket, stars_bucket, forks_bucket, gh_open_issues_bucket,
            repo_metadata_score
        ) VALUES (
            :repository, :loaded_at,
            :gh_created_at, :gh_pushed_at, :gh_size_kb, :gh_stars, :gh_forks, :gh_open_issues,
            :state_repo_size_bytes, :repo_size_mb, :repo_size_bucket, :repo_age_bucket,
            :repo_activity_bucket, :stars_bucket, :forks_bucket, :gh_open_issues_bucket,
            :repo_metadata_score
        )
        """,
        rows,
    )


def insert_codex_rows(conn: sqlite3.Connection, codex_rows: List[Dict[str, object]]) -> None:
    ts = now_utc()
    payload = []
    for c in codex_rows:
        payload.append({"loaded_at": ts, **c})
    conn.executemany(
        """
        INSERT INTO repo_context_skill_codex(
            loaded_at, source_line_no, repo_skillname, skill_hash, repository, skill_path,
            codex_domain_match, codex_domain_reason, codex_code_match, codex_code_reason,
            codex_readme_match, codex_readme_reason, codex_repo_maliciousness, codex_repo_maliciousness_reason,
            codex_final_verdict, codex_confidence, codex_risk_note, codex_score
        ) VALUES (
            :loaded_at, :source_line_no, :repo_skillname, :skill_hash, :repository, :skill_path,
            :codex_domain_match, :codex_domain_reason, :codex_code_match, :codex_code_reason,
            :codex_readme_match, :codex_readme_reason, :codex_repo_maliciousness, :codex_repo_maliciousness_reason,
            :codex_final_verdict, :codex_confidence, :codex_risk_note, :codex_score
        )
        """,
        payload,
    )


def insert_merged_rows(conn: sqlite3.Connection, rows: List[Dict[str, object]]) -> None:
    conn.executemany(
        """
        INSERT INTO repo_context(
            loaded_at, source_line_no, repo_skillname, skill_hash, repository, skill_path,
            skill_present_on_marketplaces, skill_marketplaces_count, skill_marketplaces_csv, skill_present_on_skills_sh,
            codex_domain_match, codex_domain_reason, codex_code_match, codex_code_reason,
            codex_readme_match, codex_readme_reason, codex_repo_maliciousness, codex_repo_maliciousness_reason,
            codex_final_verdict, codex_confidence, codex_risk_note, codex_score,
            gh_created_at, gh_pushed_at, gh_size_kb, gh_stars, gh_forks, gh_open_issues,
            state_repo_size_bytes, repo_size_mb, repo_size_bucket, repo_age_bucket, repo_activity_bucket,
            stars_bucket, forks_bucket, gh_open_issues_bucket, skill_repository_count, repo_metadata_score,
            penalty_applied,
            repository_context_score, final_score, final_verdict, is_false_positive, false_positive_status
        ) VALUES (
            :loaded_at, :source_line_no, :repo_skillname, :skill_hash, :repository, :skill_path,
            :skill_present_on_marketplaces, :skill_marketplaces_count, :skill_marketplaces_csv, :skill_present_on_skills_sh,
            :codex_domain_match, :codex_domain_reason, :codex_code_match, :codex_code_reason,
            :codex_readme_match, :codex_readme_reason, :codex_repo_maliciousness, :codex_repo_maliciousness_reason,
            :codex_final_verdict, :codex_confidence, :codex_risk_note, :codex_score,
            :gh_created_at, :gh_pushed_at, :gh_size_kb, :gh_stars, :gh_forks, :gh_open_issues,
            :state_repo_size_bytes, :repo_size_mb, :repo_size_bucket, :repo_age_bucket, :repo_activity_bucket,
            :stars_bucket, :forks_bucket, :gh_open_issues_bucket, :skill_repository_count, :repo_metadata_score,
            :penalty_applied,
            :repository_context_score, :final_score, :final_verdict, :is_false_positive, :false_positive_status
        )
        """,
        rows,
    )


def load_skill_marketplaces(conn: sqlite3.Connection) -> Dict[str, Set[str]]:
    out: Dict[str, Set[str]] = defaultdict(set)
    try:
        for h, m in conn.execute("SELECT skill_hash, marketplace FROM market_presence"):
            hh = normalize_skill_hash(str(h or ""))
            mm = str(m or "").strip().lower()
            if hh and mm:
                out[hh].add(mm)
    except sqlite3.OperationalError:
        pass
    return out


def score_bin_label(score: Optional[float]) -> str:
    if score is None:
        return "unknown"
    s = max(0.0, min(100.0, float(score)))
    low = int(s // 10) * 10
    high = low + 9
    if low >= 100:
        return "100"
    return f"{low:02d}-{high:02d}"


def build_category_impact(
    rows: List[Dict[str, object]],
    col: str,
    skill_marketplaces: Dict[str, Set[str]],
) -> Dict[str, Dict[str, object]]:
    buckets: Dict[str, Dict[str, object]] = {}
    for r in rows:
        label = str(r.get(col) or "unknown")
        b = buckets.setdefault(label, {"row_count": 0, "skills": set(), "repositories": set(), "marketplaces": set()})
        b["row_count"] += 1
        sh = normalize_skill_hash(str(r.get("skill_hash") or ""))
        rp = normalize_repo(str(r.get("repository") or ""))
        if sh:
            b["skills"].add(sh)
            for m in skill_marketplaces.get(sh, set()):
                b["marketplaces"].add(m)
        if rp:
            b["repositories"].add(rp)

    out: Dict[str, Dict[str, object]] = {}
    for label, b in buckets.items():
        skills = sorted(list(b["skills"]))
        repos = sorted(list(b["repositories"]))
        markets = sorted(list(b["marketplaces"]))
        out[label] = {
            "row_count": int(b["row_count"]),
            "unique_skills": len(skills),
            "unique_repositories": len(repos),
            "unique_marketplaces": len(markets),
            "marketplaces": markets,
            "sample_repositories": repos[:25],
            "sample_skills": skills[:25],
        }
    return out


def compute_stats(rows: List[Dict[str, object]], skill_marketplaces: Dict[str, Set[str]]) -> Dict[str, object]:
    verdict_counts = Counter(str(r.get("final_verdict") or "unknown") for r in rows)
    fp_counts = Counter(str(r.get("false_positive_status") or "unknown") for r in rows)
    score_bins = Counter(score_bin_label(r.get("final_score")) for r in rows)

    def cnt(col: str) -> Dict[str, int]:
        return dict(Counter(str(r.get(col) or "unknown") for r in rows))

    by_repo: Dict[str, List[float]] = defaultdict(list)
    by_skill: Dict[str, List[float]] = defaultdict(list)
    by_skill_rows: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for r in rows:
        sc = r.get("final_score")
        if sc is None:
            continue
        by_repo[str(r["repository"])].append(float(sc))
        by_skill[str(r["skill_hash"])].append(float(sc))
        by_skill_rows[str(r["skill_hash"])].append(r)

    top_repos = sorted(
        ({"repository": repo, "avg_final_score": round(sum(v) / len(v), 3), "n": len(v)} for repo, v in by_repo.items() if v),
        key=lambda x: (-x["avg_final_score"], -x["n"], x["repository"]),
    )[:20]

    def skill_top3_avg(skill_rows: List[Dict[str, object]]) -> float:
        # Prefer rows where code alignment signal exists; then keep top-3 by stars.
        with_code = [r for r in skill_rows if str(r.get("codex_code_match") or "").strip() != ""]
        chosen = with_code if with_code else list(skill_rows)
        chosen.sort(key=lambda r: (-int(r.get("gh_stars") or 0), str(r.get("repository") or "")))
        top = chosen[:3]
        vals = [float(r.get("final_score")) for r in top if r.get("final_score") is not None]
        return round(sum(vals) / len(vals), 3) if vals else 0.0

    top_skills = sorted(
        (
            {
                "skill_hash": h,
                "avg_repository_context_score_top3": skill_top3_avg(v),
                "avg_final_score": skill_top3_avg(v),
                "n": len(v),
            }
            for h, v in by_skill_rows.items()
            if v
        ),
        key=lambda x: (-x["avg_repository_context_score_top3"], -x["n"], x["skill_hash"]),
    )[:20]

    return {
        "generated_at": now_utc(),
        "rows": len(rows),
        "unique_skills": len({str(r.get("skill_hash") or "") for r in rows if str(r.get("skill_hash") or "")}),
        "unique_repositories": len({str(r.get("repository") or "") for r in rows if str(r.get("repository") or "")}),
        "formula": "repository_context_score = max(0, 0.7 * codex_score + 0.3 * repo_metadata_score - penalty_applied)",
        "metadata_formula_note": "repo_metadata_score includes uncapped skill_repository_count contribution (+1 point per analyzed repository for that skill hash)",
        "skill_aggregation_note": "skill-level ranking uses average repository_context_score over top-3 analyzed repositories by gh_stars (preferring rows with codex_code_match)",
        "penalty_rule": "penalty_applied = 50 when codex_final_verdict is aligned_but_repo_suspicious (typo variants included)",
        "penalty_rows": sum(1 for r in rows if float(r.get("penalty_applied") or 0.0) > 0.0),
        "verdict_counts": dict(verdict_counts),
        "false_positive_counts": dict(fp_counts),
        "score_bins": dict(sorted(score_bins.items())),
        "repo_size_bucket": cnt("repo_size_bucket"),
        "repo_age_bucket": cnt("repo_age_bucket"),
        "repo_activity_bucket": cnt("repo_activity_bucket"),
        "stars_bucket": cnt("stars_bucket"),
        "forks_bucket": cnt("forks_bucket"),
        "issues_bucket": cnt("gh_open_issues_bucket"),
        "codex_domain_match": cnt("codex_domain_match"),
        "codex_code_match": cnt("codex_code_match"),
        "codex_readme_match": cnt("codex_readme_match"),
        "codex_final_verdict": cnt("codex_final_verdict"),
        "top_repositories": top_repos,
        "top_skills": top_skills,
        "category_impact": {
            "final_verdict": build_category_impact(rows, "final_verdict", skill_marketplaces),
            "repo_size_bucket": build_category_impact(rows, "repo_size_bucket", skill_marketplaces),
            "repo_age_bucket": build_category_impact(rows, "repo_age_bucket", skill_marketplaces),
            "repo_activity_bucket": build_category_impact(rows, "repo_activity_bucket", skill_marketplaces),
            "codex_domain_match": build_category_impact(rows, "codex_domain_match", skill_marketplaces),
            "codex_code_match": build_category_impact(rows, "codex_code_match", skill_marketplaces),
            "codex_readme_match": build_category_impact(rows, "codex_readme_match", skill_marketplaces),
        },
    }


def write_meta_and_views(conn: sqlite3.Connection, stats: Dict[str, object], out_csv: Path) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS repo_context_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        DROP VIEW IF EXISTS repo_context_score_bins;
        CREATE VIEW repo_context_score_bins AS
        SELECT
            CASE
                WHEN final_score IS NULL THEN 'unknown'
                WHEN final_score < 10 THEN '00-09'
                WHEN final_score < 20 THEN '10-19'
                WHEN final_score < 30 THEN '20-29'
                WHEN final_score < 40 THEN '30-39'
                WHEN final_score < 50 THEN '40-49'
                WHEN final_score < 60 THEN '50-59'
                WHEN final_score < 70 THEN '60-69'
                WHEN final_score < 80 THEN '70-79'
                WHEN final_score < 90 THEN '80-89'
                ELSE '90-100'
            END AS score_bin,
            COUNT(*) AS n
        FROM repo_context
        GROUP BY 1
        ORDER BY 1;

        DROP VIEW IF EXISTS repo_context_repo_rank;
        CREATE VIEW repo_context_repo_rank AS
        SELECT repository, ROUND(AVG(final_score), 3) AS avg_final_score, COUNT(*) AS n
        FROM repo_context
        GROUP BY repository
        ORDER BY avg_final_score DESC, n DESC, repository;

        DROP VIEW IF EXISTS repo_context_skill_rank;
        CREATE VIEW repo_context_skill_rank AS
        SELECT skill_hash, ROUND(AVG(final_score), 3) AS avg_final_score, COUNT(*) AS n
        FROM repo_context
        GROUP BY skill_hash
        ORDER BY avg_final_score DESC, n DESC, skill_hash;
        """
    )

    meta_items = {
        "repo_context_generated_at": stats["generated_at"],
        "repo_context_rows": stats["rows"],
        "repo_context_unique_skills": stats["unique_skills"],
        "repo_context_unique_repositories": stats["unique_repositories"],
        "repo_context_formula": stats["formula"],
        "repo_context_penalty_rule": str(stats.get("penalty_rule", "")),
        "repo_context_penalty_rows": int(stats.get("penalty_rows", 0)),
        "repo_context_scores_csv": str(out_csv),
        "repo_context_verdict_counts": json.dumps(stats["verdict_counts"], ensure_ascii=True),
    }
    conn.executemany(
        "INSERT OR REPLACE INTO repo_context_meta(key, value) VALUES (?, ?)",
        [(k, str(v)) for k, v in meta_items.items()],
    )


def write_csv(path: Path, rows: List[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "source_line_no",
        "repo_skillname",
        "skill_hash",
        "repository",
        "skill_path",
        "skill_present_on_marketplaces",
        "skill_marketplaces_count",
        "skill_marketplaces_csv",
        "skill_present_on_skills_sh",
        "gh_created_at",
        "gh_pushed_at",
        "gh_size_kb",
        "gh_stars",
        "gh_forks",
        "gh_open_issues",
        "state_repo_size_bytes",
        "repo_size_mb",
        "repo_size_bucket",
        "repo_age_bucket",
        "repo_activity_bucket",
        "stars_bucket",
        "forks_bucket",
        "gh_open_issues_bucket",
        "skill_repository_count",
        "repo_metadata_score",
        "penalty_applied",
        "codex_domain_match",
        "codex_domain_reason",
        "codex_code_match",
        "codex_code_reason",
        "codex_readme_match",
        "codex_readme_reason",
        "codex_repo_maliciousness",
        "codex_repo_maliciousness_reason",
        "codex_final_verdict",
        "codex_confidence",
        "codex_risk_note",
        "codex_score",
        "repository_context_score",
        "final_score",
        "final_verdict",
        "is_false_positive",
        "false_positive_status",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=header,
            quoting=csv.QUOTE_ALL,
            escapechar="\\",
            lineterminator="\n",
        )
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in header})


def write_dashboard(dashboard_dir: Path, stats: Dict[str, object]) -> Path:
    dashboard_dir.mkdir(parents=True, exist_ok=True)
    src_echarts = Path("posteval/assets/echarts.min.js").resolve()
    dst_echarts = dashboard_dir / "echarts.min.js"
    if src_echarts.exists():
        shutil.copy2(src_echarts, dst_echarts)

    stats_json_path = dashboard_dir / "repo_context_stats.json"
    stats_json_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")

    html = f"""<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Repo Context Dashboard</title>
  <script src=\"./echarts.min.js\"></script>
  <style>
    body {{ margin: 0; background: #f5f7fb; color: #1f2937; font-family: 'IBM Plex Sans', 'Segoe UI', sans-serif; }}
    .wrap {{ max-width: 1500px; margin: 0 auto; padding: 20px; }}
    h1 {{ margin: 0 0 6px 0; font-size: 28px; }}
    .sub {{ color: #4b5563; margin-bottom: 14px; }}
    .kpi {{ display: grid; grid-template-columns: repeat(5, minmax(160px, 1fr)); gap: 10px; margin-bottom: 14px; }}
    .card {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 12px; padding: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.04); }}
    .k {{ font-size: 12px; color: #6b7280; }}
    .v {{ font-size: 24px; font-weight: 700; }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(320px, 1fr)); gap: 12px; }}
    .donutGrid {{ display: grid; grid-template-columns: repeat(4, minmax(200px, 1fr)); gap: 12px; margin-bottom: 12px; }}
    .full {{ margin-top: 12px; }}
    .chart {{ height: 360px; }}
    .chartTall {{ height: 520px; }}
    .chartDonut {{ height: 240px; }}
    .impactWrap {{ max-height: 620px; overflow: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    th, td {{ border-bottom: 1px solid #e5e7eb; text-align: left; padding: 6px; vertical-align: top; }}
    th {{ background: #f9fafb; position: sticky; top: 0; }}
    @media (max-width: 900px) {{ .grid {{ grid-template-columns: 1fr; }} .kpi {{ grid-template-columns: repeat(2, minmax(140px, 1fr)); }} .donutGrid {{ grid-template-columns: repeat(2, minmax(160px, 1fr)); }} }}
  </style>
</head>
<body>
  <div class=\"wrap\">
    <h1>Repository Context Ranking Dashboard</h1>
    <div class=\"sub\">Formula: <code>{stats['formula']}</code> | Penalty rows: <code>{stats.get('penalty_rows', 0)}</code> | Generated: <code>{stats['generated_at']}</code></div>
    <div class=\"kpi\">
      <div class=\"card\"><div class=\"k\">Rows</div><div class=\"v\">{stats['rows']}</div></div>
      <div class=\"card\"><div class=\"k\">Unique Skills</div><div class=\"v\">{stats['unique_skills']}</div></div>
      <div class=\"card\"><div class=\"k\">Unique Repositories</div><div class=\"v\">{stats['unique_repositories']}</div></div>
      <div class=\"card\"><div class=\"k\">False Positive</div><div class=\"v\">{stats['false_positive_counts'].get('false_positive', 0)}</div></div>
      <div class=\"card\"><div class=\"k\">Not False Positive</div><div class=\"v\">{stats['false_positive_counts'].get('not_false_positive', 0)}</div></div>
    </div>

    <div class=\"donutGrid\">
      <div class=\"card\"><div id=\"donutVerdict\" class=\"chartDonut\"></div></div>
      <div class=\"card\"><div id=\"donutFP\" class=\"chartDonut\"></div></div>
      <div class=\"card\"><div id=\"donutRepoAge\" class=\"chartDonut\"></div></div>
      <div class=\"card\"><div id=\"donutCodexVerdict\" class=\"chartDonut\"></div></div>
    </div>

    <div class=\"grid\">
      <div class=\"card\"><div id=\"verdict\" class=\"chart\"></div></div>
      <div class=\"card\"><div id=\"scoreBins\" class=\"chart\"></div></div>
      <div class=\"card\"><div id=\"codexTriplet\" class=\"chart\"></div></div>
      <div class=\"card\"><div id=\"metaBuckets\" class=\"chart\"></div></div>
      <div class=\"card\"><div id=\"topRepos\" class=\"chartTall\"></div></div>
      <div class=\"card\"><div id=\"topSkills\" class=\"chartTall\"></div></div>
    </div>
    <div class=\"card full\"><div class=\"impactWrap\"><div id=\"impactTable\"></div></div></div>
  </div>

<script>
const stats = {json.dumps(stats, ensure_ascii=True)};
function asPieData(obj) {{
  return Object.entries(obj || {{}}).map(([name, value]) => ({{ name, value }}));
}}
function mkDonut(id, title, obj, colors) {{
  const ch = echarts.init(document.getElementById(id));
  const data = asPieData(obj);
  ch.setOption({{
    title: {{ text: title, left: 'center', top: 4, textStyle: {{ fontSize: 12 }} }},
    tooltip: {{ trigger: 'item' }},
    legend: {{ bottom: 0, type: 'scroll', textStyle: {{ fontSize: 10 }} }},
    animation: false,
    series: [{{
      type: 'pie',
      radius: ['45%', '72%'],
      center: ['50%', '48%'],
      label: {{ formatter: '{{b}}\\n{{d}}%' }},
      data: data,
      color: colors || undefined
    }}]
  }});
}}
function mkBar(id, title, labels, values, color) {{
  const ch = echarts.init(document.getElementById(id));
  ch.setOption({{title: {{ text: title, left: 8, top: 6, textStyle: {{ fontSize: 14 }} }}, animation: false, grid: {{ top: 48, left: 50, right: 20, bottom: 60 }}, xAxis: {{ type: 'category', data: labels, axisLabel: {{ rotate: 20 }} }}, yAxis: {{ type: 'value' }}, tooltip: {{ trigger: 'axis' }}, series: [{{ type: 'bar', data: values, itemStyle: {{ color }} }}]}});
}}
mkBar('verdict','Final Verdict Distribution',Object.keys(stats.verdict_counts),Object.values(stats.verdict_counts),'#2563eb');
mkBar('scoreBins','Final Score Bins',Object.keys(stats.score_bins),Object.values(stats.score_bins),'#059669');
mkDonut('donutVerdict', 'Final Verdict', stats.verdict_counts, ['#2563eb','#f59e0b','#059669','#9ca3af']);
mkDonut('donutFP', 'False Positive Status', stats.false_positive_counts, ['#dc2626','#16a34a','#9ca3af']);
mkDonut('donutRepoAge', 'Repo Age Buckets', stats.repo_age_bucket, ['#1d4ed8','#0ea5e9','#f59e0b','#9ca3af']);
mkDonut('donutCodexVerdict', 'Codex Verdict', stats.codex_final_verdict || {{}}, ['#2563eb','#f59e0b','#dc2626','#9ca3af']);
(() => {{
  const labels = ['high', 'medium', 'low', 'unknown'];
  const dims = ['codex_domain_match', 'codex_code_match', 'codex_readme_match'];
  const names = ['Domain', 'Code', 'README'];
  const series = labels.map((lbl, i) => ({{ name: lbl, type: 'bar', stack: 'total', itemStyle: {{ color: ['#1d4ed8','#0ea5e9','#f59e0b','#9ca3af'][i] }}, data: dims.map((d) => (stats[d][lbl] || 0)) }}));
  const ch = echarts.init(document.getElementById('codexTriplet'));
  ch.setOption({{ title: {{ text: 'Codex Match Categories', left: 8, top: 6, textStyle: {{ fontSize: 14 }} }}, animation: false, tooltip: {{ trigger: 'axis', axisPointer: {{ type: 'shadow' }} }}, legend: {{ top: 24 }}, grid: {{ top: 60, left: 56, right: 20, bottom: 40 }}, xAxis: {{ type: 'category', data: names }}, yAxis: {{ type: 'value' }}, series }});
}})();
(() => {{
  const ch = echarts.init(document.getElementById('metaBuckets'));
  const labels = Object.keys(stats.repo_age_bucket);
  ch.setOption({{ title: {{ text: 'Repository Age Buckets', left: 8, top: 6, textStyle: {{ fontSize: 14 }} }}, animation: false, grid: {{ top: 48, left: 56, right: 20, bottom: 40 }}, tooltip: {{ trigger: 'axis' }}, xAxis: {{ type: 'category', data: labels }}, yAxis: {{ type: 'value' }}, series: [{{ type: 'bar', data: labels.map((k) => stats.repo_age_bucket[k]), itemStyle: {{ color: '#7c3aed' }} }}] }});
}})();
(() => {{
  const rows = stats.top_repositories.slice(0, 20);
  const labels = rows.map((x) => x.repository).reverse();
  const vals = rows.map((x) => x.avg_final_score).reverse();
  const ch = echarts.init(document.getElementById('topRepos'));
  ch.setOption({{ title: {{ text: 'Top Repositories by Avg Final Score', left: 8, top: 6, textStyle: {{ fontSize: 14 }} }}, animation: false, grid: {{ top: 48, left: 260, right: 20, bottom: 20 }}, tooltip: {{ trigger: 'axis' }}, xAxis: {{ type: 'value', min: 0, max: 100 }}, yAxis: {{ type: 'category', data: labels, axisLabel: {{ fontSize: 10 }} }}, series: [{{ type: 'bar', data: vals, itemStyle: {{ color: '#0f766e' }} }}] }});
}})();
(() => {{
  const rows = stats.top_skills.slice(0, 20);
  const labels = rows.map((x) => x.skill_hash.slice(0, 12)).reverse();
  const vals = rows.map((x) => x.avg_final_score).reverse();
  const ch = echarts.init(document.getElementById('topSkills'));
  ch.setOption({{ title: {{ text: 'Top Skill Hashes by Avg Final Score', left: 8, top: 6, textStyle: {{ fontSize: 14 }} }}, animation: false, grid: {{ top: 48, left: 120, right: 20, bottom: 20 }}, tooltip: {{ trigger: 'axis' }}, xAxis: {{ type: 'value', min: 0, max: 100 }}, yAxis: {{ type: 'category', data: labels, axisLabel: {{ fontSize: 10 }} }}, series: [{{ type: 'bar', data: vals, itemStyle: {{ color: '#b45309' }} }}] }});
}})();
(() => {{
  const root = document.getElementById('impactTable');
  const cats = stats.category_impact || {{}};
  let html = '<h3 style="margin:6px 0 10px 0;font-size:14px;">Category Impact: Skills, Repositories, Marketplaces</h3>';
  Object.keys(cats).forEach((cat) => {{
    html += `<h4 style="margin:10px 0 6px 0;font-size:13px;">${{cat}}</h4>`;
    html += '<table><thead><tr><th>Category</th><th>Rows</th><th>Unique Skills</th><th>Unique Repos</th><th>Marketplaces</th><th>Sample Repositories</th></tr></thead><tbody>';
    const rows = Object.entries(cats[cat]).sort((a,b)=> (b[1].row_count||0) - (a[1].row_count||0));
    rows.forEach(([label, d]) => {{
      const m = (d.marketplaces || []).join(', ') || 'n/a';
      const r = (d.sample_repositories || []).slice(0,6).join('<br/>');
      html += `<tr><td>${{label}}</td><td>${{d.row_count || 0}}</td><td>${{d.unique_skills || 0}}</td><td>${{d.unique_repositories || 0}}</td><td>${{m}}</td><td>${{r}}</td></tr>`;
    }});
    html += '</tbody></table>';
  }});
  root.innerHTML = html;
}})();
</script>
</body>
</html>
"""
    html_path = dashboard_dir / "repo_context_dashboard.html"
    html_path.write_text(html, encoding="utf-8")
    # Also place a copy in skillfix root for quick access.
    try:
        root_dir = dashboard_dir.parent
        shutil.copy2(html_path, root_dir / "repo_context_dashboard.html")
        shutil.copy2(stats_json_path, root_dir / "repo_context_stats.json")
        if dst_echarts.exists():
            shutil.copy2(dst_echarts, root_dir / "echarts.min.js")
    except Exception:
        pass
    return html_path


def main() -> None:
    args = parse_args()
    skills_input = Path(args.skills_input).resolve()
    repo_metadata_db = Path(args.repo_metadata_db).resolve()
    codex_jsonl = Path(args.codex_jsonl).resolve()
    base_db = Path(args.base_db).resolve()
    out_db = Path(args.out_db).resolve()
    out_csv = Path(args.out_merged_csv).resolve()
    dashboard_dir = Path(args.dashboard_dir).resolve()

    if not skills_input.exists():
        raise SystemExit(f"skills input not found: {skills_input}")
    if not repo_metadata_db.exists():
        raise SystemExit(f"repo metadata db not found: {repo_metadata_db}")
    if not codex_jsonl.exists():
        raise SystemExit(f"codex jsonl not found: {codex_jsonl}")

    skill_paths = load_skill_paths(skills_input)
    repo_meta = load_repo_metadata(repo_metadata_db)
    codex_rows = load_codex_rows(codex_jsonl, skill_paths)

    conn = prepare_db(base_db, out_db, overwrite=args.overwrite)
    try:
        ensure_schema(conn)
        skill_marketplaces = load_skill_marketplaces(conn)
        merged_rows = merge_rows(codex_rows, repo_meta, skill_marketplaces)
        if not merged_rows:
            raise SystemExit("no merged rows generated")
        insert_repo_metadata(conn, repo_meta)
        insert_codex_rows(conn, codex_rows)
        insert_merged_rows(conn, merged_rows)
        stats = compute_stats(merged_rows, skill_marketplaces)
        write_meta_and_views(conn, stats, out_csv)
        conn.commit()
    finally:
        conn.close()

    write_csv(out_csv, merged_rows)
    html_path = write_dashboard(dashboard_dir, stats)

    print(f"Repo metadata rows: {len(repo_meta)}")
    print(f"Codex skill rows: {len(codex_rows)}")
    print(f"Merged rows: {len(merged_rows)}")
    print(f"Output DB: {out_db}")
    print(f"Merged CSV: {out_csv}")
    print(f"Dashboard: {html_path}")
    print(f"Stats JSON: {dashboard_dir / 'repo_context_stats.json'}")


if __name__ == "__main__":
    main()
