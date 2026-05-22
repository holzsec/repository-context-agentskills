#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Dict, Set


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Build the Skills.sh five-scanner summary JSON used by the RQ2 conditional-agreement figures."
    )
    ap.add_argument("--repo-context-db", default="../data/rq2_malicious_classification/security_scan_with_questions.db")
    ap.add_argument("--state-db", default="../data/rq1_skill_dataset/source/sqlite.db")
    ap.add_argument("--manual-db", default="../data/rq2_malicious_classification/skills_sh_security_scan_manual_crawl.db")
    ap.add_argument("--out-json", default="generated/conditional/skillsh_common_5scanner_summary.json")
    return ap.parse_args()


def norm_hash_with_prefix(v: object) -> str:
    s = str(v or "").strip().lower()
    if not s:
        return ""
    if "_" in s:
        return s.split("_", 1)[1].strip()
    return s


def load_skills_sh_hashes(conn: sqlite3.Connection) -> Set[str]:
    out: Set[str] = set()
    for (h,) in conn.execute(
        "SELECT DISTINCT lower(trim(skill_hash)) FROM market_presence WHERE lower(trim(marketplace))='skills_sh'"
    ):
        hh = str(h or "").strip().lower()
        if hh:
            out.add(hh)
    return out


def load_gpt53_fail_map(conn: sqlite3.Connection, skills_sh_hashes: Set[str]) -> Dict[str, int]:
    run_row = conn.execute("SELECT MAX(run_id) FROM question_skill_results").fetchone()
    run_id = int(run_row[0]) if run_row and run_row[0] is not None else None
    if run_id is None:
        return {}

    out: Dict[str, int] = {}
    for hwp, rating in conn.execute(
        """
        SELECT hash_with_prefix, MAX(COALESCE(overal_malicousness_rating, 0))
        FROM question_skill_results
        WHERE run_id = ?
        GROUP BY hash_with_prefix
        """,
        (run_id,),
    ):
        h = norm_hash_with_prefix(hwp)
        if h and h in skills_sh_hashes:
            out[h] = 1 if float(rating or 0) > 3.0 else 0
    return out


