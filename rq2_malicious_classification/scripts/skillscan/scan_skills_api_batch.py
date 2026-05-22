#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import hashlib
import json
import multiprocessing as mp
import os
import random
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from skill_scanner import SkillScanner
from skill_scanner.core.analyzers import (
    AIDefenseAnalyzer,
    BehavioralAnalyzer,
    LLMAnalyzer,
    MetaAnalyzer,
    StaticAnalyzer,
    TriggerAnalyzer,
)


ANALYZER_BUILDERS = {
    "static": StaticAnalyzer,
    "behavioral": BehavioralAnalyzer,
    "trigger": TriggerAnalyzer,
    "llm": LLMAnalyzer,
    "meta": MetaAnalyzer,
    "aidefense": AIDefenseAnalyzer,
}

CSV_FIELDS = [
    "hash",
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
    "analyzers_used",
    "scan_duration_seconds",
    "top_finding_id",
    "top_finding_rule_id",
    "top_finding_severity",
    "top_finding_title",
    "top_finding_description",
    "top_finding_file",
    "worker_id",
    "skipped_large",
    "timed_out",
]


def iter_skill_dirs(root: Path) -> List[Path]:
    return sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name)


def load_processed_hashes(out_csv: Path) -> set[str]:
    done: set[str] = set()
    if not out_csv.exists():
        return done
    try:
        with out_csv.open("r", newline="", encoding="utf-8") as f:
            r = csv.DictReader(f)
            for row in r:
                h = (row.get("hash") or "").strip()
                if h:
                    done.add(h)
    except Exception:
        return done
    return done


def iter_worker_csvs(worker_dir: Path) -> List[Path]:
    if not worker_dir.exists():
        return []
    return sorted(worker_dir.glob("worker_*.csv"))


def load_processed_hashes_from_csvs(csv_paths: List[Path]) -> set[str]:
    done: set[str] = set()
    for p in csv_paths:
        done |= load_processed_hashes(p)
    return done


def load_rows(csv_path: Path) -> List[Dict[str, str]]:
    if not csv_path.exists():
        return []
    out: List[Dict[str, str]] = []
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            out.append({k: str(v or "") for k, v in row.items()})
    return out


def normalize_row(row: Dict[str, str]) -> Dict[str, str]:
    return {k: str(row.get(k, "") or "") for k in CSV_FIELDS}


def stable_worker_index(skill_name: str, workers: int, seed: int) -> int:
    key = f"{int(seed)}::{skill_name}".encode("utf-8", errors="ignore")
    digest = hashlib.sha256(key).digest()
    val = int.from_bytes(digest[:8], "big", signed=False)
    return val % max(1, int(workers))


def read_json_file(path: Path) -> Dict[str, object]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def aggregate_worker_states(worker_dir: Path) -> Dict[str, float]:
    done = 0
    total = 0
    ok = 0
    errors = 0
    skipped_large = 0
    timed_out = 0
    elapsed_max = 0.0
    for p in sorted(worker_dir.glob("worker_*.state.json")):
        s = read_json_file(p)
        done += int(s.get("done", 0) or 0)
        total += int(s.get("total", 0) or 0)
        ok += int(s.get("ok", 0) or 0)
        errors += int(s.get("errors", 0) or 0)
        skipped_large += int(s.get("skipped_large", 0) or 0)
        timed_out += int(s.get("timed_out", 0) or 0)
        elapsed_max = max(elapsed_max, float(s.get("elapsed_seconds", 0.0) or 0.0))
    rate = (done / elapsed_max) if elapsed_max > 0 else 0.0
    remaining = max(total - done, 0)
    eta = int(remaining / rate) if rate > 0 else -1
    return {
        "done": done,
        "total": total,
        "ok": ok,
        "errors": errors,
        "skipped_large": skipped_large,
        "timed_out": timed_out,
        "elapsed_seconds": round(elapsed_max, 2),
        "rate_scans_per_sec": round(rate, 3),
        "remaining": remaining,
        "eta_seconds": eta,
    }


