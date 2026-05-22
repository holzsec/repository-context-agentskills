#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BOOL_COLUMNS = [
    "q01_open_files_outside_current_directory",
    "q03_advertisement_tracking_endpoints",
    "q04_local_network_scanning",
    "q05_disguise_as_common_system_tool",
    "q06_description_hijacks_other_skill",
    "q07_description_prompt_injection",
    "q08_load_external_content_and_execute",
    "q09_intent_command_mismatch",
    "q10_execute_non_included_local_binaries",
    "q11_obfuscated_instructions",
    "q12_access_local_tokens_for_external_requests",
    "q13_suspicious_payload_behavior",
    "q14_spawns_subprocesses",
    "q15_contacts_blacklisted_domain_or_ip",
    "q16_persistence_mechanism",
    "q18_hardcoded_secrets",
    "q19_time_delayed_execution_evasion",
    "q20_transfers_sensitive_user_information",
    "q21_publicly_reachable_service",
    "q22_sends_pii_to_remote_services",
    "q24_crypto_scam_or_asset_theft",
    "q25_installs_other_skills",
    "cq_dynamic_code_execution_eval_exec_runtime_load",
    "cq_hidden_code",
    "cq_reads_environment_variables",
    "cq_directory_traversal_or_sandbox_escape",
    "aq_uses_non_official_or_third_party_endpoints",
    "estimated_security_tool",
]

INT_COLUMNS = [
    "q02_different_flds",
    "q17_tool_impersonation_level",
    "q23_unique_ip_count_contacted",
    "overal_malicousness_rating",
]

ALL_QUESTION_COLUMNS = BOOL_COLUMNS + INT_COLUMNS


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_bool(v: object) -> Optional[int]:
    s = str(v or "").strip().lower()
    if s in {"true", "1", "yes", "y"}:
        return 1
    if s in {"false", "0", "no", "n"}:
        return 0
    return None


def parse_int(v: object) -> Optional[int]:
    s = str(v or "").strip()
    if s == "":
        return None
    try:
        return int(float(s))
    except Exception:
        return None


def split_hash_from_skill_path(skill_path: str) -> Optional[str]:
    s = str(skill_path or "").strip()
    if not s:
        return None
    first = s.split("/", 1)[0]
    if "_" in first and len(first.split("_", 1)[0]) >= 2:
        return first
    return None


def slug_from_skill_path(skill_path: str) -> Optional[str]:
    # Expected clawhub path pattern: skills/<slug>/SKILL.md
    parts = [p for p in str(skill_path or "").strip().split("/") if p]
    if len(parts) >= 2 and parts[0].lower() == "skills":
        return parts[1]
    return None


def build_clawhub_slug_map(analysis_root: Path) -> Dict[str, str]:
    # Map slug -> hash_with_prefix, preferring highest publishedAt when duplicates exist.
    best: Dict[str, Tuple[int, str]] = {}
    for p in analysis_root.glob("010_*/_meta.json"):
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        slug = str(meta.get("slug") or "").strip()
        if not slug:
            continue
        published = int(meta.get("publishedAt") or 0)
        hash_with_prefix = p.parent.name
        old = best.get(slug)
        if old is None or published >= old[0]:
            best[slug] = (published, hash_with_prefix)
    return {k: v[1] for k, v in best.items()}


def ensure_tables(conn: sqlite3.Connection) -> None:
    # Migrate legacy schema that stored values_json.
    has_qsr = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='question_skill_results'"
    ).fetchone()
    if has_qsr:
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(question_skill_results)").fetchall()}
        if "values_json" in cols:
            conn.executescript(
                """
                DROP TABLE IF EXISTS question_consistency;
                DROP TABLE IF EXISTS question_skill_results;
                DROP TABLE IF EXISTS question_runs;
                """
            )

    q_cols_sql = ",\n            ".join(f"{c} INTEGER" for c in ALL_QUESTION_COLUMNS)
    conn.executescript(
        f"""
        PRAGMA busy_timeout = 5000;
        PRAGMA temp_store = MEMORY;

        CREATE TABLE IF NOT EXISTS question_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_name TEXT NOT NULL UNIQUE,
            source_csv TEXT NOT NULL,
            loaded_at TEXT NOT NULL,
            total_rows INTEGER NOT NULL,
            mapped_rows INTEGER NOT NULL,
            unmapped_rows INTEGER NOT NULL,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS question_skill_results (
            run_id INTEGER NOT NULL,
            hash_with_prefix TEXT,
            archive_type TEXT,
            archive_path TEXT,
            skill_path TEXT NOT NULL,
            mapped_by TEXT,
            {q_cols_sql},
            extra_json TEXT,
            PRIMARY KEY (run_id, skill_path, archive_path),
            FOREIGN KEY(run_id) REFERENCES question_runs(run_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_qsr_run_hash ON question_skill_results(run_id, hash_with_prefix);
        CREATE INDEX IF NOT EXISTS idx_qsr_archive ON question_skill_results(archive_path);

        CREATE TABLE IF NOT EXISTS question_consistency (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_a_id INTEGER NOT NULL,
            run_b_id INTEGER NOT NULL,
            question_key TEXT NOT NULL,
            compared_skills INTEGER NOT NULL,
            consistent_skills INTEGER NOT NULL,
            inconsistent_skills INTEGER NOT NULL,
            consistency_rate_pct REAL NOT NULL,
            computed_at TEXT NOT NULL,
            UNIQUE(run_a_id, run_b_id, question_key),
            FOREIGN KEY(run_a_id) REFERENCES question_runs(run_id) ON DELETE CASCADE,
            FOREIGN KEY(run_b_id) REFERENCES question_runs(run_id) ON DELETE CASCADE
        );
        """
    )


