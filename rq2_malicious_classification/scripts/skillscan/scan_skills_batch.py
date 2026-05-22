#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import random
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional


def iter_skill_dirs(root: Path) -> List[Path]:
    return sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name)


def parse_json_from_stdout(stdout: str) -> Optional[dict]:
    for line in (stdout or "").splitlines():
        t = line.strip()
        if not t:
            continue
        if t.startswith("{") and t.endswith("}"):
            try:
                return json.loads(t)
            except json.JSONDecodeError:
                continue
    return None


def run_one(scanner_bin: Path, skill_dir: Path, extra_args: List[str]) -> Dict[str, str]:
    cmd = [str(scanner_bin), "scan", str(skill_dir), "--format", "json", "--compact"] + list(extra_args)
    p = subprocess.run(cmd, capture_output=True, text=True)
    out = p.stdout or ""
    err = p.stderr or ""
    obj = parse_json_from_stdout(out)

    row: Dict[str, str] = {
        "hash": skill_dir.name,
        "skill_path": str(skill_dir),
        "ok": "0",
        "scan_error": "",
        "is_safe": "",
        "max_severity": "",
        "findings_count": "",
        "critical": "0",
        "high": "0",
        "medium": "0",
        "low": "0",
        "info": "0",
        "duration_ms": "",
        "analyzers_used": "",
    }

    if p.returncode != 0 and obj is None:
        row["scan_error"] = (err.strip() or out.strip() or f"non-zero exit {p.returncode}")[:2000]
        return row

    if obj is None:
        row["scan_error"] = (out.strip() or err.strip() or "no-json-output")[:2000]
        return row

    row["ok"] = "1"
    row["is_safe"] = str(obj.get("is_safe", ""))
    row["max_severity"] = str(obj.get("max_severity", ""))
    row["findings_count"] = str(obj.get("findings_count", ""))
    row["duration_ms"] = str(obj.get("duration_ms", ""))
    analyzers = obj.get("analyzers_used", [])
    if isinstance(analyzers, list):
        row["analyzers_used"] = ",".join(str(x) for x in analyzers)

    sev_counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "INFO": 0}
    findings = obj.get("findings", [])
    if isinstance(findings, list):
        for f in findings:
            if not isinstance(f, dict):
                continue
            s = str(f.get("severity", "")).strip().upper()
            if s in sev_counts:
                sev_counts[s] += 1
    row["critical"] = str(sev_counts["CRITICAL"])
    row["high"] = str(sev_counts["HIGH"])
    row["medium"] = str(sev_counts["MEDIUM"])
    row["low"] = str(sev_counts["LOW"])
    row["info"] = str(sev_counts["INFO"])
    return row


def main() -> None:
    ap = argparse.ArgumentParser(description="Run Cisco skill-scanner over many skills and write CSV summary.")
    ap.add_argument("--skills-root", required=True)
    ap.add_argument("--scanner-bin", default="skill-scanner")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out-csv", default="posteval/dynamic_test/skill_scanner_results.csv")
    ap.add_argument("--error-log", default="posteval/dynamic_test/skill_scanner_errors.log")
    ap.add_argument("--extra-arg", action="append", default=[], help="Extra arg passed to scanner scan command")
    args = ap.parse_args()

    root = Path(args.skills_root)
    scanner = Path(args.scanner_bin)
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"skills root not found: {root}")
    if not scanner.exists():
        raise SystemExit(f"scanner bin not found: {scanner}")

    dirs = iter_skill_dirs(root)
    if int(args.sample) > 0 and int(args.sample) < len(dirs):
        rng = random.Random(int(args.seed))
        dirs = rng.sample(dirs, int(args.sample))
        dirs.sort(key=lambda p: p.name)

    rows: List[Dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=int(args.workers)) as ex:
        futs = [ex.submit(run_one, scanner, d, list(args.extra_arg)) for d in dirs]
        for fut in as_completed(futs):
            rows.append(fut.result())

    rows.sort(key=lambda r: r["hash"])
    out = Path(args.out_csv)
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "hash",
        "skill_path",
        "ok",
        "scan_error",
        "is_safe",
        "max_severity",
        "findings_count",
        "critical",
        "high",
        "medium",
        "low",
        "info",
        "duration_ms",
        "analyzers_used",
    ]
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    err_log = Path(args.error_log)
    err_log.parent.mkdir(parents=True, exist_ok=True)
    with err_log.open("w", encoding="utf-8") as f:
        for r in rows:
            if r["ok"] != "1":
                f.write(f"{r['hash']}\t{r['scan_error']}\n")

    ok = sum(1 for r in rows if r["ok"] == "1")
    bad = len(rows) - ok
    print(json.dumps({"processed": len(rows), "ok": ok, "errors": bad, "out_csv": str(out), "error_log": str(err_log)}, indent=2))


if __name__ == "__main__":
    main()
