#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


TABLE_NAME = "repo_context_skill_api"
MISSING_POINTS = 0.25

PRESENCE_MAP = {"0": 0, "1": 1}
EVIDENCE_MAP = {"n": 0, "s": 1, "e": 2, "na": None}
CONFIDENCE_MAP = {"l": 0, "m": 1, "h": 2}
VERDICT_MAP = {"i": 0, "na": 1, "as": 2, "ab": 3}


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Import compact repo-context API JSONL runs into repo_context.db.")
    ap.add_argument("--db", default="output/repo_context.db", help="Target SQLite DB path.")
    ap.add_argument(
        "--inputs",
        nargs="+",
        default=["posteval/run_top.jsonl", "posteval/run_2_3.jsonl"],
        help="Input JSONL files to combine.",
    )
    ap.add_argument("--replace", action="store_true", help="Drop and recreate the target table.")
    return ap.parse_args()


def ensure_schema(conn: sqlite3.Connection, replace: bool) -> None:
    if replace:
        conn.execute(f"DROP TABLE IF EXISTS {TABLE_NAME}")
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            loaded_at TEXT NOT NULL,
            source_jsonl TEXT NOT NULL,
            source_line_no INTEGER,
            bundle TEXT NOT NULL,
            repo_skillname TEXT,
            skill_hash TEXT NOT NULL,
            repository TEXT NOT NULL,
            skill_path TEXT,
            api_readme_exists TEXT,
            api_readme_exists_num INTEGER,
            api_skill_code_exists TEXT,
            api_skill_code_exists_num INTEGER,
            api_repo_code_exists TEXT,
            api_repo_code_exists_num INTEGER,
            api_domain_match TEXT,
            api_domain_match_num INTEGER,
            api_code_match TEXT,
            api_code_match_num INTEGER,
            api_readme_match TEXT,
            api_readme_match_num INTEGER,
            api_repo_maliciousness TEXT,
            api_repo_maliciousness_num INTEGER,
            api_security_tooling TEXT,
            api_security_tooling_num INTEGER,
            api_final_verdict TEXT,
            api_final_verdict_num INTEGER,
            api_confidence TEXT,
            api_confidence_num INTEGER,
            api_why TEXT,
            codebase_score REAL
        );
        CREATE INDEX IF NOT EXISTS idx_rctx_api_skill_repo ON {TABLE_NAME}(skill_hash, repository);
        CREATE INDEX IF NOT EXISTS idx_rctx_api_repo ON {TABLE_NAME}(repository);
        CREATE INDEX IF NOT EXISTS idx_rctx_api_bundle ON {TABLE_NAME}(bundle);
        """
    )
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE_NAME})")}
    if "codebase_score" not in cols:
        conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN codebase_score REAL")


def parse_bundle(bundle: str) -> tuple[str, str, str]:
    stem = Path(bundle).stem
    parts = stem.split("__", 3)
    if len(parts) < 4:
        return "", "", ""
    owner, repo, skill_hash, tail = parts
    repository = f"{owner}/{repo}"
    return repository, skill_hash, tail


def load_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            obj["_source_jsonl"] = str(path)
            obj["_source_line_no"] = line_no
            rows.append(obj)
    return rows


def evidence_points(value: object) -> float:
    v = str(value or "").strip().lower()
    if v == "e":
        return 1.0
    if v == "s":
        return 0.5
    if v == "n":
        return 0.0
    return MISSING_POINTS


def calc_codebase_score(obj: dict) -> float:
    domain_points = evidence_points(obj.get("dm"))
    code_points = evidence_points(obj.get("cm")) if str(obj.get("c")) == "1" else MISSING_POINTS
    readme_points = evidence_points(obj.get("rm")) if str(obj.get("r")) == "1" else MISSING_POINTS
    return round(
        100.0 * (
            0.40 * domain_points +
            0.35 * code_points +
            0.25 * readme_points
        ),
        2,
    )


def map_row(obj: dict, loaded_at: str) -> dict:
    bundle = str(obj.get("bundle") or "")
    repository, skill_hash, skill_path = parse_bundle(bundle)
    return {
        "loaded_at": loaded_at,
        "source_jsonl": obj["_source_jsonl"],
        "source_line_no": obj["_source_line_no"],
        "bundle": bundle,
        "repo_skillname": obj.get("repo_name_skill"),
        "skill_hash": skill_hash,
        "repository": repository,
        "skill_path": skill_path,
        "api_readme_exists": obj.get("r"),
        "api_readme_exists_num": PRESENCE_MAP.get(obj.get("r")),
        "api_skill_code_exists": obj.get("s"),
        "api_skill_code_exists_num": PRESENCE_MAP.get(obj.get("s")),
        "api_repo_code_exists": obj.get("c"),
        "api_repo_code_exists_num": PRESENCE_MAP.get(obj.get("c")),
        "api_domain_match": obj.get("dm"),
        "api_domain_match_num": EVIDENCE_MAP.get(obj.get("dm")),
        "api_code_match": obj.get("cm"),
        "api_code_match_num": EVIDENCE_MAP.get(obj.get("cm")),
        "api_readme_match": obj.get("rm"),
        "api_readme_match_num": EVIDENCE_MAP.get(obj.get("rm")),
        "api_repo_maliciousness": obj.get("mal"),
        "api_repo_maliciousness_num": EVIDENCE_MAP.get(obj.get("mal")),
        "api_security_tooling": obj.get("sec"),
        "api_security_tooling_num": EVIDENCE_MAP.get(obj.get("sec")),
        "api_final_verdict": obj.get("v"),
        "api_final_verdict_num": VERDICT_MAP.get(obj.get("v")),
        "api_confidence": obj.get("cf"),
        "api_confidence_num": CONFIDENCE_MAP.get(obj.get("cf")),
        "api_why": obj.get("why"),
        "codebase_score": calc_codebase_score(obj),
    }


def insert_rows(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.executemany(
        f"""
        INSERT INTO {TABLE_NAME}(
            loaded_at, source_jsonl, source_line_no, bundle, repo_skillname, skill_hash, repository, skill_path,
            api_readme_exists, api_readme_exists_num,
            api_skill_code_exists, api_skill_code_exists_num,
            api_repo_code_exists, api_repo_code_exists_num,
            api_domain_match, api_domain_match_num,
            api_code_match, api_code_match_num,
            api_readme_match, api_readme_match_num,
            api_repo_maliciousness, api_repo_maliciousness_num,
            api_security_tooling, api_security_tooling_num,
            api_final_verdict, api_final_verdict_num,
            api_confidence, api_confidence_num,
            api_why,
            codebase_score
        ) VALUES (
            :loaded_at, :source_jsonl, :source_line_no, :bundle, :repo_skillname, :skill_hash, :repository, :skill_path,
            :api_readme_exists, :api_readme_exists_num,
            :api_skill_code_exists, :api_skill_code_exists_num,
            :api_repo_code_exists, :api_repo_code_exists_num,
            :api_domain_match, :api_domain_match_num,
            :api_code_match, :api_code_match_num,
            :api_readme_match, :api_readme_match_num,
            :api_repo_maliciousness, :api_repo_maliciousness_num,
            :api_security_tooling, :api_security_tooling_num,
            :api_final_verdict, :api_final_verdict_num,
            :api_confidence, :api_confidence_num,
            :api_why,
            :codebase_score
        )
        """,
        rows,
    )


def main() -> int:
    args = parse_args()
    db_path = Path(args.db).resolve()
    input_paths = [Path(p).resolve() for p in args.inputs]
    for path in input_paths:
        if not path.exists():
            raise SystemExit(f"Input file not found: {path}")

    conn = sqlite3.connect(str(db_path))
    try:
        ensure_schema(conn, replace=args.replace)
        loaded_at = now_utc()
        payload: list[dict] = []
        for input_path in input_paths:
            for obj in load_rows(input_path):
                payload.append(map_row(obj, loaded_at))
        if args.replace:
            conn.execute(f"DELETE FROM {TABLE_NAME}")
        insert_rows(conn, payload)
        conn.commit()
        count = conn.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}").fetchone()[0]
    finally:
        conn.close()

    print(json.dumps({"table": TABLE_NAME, "rows": count, "db": str(db_path)}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