def sev_to_str(x) -> str:
    if x is None:
        return ""
    name = getattr(x, "name", None)
    if name:
        return str(name)
    return str(x)


def sev_rank(sev: str) -> int:
    s = str(sev or "").upper()
    order = {"SAFE": 0, "INFO": 1, "LOW": 2, "MEDIUM": 3, "HIGH": 4, "CRITICAL": 5}
    return order.get(s, -1)


def make_scanner(analyzers: List[str], llm_model: str, llm_api_key: str, llm_provider: str) -> SkillScanner:
    objs = []
    for a in analyzers:
        if a == "llm":
            objs.append(
                LLMAnalyzer(
                    model=llm_model,
                    api_key=llm_api_key or None,
                    provider=llm_provider or None,
                )
            )
            continue
        if a == "meta":
            objs.append(
                MetaAnalyzer(
                    model=llm_model,
                    api_key=llm_api_key or None,
                )
            )
            continue
        b = ANALYZER_BUILDERS.get(a)
        if b is None:
            raise RuntimeError(f"Unknown analyzer: {a}")
        objs.append(b())
    return SkillScanner(analyzers=objs)


def finding_to_dict(f) -> Dict[str, str]:
    return {
        "id": str(getattr(f, "id", "") or ""),
        "rule_id": str(getattr(f, "rule_id", "") or ""),
        "severity": sev_to_str(getattr(f, "severity", "")),
        "title": str(getattr(f, "title", "") or ""),
        "description": str(getattr(f, "description", "") or ""),
        "file_path": str(getattr(f, "file_path", "") or ""),
    }


def _scan_stage_worker(
    q: "mp.Queue",
    *,
    skill_dir: str,
    lenient: bool,
    mode: str,
    analyzers: List[str],
    llm_model: str,
    llm_api_key: str,
    llm_provider: str,
    suppress_stage_output: bool,
) -> None:
    try:
        if mode == "default_core":
            scanner = SkillScanner()
        elif mode == "extra":
            scanner = make_scanner(analyzers, llm_model=llm_model, llm_api_key=llm_api_key, llm_provider=llm_provider)
        else:
            q.put({"ok": False, "error": f"unknown mode: {mode}"})
            return

        if suppress_stage_output:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                r = scanner.scan_skill(skill_dir, lenient=lenient)
        else:
            r = scanner.scan_skill(skill_dir, lenient=lenient)
        q.put(
            {
                "ok": True,
                "result": {
                    "is_safe": bool(getattr(r, "is_safe", True)),
                    "max_severity": sev_to_str(getattr(r, "max_severity", "SAFE")),
                    "scan_duration_seconds": float(getattr(r, "scan_duration_seconds", 0.0) or 0.0),
                    "analyzers_used": [str(x) for x in (getattr(r, "analyzers_used", []) or [])],
                    "findings": [finding_to_dict(f) for f in (getattr(r, "findings", []) or [])],
                },
            }
        )
    except Exception as e:
        q.put({"ok": False, "error": str(e)})


def scan_stage_with_timeout(
    *,
    skill_dir: Path,
    lenient: bool,
    mode: str,
    analyzers: List[str],
    llm_model: str,
    llm_api_key: str,
    llm_provider: str,
    timeout_sec: float,
    suppress_stage_output: bool,
) -> Tuple[Optional[Dict[str, object]], Optional[str], bool]:
    try:
        ctx = mp.get_context("spawn")
        q: mp.Queue = ctx.Queue(maxsize=1)
        p = ctx.Process(
            target=_scan_stage_worker,
            kwargs={
                "q": q,
                "skill_dir": str(skill_dir),
                "lenient": bool(lenient),
                "mode": mode,
                "analyzers": list(analyzers),
                "llm_model": llm_model,
                "llm_api_key": llm_api_key,
                "llm_provider": llm_provider,
                "suppress_stage_output": bool(suppress_stage_output),
            },
            daemon=True,
        )
        p.start()
        p.join(timeout=max(float(timeout_sec), 0.1))
        if p.is_alive():
            p.terminate()
            p.join(timeout=2.0)
            return None, f"{mode}: stage timeout>{int(timeout_sec)}s", True
        if p.exitcode != 0 and q.empty():
            return None, f"{mode}: worker exited with code {p.exitcode}", False
        try:
            payload = q.get_nowait()
        except Exception:
            return None, f"{mode}: no worker payload", False
        if not bool(payload.get("ok")):
            return None, str(payload.get("error", "unknown stage error")), False
        return payload.get("result"), None, False
    except Exception as e:
        return None, f"{mode}: {e}", False