def load_cisco_fail_map(conn: sqlite3.Connection, skills_sh_hashes: Set[str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for h, crit, high in conn.execute(
        """
        SELECT lower(trim(skill_hash)), MAX(COALESCE(critical_count,0)), MAX(COALESCE(high_count,0))
        FROM scan_results
        GROUP BY lower(trim(skill_hash))
        """
    ):
        hh = str(h or "").strip().lower()
        if hh and hh in skills_sh_hashes:
            out[hh] = 1 if (int(crit or 0) > 0 or int(high or 0) > 0) else 0
    return out


def load_manual_maps(
    state_db: Path, manual_db: Path, skills_sh_hashes: Set[str]
) -> Dict[str, Dict[str, int]]:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("ATTACH DATABASE ? AS state", (str(state_db),))
    conn.execute("ATTACH DATABASE ? AS manual", (str(manual_db),))

    conn.executescript(
        """
        DROP TABLE IF EXISTS temp_state_skills;
        DROP TABLE IF EXISTS temp_manual_scans;

        CREATE TEMP TABLE temp_state_skills AS
        SELECT
            lower(trim(source)) AS source,
            lower(trim(input_skill_id)) AS input_skill_id,
            lower(trim(slug)) AS slug,
            lower(trim(skill_hash)) AS skill_hash
        FROM state.skill_input_metadata
        WHERE lower(trim(marketplace)) = 'skills_sh'
          AND skill_hash IS NOT NULL
          AND trim(skill_hash) <> '';

        CREATE INDEX idx_temp_state_source_id ON temp_state_skills(source, input_skill_id);
        CREATE INDEX idx_temp_state_source_slug ON temp_state_skills(source, slug);
        CREATE INDEX idx_temp_state_hash ON temp_state_skills(skill_hash);

        CREATE TEMP TABLE temp_manual_scans AS
        SELECT
            lower(trim(s.source)) AS source,
            lower(trim(s.skill_id)) AS skill_id,
            lower(trim(r.scanner_slug)) AS scanner_slug,
            lower(trim(r.status)) AS status
        FROM manual.skill_security_scanner_results r
        JOIN manual.skills s ON s.id = r.skill_id
        WHERE r.scanner_slug IN ('agent-trust-hub', 'snyk', 'socket');

        CREATE INDEX idx_temp_manual_source_id ON temp_manual_scans(source, skill_id);
        CREATE INDEX idx_temp_manual_scanner ON temp_manual_scans(scanner_slug);
        """
    )

    by_scanner: Dict[str, Dict[str, int]] = {}
    for h, scanner, fail in conn.execute(
        """
        WITH mapped AS (
          SELECT t.skill_hash, m.scanner_slug, m.status
          FROM temp_manual_scans m
          JOIN temp_state_skills t
            ON t.source = m.source
           AND (t.input_skill_id = m.skill_id OR t.slug = m.skill_id)
        )
        SELECT skill_hash, scanner_slug, MAX(CASE WHEN status='pass' THEN 0 ELSE 1 END) AS fail
        FROM mapped
        GROUP BY skill_hash, scanner_slug
        """
    ):
        hh = str(h or "").strip().lower()
        ss = str(scanner or "").strip().lower()
        if hh and hh in skills_sh_hashes and ss:
            by_scanner.setdefault(ss, {})[hh] = int(fail or 0)

    conn.close()
    return by_scanner


def main() -> None:
    args = parse_args()
    repo_db = Path(args.repo_context_db).resolve()
    state_db = Path(args.state_db).resolve()
    manual_db = Path(args.manual_db).resolve()
    out_json = Path(args.out_json).resolve()
    out_json.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(repo_db))
    conn.execute("PRAGMA temp_store=MEMORY")
    skills_sh_hashes = load_skills_sh_hashes(conn)
    gpt_map = load_gpt53_fail_map(conn, skills_sh_hashes)
    cisco_map = load_cisco_fail_map(conn, skills_sh_hashes)
    conn.close()

    manual_maps = load_manual_maps(state_db, manual_db, skills_sh_hashes)
    scanner_maps: Dict[str, Dict[str, int]] = {
        "GPT5.3-Judge": gpt_map,
        "Cisco Skill Scanner": cisco_map,
        "Agent Trust Hub": manual_maps.get("agent-trust-hub", {}),
        "Snyk": manual_maps.get("snyk", {}),
        "Socket": manual_maps.get("socket", {}),
    }

    common_hashes = set.intersection(*[set(m.keys()) for m in scanner_maps.values() if m])
    labels = list(scanner_maps.keys())
    fail_sets: Dict[str, Set[str]] = {
        scanner: {h for h in common_hashes if int(smap.get(h, 0)) == 1}
        for scanner, smap in scanner_maps.items()
    }

    scanner_rows = []
    for scanner, smap in scanner_maps.items():
        total = len(common_hashes)
        fails = sum(int(smap[h]) for h in common_hashes)
        passes = total - fails
        scanner_rows.append(
            {
                "scanner": scanner,
                "common_skills_evaluated": total,
                "pass_count": passes,
                "fail_count": fails,
                "pass_rate": round((passes / total) if total else 0.0, 4),
                "fail_rate": round((fails / total) if total else 0.0, 4),
            }
        )

    pairwise_conditional_overlap = {}
    for a in labels:
        fa = fail_sets[a]
        pairwise_conditional_overlap[a] = {}
        for b in labels:
            fb = fail_sets[b]
            pairwise_conditional_overlap[a][b] = round((len(fa & fb) / len(fa)) if fa else 0.0, 4)

    flagged_by_k = {str(k): 0 for k in range(1, len(labels) + 1)}
    for h in common_hashes:
        k = sum(1 for lbl in labels if int(scanner_maps[lbl].get(h, 0)) == 1)
        if k >= 1:
            flagged_by_k[str(k)] += 1

    summary = {
        "repo_context_db": str(repo_db),
        "state_db": str(state_db),
        "manual_db": str(manual_db),
        "skills_sh_hashes_in_repo_context": len(skills_sh_hashes),
        "common_skills_all_5_scanners": len(common_hashes),
        "scanners": scanner_rows,
        "pairwise_conditional_overlap": pairwise_conditional_overlap,
        "flagged_by_k_scanners": flagged_by_k,
    }
    out_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({"summary_json": str(out_json), **summary}, indent=2))


if __name__ == "__main__":
    main()
