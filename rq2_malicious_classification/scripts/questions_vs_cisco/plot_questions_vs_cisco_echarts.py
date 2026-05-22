#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple


BOOL_COLS = [
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
]

NUM_COLS = [
    "q02_different_flds",
    "q17_tool_impersonation_level",
    "q23_unique_ip_count_contacted",
    "overal_malicousness_rating",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_bool_distributions(conn: sqlite3.Connection, run_id: int) -> Dict[str, Dict[str, int]]:
    out: Dict[str, Dict[str, int]] = {}
    for c in BOOL_COLS:
        q = f"""
            SELECT
                SUM(CASE WHEN {c}=1 THEN 1 ELSE 0 END) AS true_n,
                SUM(CASE WHEN {c}=0 THEN 1 ELSE 0 END) AS false_n,
                SUM(CASE WHEN {c} IS NULL THEN 1 ELSE 0 END) AS null_n,
                COUNT(*) AS total_n
            FROM question_skill_results
            WHERE run_id = ?
        """
        r = conn.execute(q, (run_id,)).fetchone()
        out[c] = {
            "true": int(r[0] or 0),
            "false": int(r[1] or 0),
            "null": int(r[2] or 0),
            "total": int(r[3] or 0),
        }
    return out


def load_numeric_distributions(conn: sqlite3.Connection, run_id: int) -> Dict[str, List[Tuple[int, int]]]:
    out: Dict[str, List[Tuple[int, int]]] = {}
    for c in NUM_COLS:
        q = f"""
            SELECT {c} AS v, COUNT(*) AS n
            FROM question_skill_results
            WHERE run_id = ?
              AND {c} IS NOT NULL
            GROUP BY {c}
            ORDER BY {c}
        """
        out[c] = [(int(r[0]), int(r[1])) for r in conn.execute(q, (run_id,)).fetchall()]
    return out


def load_overlap_metrics(
    conn: sqlite3.Connection, run_id: int, llm_threshold: int
) -> Dict[str, int]:
    # Hash-level aggregation:
    # - LLM malicious: max(overal_malicousness_rating) >= threshold
    # - Cisco flagged: is_safe = 0
    q = """
        WITH qhash AS (
            SELECT
                hash_with_prefix,
                MAX(COALESCE(overal_malicousness_rating, 0)) AS max_rating
            FROM question_skill_results
            WHERE run_id = ?
              AND hash_with_prefix IS NOT NULL
              AND trim(hash_with_prefix) <> ''
            GROUP BY hash_with_prefix
        ),
        joined AS (
            SELECT
                q.hash_with_prefix,
                CASE WHEN q.max_rating >= ? THEN 1 ELSE 0 END AS llm_flag,
                CASE WHEN s.is_safe = 0 THEN 1 ELSE 0 END AS cisco_flag
            FROM qhash q
            JOIN scan_results s
              ON s.hash_with_prefix = q.hash_with_prefix
        )
        SELECT
            COUNT(*) AS total_common,
            SUM(CASE WHEN llm_flag=1 AND cisco_flag=1 THEN 1 ELSE 0 END) AS both,
            SUM(CASE WHEN llm_flag=1 AND cisco_flag=0 THEN 1 ELSE 0 END) AS llm_only,
            SUM(CASE WHEN llm_flag=0 AND cisco_flag=1 THEN 1 ELSE 0 END) AS cisco_only,
            SUM(CASE WHEN llm_flag=0 AND cisco_flag=0 THEN 1 ELSE 0 END) AS neither,
            SUM(llm_flag) AS llm_fail,
            SUM(CASE WHEN llm_flag=0 THEN 1 ELSE 0 END) AS llm_pass,
            SUM(cisco_flag) AS cisco_fail,
            SUM(CASE WHEN cisco_flag=0 THEN 1 ELSE 0 END) AS cisco_pass
        FROM joined
    """
    r = conn.execute(q, (run_id, llm_threshold)).fetchone()
    return {
        "total_common": int(r[0] or 0),
        "both": int(r[1] or 0),
        "llm_only": int(r[2] or 0),
        "cisco_only": int(r[3] or 0),
        "neither": int(r[4] or 0),
        "llm_fail": int(r[5] or 0),
        "llm_pass": int(r[6] or 0),
        "cisco_fail": int(r[7] or 0),
        "cisco_pass": int(r[8] or 0),
    }


def pct(n: int, d: int) -> float:
    return 0.0 if d <= 0 else round(100.0 * n / d, 2)


def write_html(path: Path, title: str, body: str) -> None:
    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <script src="./echarts.min.js"></script>
  <style>
    body {{
      margin: 0;
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      background: #f6f8fb;
      color: #1f2937;
    }}
    .wrap {{
      max-width: 1400px;
      margin: 0 auto;
      padding: 20px;
    }}
    h1 {{ margin: 0 0 6px 0; font-size: 26px; }}
    .sub {{ color: #4b5563; margin-bottom: 16px; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
      gap: 14px;
    }}
    .card {{
      background: white;
      border: 1px solid #e5e7eb;
      border-radius: 12px;
      padding: 10px;
      box-shadow: 0 1px 4px rgba(0,0,0,.05);
    }}
    .card h3 {{
      margin: 4px 0 8px 0;
      font-size: 13px;
      line-height: 1.3;
      min-height: 34px;
      color: #374151;
    }}
    .chart {{ width: 100%; height: 220px; }}
    .wide {{ height: 420px; }}
  </style>
</head>
<body>
{body}
</body>
</html>
"""
    path.write_text(html, encoding="utf-8")


def build_question_donuts(out_dir: Path, bool_dist: Dict[str, Dict[str, int]], run_name: str) -> None:
    cards = []
    script_blocks = []
    for i, c in enumerate(BOOL_COLS):
        cid = f"c_{i}"
        cards.append(f'<div class="card"><h3>{c}</h3><div id="{cid}" class="chart"></div></div>')
        d = bool_dist[c]
        script_blocks.append(
            f"""
(() => {{
  const el = document.getElementById('{cid}');
  const ch = echarts.init(el);
  ch.setOption({{
    animation: false,
    tooltip: {{ trigger: 'item' }},
    legend: {{ bottom: 0 }},
    series: [{{
      type: 'pie',
      radius: ['58%', '82%'],
      center: ['50%', '46%'],
      avoidLabelOverlap: true,
      label: {{ formatter: '{{b}}\\n{{d}}%' }},
      data: [
        {{ value: {d['true']}, name: 'True', itemStyle: {{ color: '#d1495b' }} }},
        {{ value: {d['false']}, name: 'False', itemStyle: {{ color: '#2a9d8f' }} }},
        {{ value: {d['null']}, name: 'Null', itemStyle: {{ color: '#9ca3af' }} }}
      ]
    }}]
  }});
}})();
"""
        )
    body = f"""
<div class="wrap">
  <h1>Question Booleans Mini Donuts</h1>
  <div class="sub">Run: <code>{run_name}</code>. Each donut shows True/False/Null distribution per question.</div>
  <div class="grid">
    {''.join(cards)}
  </div>
</div>
<script>
{''.join(script_blocks)}
</script>
"""
    write_html(out_dir / "questions_mini_donuts.html", "Question Mini Donuts", body)


def build_numeric_charts(out_dir: Path, num_dist: Dict[str, List[Tuple[int, int]]], run_name: str) -> None:
    cards = []
    script_blocks = []
    for i, c in enumerate(NUM_COLS):
        cid = f"n_{i}"
        cards.append(f'<div class="card"><h3>{c}</h3><div id="{cid}" class="chart"></div></div>')
        vals = [v for v, _ in num_dist[c]]
        cnts = [n for _, n in num_dist[c]]
        script_blocks.append(
            f"""
(() => {{
  const el = document.getElementById('{cid}');
  const ch = echarts.init(el);
  ch.setOption({{
    animation: false,
    tooltip: {{ trigger: 'axis' }},
    xAxis: {{ type: 'category', data: {json.dumps(vals)} }},
    yAxis: {{ type: 'value' }},
    series: [{{
      type: 'bar',
      data: {json.dumps(cnts)},
      itemStyle: {{ color: '#457b9d' }},
      barMaxWidth: 34
    }}]
  }});
}})();
"""
        )
    body = f"""
<div class="wrap">
  <h1>Numeric Question Distributions</h1>
  <div class="sub">Run: <code>{run_name}</code>. Grouped bar distributions for numeric question outputs.</div>
  <div class="grid">
    {''.join(cards)}
  </div>
</div>
<script>
{''.join(script_blocks)}
</script>
"""
    write_html(out_dir / "questions_numeric_distributions.html", "Numeric Question Distributions", body)


def build_overlap_chart(
    out_dir: Path, overlap: Dict[str, int], run_name: str, llm_threshold: int
) -> None:
    total = overlap["total_common"]
    both = overlap["both"]
    llm_only = overlap["llm_only"]
    cisco_only = overlap["cisco_only"]
    neither = overlap["neither"]
    body = f"""
<div class="wrap">
  <h1>LLM vs Cisco Detection Overlap</h1>
  <div class="sub">Run: <code>{run_name}</code>. LLM flag = <code>overal_malicousness_rating &gt;= {llm_threshold}</code>, Cisco flag = <code>is_safe = 0</code>. Common hashes: <b>{total}</b>.</div>
  <div class="grid">
    <div class="card"><h3>Overlap (4-way)</h3><div id="overlap_pie" class="chart wide"></div></div>
    <div class="card"><h3>Fail Rates</h3><div id="fail_rate" class="chart wide"></div></div>
  </div>
</div>
<script>
(() => {{
  const pie = echarts.init(document.getElementById('overlap_pie'));
  pie.setOption({{
    animation: false,
    tooltip: {{ trigger: 'item' }},
    series: [{{
      type: 'pie',
      radius: ['48%', '78%'],
      data: [
        {{ name: 'Both Flagged', value: {both}, itemStyle: {{ color: '#d62828' }} }},
        {{ name: 'LLM Only', value: {llm_only}, itemStyle: {{ color: '#f77f00' }} }},
        {{ name: 'Cisco Only', value: {cisco_only}, itemStyle: {{ color: '#003049' }} }},
        {{ name: 'Neither', value: {neither}, itemStyle: {{ color: '#90be6d' }} }}
      ],
      label: {{ formatter: '{{b}}\\n{{c}} ({{d}}%)' }}
    }}]
  }});

  const bar = echarts.init(document.getElementById('fail_rate'));
  bar.setOption({{
    animation: false,
    tooltip: {{ trigger: 'axis' }},
    xAxis: {{ type: 'category', data: ['LLM-Questions', 'Cisco-Scan'] }},
    yAxis: {{ type: 'value', name: 'Percent' }},
    series: [{{
      type: 'bar',
      data: [
        {pct(overlap['llm_fail'], total)},
        {pct(overlap['cisco_fail'], total)}
      ],
      itemStyle: {{ color: '#2f3e46' }},
      label: {{ show: true, position: 'top', formatter: '{{c}}%' }}
    }}]
  }});
}})();
</script>
"""
    write_html(out_dir / "llm_vs_cisco_overlap.html", "LLM vs Cisco Overlap", body)


def write_latex_rows(out_dir: Path, overlap: Dict[str, int], llm_threshold: int) -> None:
    total = overlap["total_common"]
    llm_fail = overlap["llm_fail"]
    llm_pass = overlap["llm_pass"]
    cisco_fail = overlap["cisco_fail"]
    cisco_pass = overlap["cisco_pass"]
    llm_rate = pct(llm_fail, total)
    cisco_rate = pct(cisco_fail, total)

    txt = (
        "% Add these rows under your scanner table \\midrule\n"
        f"% Cohort: hashes present in both question run and Cisco scan (n={total})\n"
        f"% LLM malicious threshold: overal_malicousness_rating >= {llm_threshold}\n"
        f"LLM-Questions & {total:,} & {llm_pass:,} & {llm_fail:,} & {llm_rate:.2f}\\% \\\\\n"
        f"Cisco-Scan & {total:,} & {cisco_pass:,} & {cisco_fail:,} & {cisco_rate:.2f}\\% \\\\\n"
    )
    (out_dir / "skills_sh_scanner_table_rows_questions_vs_cisco.tex").write_text(txt, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate ECharts plots for question outputs and LLM-vs-Cisco overlap.")
    ap.add_argument("--db", default="../data/rq2_malicious_classification/security_scan_with_questions.db")
    ap.add_argument("--run-name", default="results_questions")
    ap.add_argument("--llm-threshold", type=int, default=4, help="Malicious threshold on overal_malicousness_rating.")
    ap.add_argument("--out-dir", default="generated/questions_vs_cisco")
    ap.add_argument("--echarts-js", default="posteval/assets/echarts.min.js")
    args = ap.parse_args()

    db = Path(args.db).resolve()
    out_dir = Path(args.out_dir).resolve()
    echarts_js = Path(args.echarts_js).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if not db.exists():
        raise SystemExit(f"DB not found: {db}")
    if echarts_js.is_file():
        shutil.copy2(echarts_js, out_dir / "echarts.min.js")

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        run = conn.execute("SELECT run_id, run_name FROM question_runs WHERE run_name = ?", (args.run_name,)).fetchone()
        if run is None:
            raise SystemExit(f"Run not found: {args.run_name}")
        run_id = int(run["run_id"])
        run_name = str(run["run_name"])

        bool_dist = load_bool_distributions(conn, run_id)
        num_dist = load_numeric_distributions(conn, run_id)
        overlap = load_overlap_metrics(conn, run_id, int(args.llm_threshold))
    finally:
        conn.close()

    build_question_donuts(out_dir, bool_dist, run_name)
    build_numeric_charts(out_dir, num_dist, run_name)
    build_overlap_chart(out_dir, overlap, run_name, int(args.llm_threshold))
    write_latex_rows(out_dir, overlap, int(args.llm_threshold))

    summary = {
        "created_at": now_iso(),
        "db": str(db),
        "run_name": args.run_name,
        "llm_threshold": int(args.llm_threshold),
        "overlap": overlap,
        "outputs": {
            "mini_donuts_html": str(out_dir / "questions_mini_donuts.html"),
            "numeric_html": str(out_dir / "questions_numeric_distributions.html"),
            "overlap_html": str(out_dir / "llm_vs_cisco_overlap.html"),
            "latex_rows": str(out_dir / "skills_sh_scanner_table_rows_questions_vs_cisco.tex"),
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Wrote outputs to: {out_dir}")


if __name__ == "__main__":
    main()