def load_run_into_db(
    conn: sqlite3.Connection,
    run_name: str,
    csv_path: Path,
    clawhub_map: Dict[str, str],
    replace: bool = True,
) -> int:
    if replace:
        old = conn.execute("SELECT run_id FROM question_runs WHERE run_name = ?", (run_name,)).fetchone()
        if old:
            conn.execute("DELETE FROM question_runs WHERE run_id = ?", (int(old[0]),))

    with csv_path.open("r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        cols = list(r.fieldnames or [])
        reserved = {"archive_type", "archive_path", "skill_path"}
        value_cols = [c for c in cols if c not in reserved]

        rows_payload: List[Tuple] = []
        total_rows = 0
        mapped_rows = 0
        unmapped_rows = 0

        for row in r:
            total_rows += 1
            archive_type = str(row.get("archive_type") or "")
            archive_path = str(row.get("archive_path") or "")
            skill_path = str(row.get("skill_path") or "")

            hash_with_prefix = split_hash_from_skill_path(skill_path)
            mapped_by = "path_prefix"

            if not hash_with_prefix and "clawhub" in archive_path.lower():
                slug = slug_from_skill_path(skill_path)
                if slug and slug in clawhub_map:
                    hash_with_prefix = clawhub_map[slug]
                    mapped_by = "clawhub_slug"

            if hash_with_prefix:
                mapped_rows += 1
            else:
                unmapped_rows += 1
                mapped_by = "unmapped"

            unknown = {k: row.get(k, "") for k in value_cols if k not in ALL_QUESTION_COLUMNS}
            bool_vals = {k: parse_bool(row.get(k, "")) for k in BOOL_COLUMNS}
            int_vals = {k: parse_int(row.get(k, "")) for k in INT_COLUMNS}
            rows_payload.append(
                (
                    hash_with_prefix,
                    archive_type,
                    archive_path,
                    skill_path,
                    mapped_by,
                    *[bool_vals[k] for k in BOOL_COLUMNS],
                    *[int_vals[k] for k in INT_COLUMNS],
                    json.dumps(unknown, ensure_ascii=False) if unknown else None,
                )
            )

    cur = conn.execute(
        """
        INSERT INTO question_runs(run_name, source_csv, loaded_at, total_rows, mapped_rows, unmapped_rows, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (run_name, str(csv_path), now_utc(), total_rows, mapped_rows, unmapped_rows, ""),
    )
    run_id = int(cur.lastrowid)

    q_insert_cols = [
        "run_id",
        "hash_with_prefix",
        "archive_type",
        "archive_path",
        "skill_path",
        "mapped_by",
        *ALL_QUESTION_COLUMNS,
        "extra_json",
    ]
    placeholders = ",".join(["?"] * len(q_insert_cols))
    conn.executemany(
        f"INSERT INTO question_skill_results({','.join(q_insert_cols)}) VALUES ({placeholders})",
        [(run_id, *p) for p in rows_payload],
    )
    return run_id


def fetch_run_values(conn: sqlite3.Connection, run_id: int) -> Dict[str, Dict[str, int]]:
    # Returns: hash -> {question_key -> bool_value(0/1)} for boolean question columns.
    out: Dict[str, Dict[str, int]] = {}
    col_sql = ", ".join(BOOL_COLUMNS)
    q = """
        SELECT hash_with_prefix, {cols}
        FROM question_skill_results
        WHERE run_id = ?
          AND hash_with_prefix IS NOT NULL
          AND trim(hash_with_prefix) <> ''
    """.format(cols=col_sql)
    for row in conn.execute(q, (run_id,)):
        h = str(row[0] or "").strip()
        if not h:
            continue
        bools: Dict[str, int] = {}
        for i, k in enumerate(BOOL_COLUMNS, start=1):
            v = row[i]
            if v is None:
                continue
            bools[k] = int(v)
        if bools:
            out[h] = bools
    return out


def compute_consistency(conn: sqlite3.Connection, run_a_id: int, run_b_id: int) -> None:
    vals_a = fetch_run_values(conn, run_a_id)
    vals_b = fetch_run_values(conn, run_b_id)
    common_hashes = sorted(set(vals_a.keys()) & set(vals_b.keys()))

    stats: Dict[str, Dict[str, int]] = defaultdict(lambda: {"compared": 0, "consistent": 0, "inconsistent": 0})
    for h in common_hashes:
        a = vals_a[h]
        b = vals_b[h]
        for qk in sorted(set(a.keys()) & set(b.keys())):
            stats[qk]["compared"] += 1
            if int(a[qk]) == int(b[qk]):
                stats[qk]["consistent"] += 1
            else:
                stats[qk]["inconsistent"] += 1

    rows = []
    ts = now_utc()
    for qk, s in sorted(stats.items()):
        compared = int(s["compared"])
        consistent = int(s["consistent"])
        inconsistent = int(s["inconsistent"])
        rate = 0.0 if compared <= 0 else round(100.0 * consistent / compared, 4)
        rows.append((run_a_id, run_b_id, qk, compared, consistent, inconsistent, rate, ts))

    conn.executemany(
        """
        INSERT OR REPLACE INTO question_consistency(
            run_a_id, run_b_id, question_key, compared_skills, consistent_skills,
            inconsistent_skills, consistency_rate_pct, computed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def copy_base_db(src: Path, dst: Path, overwrite: bool) -> None:
    if dst.exists():
        if overwrite:
            dst.unlink()
        else:
            raise SystemExit(f"Output DB already exists: {dst} (use --overwrite)")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Create security_scan_with_questions.db by attaching question run CSVs to scan hashes and "
            "computing per-question consistency across runs."
        )
    )
    ap.add_argument("--base-db", default="../data/rq2_malicious_classification/security_scan.db", help="Base scan DB with scan_results table.")
    ap.add_argument("--out-db", default="../data/rq2_malicious_classification/security_scan_with_questions.db", help="Output DB path.")
    ap.add_argument("--analysis-root", default="/scans/01_DATA_agentskills/analysis", help="Analysis root for 010_* slug mapping.")
    ap.add_argument("--overwrite", action="store_true", help="Overwrite out DB if it exists.")
    ap.add_argument(
        "--run",
        action="append",
        default=[],
        help="Run spec NAME=CSV_PATH. Can be passed multiple times.",
    )
    ap.add_argument(
        "--append-only",
        action="store_true",
        help="Do not copy base DB; append runs/consistency into existing out DB.",
    )
    args = ap.parse_args()

    base_db = Path(args.base_db).resolve()
    out_db = Path(args.out_db).resolve()
    analysis_root = Path(args.analysis_root).resolve()
    if not base_db.exists():
        raise SystemExit(f"Base DB not found: {base_db}")

    run_specs: List[Tuple[str, Path]] = []
    if args.run:
        for spec in args.run:
            if "=" not in spec:
                raise SystemExit(f"Invalid --run spec (expected NAME=CSV): {spec}")
            name, path = spec.split("=", 1)
            rp = Path(path).resolve()
            if not rp.exists():
                raise SystemExit(f"CSV not found: {rp}")
            run_specs.append((name.strip(), rp))
    else:
        run_specs = [
            ("results_questions", Path("../data/rq2_malicious_classification/results_questions.csv").resolve()),
            (
                "results_questions_with_security_scanner_classification",
                Path("../data/rq2_malicious_classification/results_questions_with_security_scanner_classification.csv").resolve(),
            ),
        ]
        for _, rp in run_specs:
            if not rp.exists():
                raise SystemExit(f"CSV not found: {rp}")

    if not args.append_only:
        copy_base_db(base_db, out_db, overwrite=bool(args.overwrite))
    elif not out_db.exists():
        raise SystemExit(f"--append-only requires existing out DB: {out_db}")

    clawhub_map = build_clawhub_slug_map(analysis_root)

    conn = sqlite3.connect(str(out_db))
    try:
        ensure_tables(conn)
        loaded_ids: List[int] = []
        for run_name, csv_path in run_specs:
            rid = load_run_into_db(conn, run_name, csv_path, clawhub_map, replace=True)
            loaded_ids.append(rid)

        # Compute consistency for all loaded pairs in this invocation.
        if len(loaded_ids) >= 2:
            for i in range(len(loaded_ids)):
                for j in range(i + 1, len(loaded_ids)):
                    compute_consistency(conn, loaded_ids[i], loaded_ids[j])

        conn.commit()
    finally:
        conn.close()

    print(f"Wrote: {out_db}")
    print("Loaded runs:")
    for run_name, csv_path in run_specs:
        print(f"  - {run_name}: {csv_path}")


if __name__ == "__main__":
    main()