def scan_one(
    skill_dir: Path,
    analyzers: List[str],
    use_default_core: bool,
    lenient: bool,
    llm_model: str,
    llm_api_key: str,
    llm_provider: str,
    stage_timeout_sec: float,
    hard_timeout_sec: float,
    suppress_stage_output: bool,
) -> Dict[str, str]:
    row: Dict[str, str] = {
        "hash": skill_dir.name,
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
        "analyzers_used": "",
        "scan_duration_seconds": "",
        "top_finding_id": "",
        "top_finding_rule_id": "",
        "top_finding_severity": "",
        "top_finding_title": "",
        "top_finding_description": "",
        "top_finding_file": "",
        "worker_id": str(threading.get_ident()),
        "skipped_large": "0",
        "timed_out": "0",
    }

    results: List[Dict[str, object]] = []
    errors: List[str] = []
    analyzers_used: List[str] = []
    t_skill_start = time.time()

    stages: List[Tuple[str, List[str]]] = []
    if use_default_core:
        stages.append(("default_core", []))
    if analyzers:
        stages.append(("extra", analyzers))

    for mode, stage_analyzers in stages:
        elapsed = time.time() - t_skill_start
        remaining_hard = float(hard_timeout_sec) - elapsed
        if remaining_hard <= 0:
            row["scan_error"] = f"hard timeout>{int(hard_timeout_sec)}s"
            row["timed_out"] = "1"
            return row
        budget = min(float(stage_timeout_sec), remaining_hard)
        stage_res, stage_err, stage_timed_out = scan_stage_with_timeout(
            skill_dir=skill_dir,
            lenient=lenient,
            mode=mode,
            analyzers=stage_analyzers,
            llm_model=llm_model,
            llm_api_key=llm_api_key,
            llm_provider=llm_provider,
            timeout_sec=budget,
            suppress_stage_output=suppress_stage_output,
        )
        if stage_timed_out:
            row["scan_error"] = stage_err or f"{mode}: timeout"
            row["timed_out"] = "1"
            return row
        if stage_err:
            errors.append(f"{mode}: {stage_err}")
            continue
        if stage_res is not None:
            results.append(stage_res)
            analyzers_used.extend([str(x) for x in (stage_res.get("analyzers_used") or [])])

    if not results:
        row["scan_error"] = " | ".join(errors)[:2000]
        return row

    try:
        all_findings: List[Dict[str, str]] = []
        max_sev = "SAFE"
        is_safe = True
        dur = 0.0
        for r in results:
            dur += float(r.get("scan_duration_seconds", 0.0) or 0.0)
            is_safe = bool(r.get("is_safe", True)) and is_safe
            sev = sev_to_str(r.get("max_severity", "SAFE"))
            if sev_rank(sev) > sev_rank(max_sev):
                max_sev = sev
            for f in (r.get("findings") or []):
                all_findings.append(dict(f))

        findings = all_findings
        row["ok"] = "1"
        row["is_safe"] = str(is_safe)
        row["max_severity"] = str(max_sev)
        row["findings_count"] = str(len(findings))
        row["scan_duration_seconds"] = str(dur)
        row["analyzers_used"] = ",".join(sorted(set(analyzers_used)))

        sev_counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "INFO": 0}
        for f in findings:
            s = str(f.get("severity", "")).upper()
            if s in sev_counts:
                sev_counts[s] += 1
        row["critical"] = str(sev_counts["CRITICAL"])
        row["high"] = str(sev_counts["HIGH"])
        row["medium"] = str(sev_counts["MEDIUM"])
        row["low"] = str(sev_counts["LOW"])
        row["info"] = str(sev_counts["INFO"])

        if findings:
            findings_sorted = sorted(findings, key=lambda x: sev_rank(str(x.get("severity", ""))), reverse=True)
            top = findings_sorted[0]
            row["top_finding_id"] = top.get("id", "")
            row["top_finding_rule_id"] = top.get("rule_id", "")
            row["top_finding_severity"] = top.get("severity", "")
            row["top_finding_title"] = top.get("title", "")
            row["top_finding_description"] = top.get("description", "")
            row["top_finding_file"] = top.get("file_path", "")

        if errors:
            row["scan_error"] = " | ".join(errors)[:2000]
    except Exception as e:
        row["ok"] = "0"
        row["scan_error"] = f"result parse error: {e}"[:2000]
    return row


def run_worker_shard(
    *,
    worker_idx: int,
    dirs: List[Path],
    worker_out_csv: Path,
    worker_err_log: Path,
    worker_state_json: Path,
    analyzers: List[str],
    use_default_core: bool,
    lenient: bool,
    llm_model: str,
    llm_api_key: str,
    llm_provider: str,
    stage_timeout_sec: float,
    hard_timeout_sec: float,
    progress_every: int,
    resume: bool,
    suppress_stage_output: bool,
    shared_state: Optional[Dict[str, float]] = None,
    shared_lock: Optional[threading.Lock] = None,
) -> Dict[str, int]:
    worker_out_csv.parent.mkdir(parents=True, exist_ok=True)
    worker_err_log.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if (resume and worker_out_csv.exists()) else "w"
    need_header = not worker_out_csv.exists() or mode == "w"

    done = 0
    ok = 0
    err = 0
    skipped_large = 0
    timed_out = 0
    total = len(dirs)
    started = time.time()

    def write_state() -> None:
        elapsed = max(time.time() - started, 1e-9)
        remaining = max(total - done, 0)
        rate = done / elapsed if done > 0 else 0.0
        eta = int(remaining / rate) if rate > 0 else -1
        payload = {
            "worker": worker_idx,
            "total": total,
            "done": done,
            "ok": ok,
            "errors": err,
            "skipped_large": skipped_large,
            "timed_out": timed_out,
            "remaining": remaining,
            "elapsed_seconds": round(elapsed, 2),
            "rate_scans_per_sec": round(rate, 4),
            "eta_seconds": eta,
            "updated_unix": int(time.time()),
        }
        worker_state_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    with worker_out_csv.open(mode, newline="", encoding="utf-8") as out_f, worker_err_log.open(
        "a" if resume else "w", encoding="utf-8"
    ) as err_f:
        writer = csv.DictWriter(out_f, fieldnames=CSV_FIELDS)
        if need_header:
            writer.writeheader()
            out_f.flush()

        for d in dirs:
            row = scan_one(
                d,
                analyzers,
                use_default_core,
                lenient,
                llm_model,
                llm_api_key,
                llm_provider,
                stage_timeout_sec,
                hard_timeout_sec,
                suppress_stage_output,
            )
            row["worker_id"] = str(worker_idx)
            writer.writerow(normalize_row(row))
            out_f.flush()

            done += 1
            if row.get("ok") == "1":
                ok += 1
            else:
                err += 1
                err_f.write(f"{row['hash']}\t{row.get('scan_error','')}\n")
                err_f.flush()
            if row.get("skipped_large") == "1":
                skipped_large += 1
            if row.get("timed_out") == "1":
                timed_out += 1

            if shared_state is not None and shared_lock is not None:
                with shared_lock:
                    shared_state["done"] = float(shared_state.get("done", 0.0) + 1.0)
                    if row.get("ok") == "1":
                        shared_state["ok"] = float(shared_state.get("ok", 0.0) + 1.0)
                    else:
                        shared_state["errors"] = float(shared_state.get("errors", 0.0) + 1.0)
                    if row.get("skipped_large") == "1":
                        shared_state["skipped_large"] = float(shared_state.get("skipped_large", 0.0) + 1.0)
                    if row.get("timed_out") == "1":
                        shared_state["timed_out"] = float(shared_state.get("timed_out", 0.0) + 1.0)

            if progress_every > 0 and (done % progress_every == 0 or done == total):
                write_state()

    write_state()
    return {
        "worker": worker_idx,
        "done": done,
        "ok": ok,
        "errors": err,
        "skipped_large": skipped_large,
        "timed_out": timed_out,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Run SkillScanner Python API over many skills and write CSV summary.")
    ap.add_argument("--skills-root", required=True)
    ap.add_argument(
        "--analyzers",
        default="behavioral",
        help="Comma list: static,behavioral,trigger,llm,meta,aidefense",
    )
    ap.add_argument(
        "--use-default-core",
        action="store_true",
        default=True,
        help="Also run default core scanner (static+bytecode+pipeline) [default: on]",
    )
    ap.add_argument(
        "--no-default-core",
        action="store_true",
        help="Disable default core scanner (static+bytecode+pipeline)",
    )
    ap.add_argument("--use-llm", action="store_true", help="Append LLM analyzer on top of defaults")
    ap.add_argument("--llm-model", default="gpt-5-nano", help="LLM model for llm/meta analyzers (default: gpt-5-nano)")
    ap.add_argument("--llm-provider", default="openai", help="LLM provider for llm analyzer (default: openai)")
    ap.add_argument("--llm-api-key", default="", help="Optional API key override (otherwise uses env)")
    ap.add_argument("--lenient", action="store_true", help="Use lenient parsing (tolerate missing fields like name)")
    ap.add_argument("--workers", type=int, default=max(8, min(64, (os.cpu_count() or 8) * 2)))
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument(
        "--progress-every",
        type=int,
        default=100,
        help="Flush worker state files every N completed scans (heartbeat output is controlled by --heartbeat-seconds).",
    )
    ap.add_argument("--resume", action="store_true", default=True, help="Skip hashes already present in out CSV [default: on]")
    ap.add_argument("--no-resume", action="store_true", help="Disable resume and overwrite output CSV")
    ap.add_argument("--recreate", action="store_true", help="Delete prior out/error/state and worker files before run")
    ap.add_argument(
        "--stage-timeout-seconds",
        type=float,
        default=30.0,
        help="Hard kill a stage if it exceeds this runtime in seconds (default: 30).",
    )
    ap.add_argument(
        "--hard-timeout-seconds",
        type=float,
        default=120.0,
        help="Hard cap for entire skill scan runtime in seconds (default: 120).",
    )
    ap.add_argument(
        "--worker-dir",
        default="",
        help="Directory for per-worker csv/log/state files (default: <out_csv_stem>_workers under out dir).",
    )
    ap.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=10.0,
        help="Print global aggregated progress heartbeat every X seconds (<=0 disables).",
    )
    ap.add_argument(
        "--no-suppress-stage-output",
        action="store_true",
        help="Do not suppress analyzer stdout/stderr messages (default: suppressed).",
    )
    ap.add_argument("--state-json", default="", help="Optional path to write resumable progress state JSON")
    ap.add_argument("--out-csv", default="posteval/dynamic_test/skill_scanner_py_results.csv")
    ap.add_argument("--error-log", default="posteval/dynamic_test/skill_scanner_py_errors.log")
    args = ap.parse_args()

    root = Path(args.skills_root)
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"skills root not found: {root}")

    analyzers = [x.strip().lower() for x in str(args.analyzers).split(",") if x.strip()]
    if bool(args.use_llm) and "llm" not in analyzers:
        analyzers.append("llm")
    if not analyzers:
        raise SystemExit("No analyzers selected")
    bad = [a for a in analyzers if a not in ANALYZER_BUILDERS]
    if bad:
        raise SystemExit(f"Unknown analyzers: {bad}")

    out = Path(args.out_csv)
    out.parent.mkdir(parents=True, exist_ok=True)
    err_log = Path(args.error_log)
    err_log.parent.mkdir(parents=True, exist_ok=True)
    state_json = Path(args.state_json) if str(args.state_json).strip() else out.with_suffix(".state.json")
    worker_dir = Path(args.worker_dir) if str(args.worker_dir).strip() else (out.parent / f"{out.stem}_workers")
    worker_dir.mkdir(parents=True, exist_ok=True)

    if bool(args.recreate):
        for p in [out, err_log, state_json]:
            if p.exists() and p.is_file():
                p.unlink()
        if worker_dir.exists():
            for p in worker_dir.glob("*"):
                if p.is_file():
                    p.unlink()

    resume = bool(args.resume) and not bool(args.no_resume)
    processed_hashes: set[str] = set()
    if resume:
        processed_hashes |= load_processed_hashes(out)
        processed_hashes |= load_processed_hashes_from_csvs(iter_worker_csvs(worker_dir))

    dirs = iter_skill_dirs(root)
    if processed_hashes:
        dirs = [d for d in dirs if d.name not in processed_hashes]

    if int(args.sample) > 0 and int(args.sample) < len(dirs):
        rng = random.Random(int(args.seed))
        dirs = rng.sample(dirs, int(args.sample))
        dirs.sort(key=lambda p: p.name)

    use_default_core = bool(args.use_default_core) and not bool(args.no_default_core)
    total = len(dirs)
    done = 0
    ok_count = 0
    err_count = 0
    skip_large_count = 0
    timeout_count = 0
    started = time.time()

    stage_timeout_sec = float(args.stage_timeout_seconds)
    hard_timeout_sec = float(args.hard_timeout_seconds)
    suppress_stage_output = not bool(args.no_suppress_stage_output)

    def write_state() -> None:
        remaining = max(total - done, 0)
        elapsed = max(time.time() - started, 1e-9)
        rate = done / elapsed if done > 0 else 0.0
        eta_s = int(remaining / rate) if rate > 0 else -1
        state = {
            "skills_root": str(root),
            "out_csv": str(out),
            "error_log": str(err_log),
            "worker_dir": str(worker_dir),
            "resume": resume,
            "already_processed_before_run": len(processed_hashes),
            "scheduled_this_run": total,
            "completed_this_run": done,
            "ok_this_run": ok_count,
            "errors_this_run": err_count,
            "skipped_large_this_run": skip_large_count,
            "timed_out_this_run": timeout_count,
            "remaining": remaining,
            "elapsed_seconds": round(elapsed, 2),
            "rate_scans_per_sec": round(rate, 4),
            "eta_seconds": eta_s,
            "updated_unix": int(time.time()),
        }
        state_json.parent.mkdir(parents=True, exist_ok=True)
        state_json.write_text(json.dumps(state, indent=2), encoding="utf-8")

    workers = max(1, int(args.workers))
    seed = int(args.seed)
    shards: List[List[Path]] = [[] for _ in range(workers)]
    for d in dirs:
        wi = stable_worker_index(d.name, workers, seed)
        shards[wi].append(d)
    for wi in range(workers):
        shards[wi].sort(key=lambda p: p.name)

    pe_worker = max(1, int(args.progress_every))
    heartbeat_sec = float(args.heartbeat_seconds)
    hb_lock = threading.Lock()
    hb_state: Dict[str, float] = {
        "done": 0.0,
        "total": float(total),
        "ok": 0.0,
        "errors": 0.0,
        "skipped_large": 0.0,
        "timed_out": 0.0,
    }

    def print_heartbeat() -> None:
        with hb_lock:
            done_hb = int(hb_state.get("done", 0.0))
            total_hb = int(hb_state.get("total", 0.0))
            ok_hb = int(hb_state.get("ok", 0.0))
            err_hb = int(hb_state.get("errors", 0.0))
            sk_hb = int(hb_state.get("skipped_large", 0.0))
            to_hb = int(hb_state.get("timed_out", 0.0))
        if total_hb <= 0:
            return
        elapsed = max(time.time() - started, 1e-9)
        rate = done_hb / elapsed if done_hb > 0 else 0.0
        remaining = max(total_hb - done_hb, 0)
        eta = int(remaining / rate) if rate > 0 else -1
        print(
            json.dumps(
                {
                    "global_heartbeat": True,
                    "progress_done": done_hb,
                    "progress_total": total_hb,
                    "ok_so_far": ok_hb,
                    "errors_so_far": err_hb,
                    "skipped_large_so_far": sk_hb,
                    "timed_out_so_far": to_hb,
                    "remaining": remaining,
                    "elapsed_seconds": round(elapsed, 2),
                    "rate_scans_per_sec": round(rate, 3),
                    "eta_seconds": eta,
                }
            ),
            flush=True,
        )

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = []
        for wi, shard in enumerate(shards):
            if not shard:
                continue
            futs.append(
                ex.submit(
                    run_worker_shard,
                    worker_idx=wi,
                    dirs=shard,
                    worker_out_csv=worker_dir / f"worker_{wi:03d}.csv",
                    worker_err_log=worker_dir / f"worker_{wi:03d}.log",
                    worker_state_json=worker_dir / f"worker_{wi:03d}.state.json",
                    analyzers=analyzers,
                    use_default_core=use_default_core,
                    lenient=bool(args.lenient),
                    llm_model=str(args.llm_model),
                    llm_api_key=str(args.llm_api_key),
                    llm_provider=str(args.llm_provider),
                    stage_timeout_sec=stage_timeout_sec,
                    hard_timeout_sec=hard_timeout_sec,
                    progress_every=pe_worker,
                    resume=resume,
                    suppress_stage_output=suppress_stage_output,
                    shared_state=hb_state,
                    shared_lock=hb_lock,
                )
            )
        pending = set(futs)
        last_hb = time.time()
        while pending:
            if heartbeat_sec > 0:
                done_set, pending = wait(pending, timeout=heartbeat_sec, return_when=FIRST_COMPLETED)
                now = time.time()
                if (now - last_hb) >= heartbeat_sec:
                    print_heartbeat()
                    last_hb = now
            else:
                done_set, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done_set:
                s = fut.result()
                done += int(s.get("done", 0))
                ok_count += int(s.get("ok", 0))
                err_count += int(s.get("errors", 0))
                skip_large_count += int(s.get("skipped_large", 0))
                timeout_count += int(s.get("timed_out", 0))
                write_state()

    # Aggregate final output from prior final CSV + all worker CSVs.
    final_rows: Dict[str, Dict[str, str]] = {}
    if resume and out.exists():
        for row in load_rows(out):
            h = (row.get("hash") or "").strip()
            if h:
                final_rows[h] = normalize_row(row)
    for p in iter_worker_csvs(worker_dir):
        for row in load_rows(p):
            h = (row.get("hash") or "").strip()
            if h:
                final_rows[h] = normalize_row(row)

    with out.open("w", newline="", encoding="utf-8") as out_f:
        w = csv.DictWriter(out_f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for h in sorted(final_rows.keys()):
            w.writerow(final_rows[h])

    with err_log.open("w", encoding="utf-8") as err_f:
        for h in sorted(final_rows.keys()):
            r = final_rows[h]
            if r.get("ok") != "1":
                err_f.write(f"{h}\t{r.get('scan_error','')}\n")

    write_state()

    print(
        json.dumps(
            {
                "processed_this_run": done,
                "ok_this_run": ok_count,
                "errors_this_run": err_count,
                "skipped_large_this_run": skip_large_count,
                "timed_out_this_run": timeout_count,
                "already_processed_before_run": len(processed_hashes),
                "analyzers": analyzers,
                "use_default_core": use_default_core,
                "use_llm": bool(args.use_llm),
                "llm_model": str(args.llm_model),
                "llm_provider": str(args.llm_provider),
                "lenient": bool(args.lenient),
                "workers": int(args.workers),
                "resume": resume,
                "recreate": bool(args.recreate),
                "stage_timeout_seconds": stage_timeout_sec,
                "hard_timeout_seconds": hard_timeout_sec,
                "suppress_stage_output": suppress_stage_output,
                "out_csv": str(out),
                "error_log": str(err_log),
                "state_json": str(state_json),
                "worker_dir": str(worker_dir),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
