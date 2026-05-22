#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from evaluate_skill_openai import evaluate_zip


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run SKILLFIX repo-context checks over many zips via the OpenAI API."
    )
    parser.add_argument(
        "input_path",
        nargs="?",
        help="Zip file or directory containing zip files",
    )
    parser.add_argument(
        "--input-error-jsonl",
        help="Read bundle names from an error JSONL and rerun only those entries",
    )
    parser.add_argument(
        "--prompt-file",
        default="codex_skillfix/prompt_repo_context_eval.md",
        help="Prompt file to use",
    )
    parser.add_argument(
        "--model",
        default="gpt-5",
        help="OpenAI model name",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENAI_API_KEY"),
        help="OpenAI API key; defaults to OPENAI_API_KEY",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=300,
        help="Max output tokens per API response",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of parallel API requests",
    )
    parser.add_argument(
        "--output-jsonl",
        default="posteval/repository_context_checks_api.jsonl",
        help="Where valid responses are appended",
    )
    parser.add_argument(
        "--error-jsonl",
        default="posteval/repository_context_checks_api_errors.jsonl",
        help="Where invalid/failed responses are appended",
    )
    parser.add_argument(
        "--status-json",
        default="posteval/repository_context_checks_api_status.json",
        help="Live worker status file",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="Retries per zip after the first attempt",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip bundles already present in output or error logs",
    )
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="When resuming, do not skip bundles already present in the error log",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=10,
        help="Print progress JSON every N completed bundles; 0 disables count-based progress",
    )
    parser.add_argument(
        "--progress-seconds",
        type=float,
        default=30.0,
        help="Print progress JSON at least every N seconds during processing; 0 disables time-based progress",
    )
    parser.add_argument(
        "--test-one",
        action="store_true",
        help="Process only the first pending zip and print the raw model response on failure",
    )
    parser.add_argument(
        "--print-raw-response",
        action="store_true",
        help="When used with --test-one, print the raw model response on failure",
    )
    parser.add_argument(
        "--rate-limit-cooldown",
        type=float,
        default=20.0,
        help="Base seconds to pause all workers after a rate-limit error",
    )
    return parser.parse_args()


def load_seen(paths: list[Path]) -> set[str]:
    seen: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            bundle = obj.get("bundle")
            if isinstance(bundle, str) and bundle:
                seen.add(bundle)
    return seen


def list_zips(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    return sorted(input_path.glob("*.zip"))


def list_zips_from_error_log(error_jsonl: Path, search_root: Path) -> list[Path]:
    bundles: list[str] = []
    for line in error_jsonl.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        bundle = obj.get("bundle")
        if isinstance(bundle, str) and bundle:
            bundles.append(bundle)

    found: list[Path] = []
    missing: list[str] = []
    for bundle in bundles:
        path = search_root / bundle
        if path.exists():
            found.append(path)
        else:
            missing.append(bundle)
    if missing:
        print(
            json.dumps(
                {
                    "warning": "missing_bundles",
                    "count": len(missing),
                },
                ensure_ascii=True,
            ),
            flush=True,
        )
    return found


def append_jsonl(path: Path, obj: dict, lock: threading.Lock) -> None:
    line = json.dumps(obj, ensure_ascii=True)
    with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def is_rate_limit_error(message: str) -> bool:
    lowered = message.lower()
    return "rate limit" in lowered or "429" in lowered or "too many requests" in lowered


def wait_for_global_pause(shared_state: dict, shared_lock: threading.Lock) -> None:
    while True:
        with shared_lock:
            pause_until = float(shared_state.get("pause_until", 0.0) or 0.0)
        now = time.time()
        if pause_until <= now:
            return
        time.sleep(min(pause_until - now, 1.0))


def apply_global_pause(shared_state: dict, shared_lock: threading.Lock, cooldown_seconds: float) -> None:
    pause_target = time.time() + cooldown_seconds + random.uniform(0.0, 1.5)
    with shared_lock:
        shared_state["pause_until"] = max(float(shared_state.get("pause_until", 0.0) or 0.0), pause_target)


def write_status_json(path: Path, payload: dict, lock: threading.Lock) -> None:
    with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def emit_progress(
    done: int,
    total: int,
    ok_count: int,
    err_count: int,
    started_at: float,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
) -> None:
    elapsed = max(time.time() - started_at, 0.001)
    rate_per_min = done / elapsed * 60.0
    print(
        json.dumps(
            {
                "done": done,
                "total": total,
                "ok": ok_count,
                "errors": err_count,
                "rate_per_min": round(rate_per_min, 2),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
            },
            ensure_ascii=True,
            sort_keys=True,
        ),
        flush=True,
    )


def process_one(
    zip_path: Path,
    prompt_file: Path,
    model: str,
    api_key: str,
    max_output_tokens: int,
    retries: int,
    worker_id: int,
    status_path: Path,
    status_lock: threading.Lock,
    worker_state: dict,
    shared_state: dict,
    shared_lock: threading.Lock,
    rate_limit_cooldown: float,
) -> dict:
    worker_key = str(worker_id)
    worker_state[worker_key] = {
        "state": "running",
        "bundle": zip_path.name,
        "attempt": 0,
        "updated_at": time.time(),
    }
    write_status_json(status_path, worker_state, status_lock)
    last_error = None
    for attempt in range(retries + 1):
        wait_for_global_pause(shared_state, shared_lock)
        worker_state[worker_key] = {
            "state": "running",
            "bundle": zip_path.name,
            "attempt": attempt + 1,
            "updated_at": time.time(),
        }
        write_status_json(status_path, worker_state, status_lock)
        try:
            result, raw, usage = evaluate_zip(
                zip_path=zip_path,
                prompt_file=prompt_file,
                model=model,
                api_key=api_key,
                max_output_tokens=max_output_tokens,
            )
            result["bundle"] = zip_path.name
            worker_state[worker_key] = {
                "state": "done",
                "bundle": zip_path.name,
                "attempt": attempt + 1,
                "updated_at": time.time(),
            }
            write_status_json(status_path, worker_state, status_lock)
            return {"ok": True, "result": result, "raw": raw, "usage": usage}
        except Exception as exc:
            error_text = str(exc)
            last_error = {
                "bundle": zip_path.name,
                "error": error_text,
                "attempt": attempt + 1,
            }
            raw_text = getattr(exc, "raw_response_text", None)
            if isinstance(raw_text, str) and raw_text:
                last_error["raw_response"] = raw_text[:4000]
            if is_rate_limit_error(error_text):
                apply_global_pause(shared_state, shared_lock, rate_limit_cooldown)
                last_error["error_type"] = "rate_limit"
            worker_state[worker_key] = {
                "state": "retrying" if attempt < retries else "error",
                "bundle": zip_path.name,
                "attempt": attempt + 1,
                "error": error_text,
                "raw_response": last_error.get("raw_response"),
                "updated_at": time.time(),
            }
            write_status_json(status_path, worker_state, status_lock)
            if attempt < retries:
                time.sleep(min(2 * (attempt + 1), 5))
    assert last_error is not None
    return {"ok": False, "result": last_error}


def main() -> int:
    args = parse_args()
    if not args.api_key:
        raise SystemExit("Missing API key. Set OPENAI_API_KEY or pass --api-key.")

    prompt_file = Path(args.prompt_file).resolve()
    output_jsonl = Path(args.output_jsonl).resolve()
    error_jsonl = Path(args.error_jsonl).resolve()
    status_json = Path(args.status_json).resolve()
    input_path = Path(args.input_path).resolve() if args.input_path else None
    input_error_jsonl = Path(args.input_error_jsonl).resolve() if args.input_error_jsonl else None

    if not prompt_file.exists():
        raise SystemExit(f"Prompt file not found: {prompt_file}")
    if input_error_jsonl:
        if input_path is None:
            raise SystemExit("Provide input_path as the zip directory when using --input-error-jsonl.")
        if not input_path.exists():
            raise SystemExit(f"Input path not found: {input_path}")
        if not input_error_jsonl.exists():
            raise SystemExit(f"Input error JSONL not found: {input_error_jsonl}")
        zips = list_zips_from_error_log(input_error_jsonl, input_path)
    else:
        if input_path is None:
            raise SystemExit("input_path is required unless --input-error-jsonl is used.")
        if not input_path.exists():
            raise SystemExit(f"Input path not found: {input_path}")
        zips = list_zips(input_path)
    if not zips:
        raise SystemExit("No zip files found.")

    if args.resume:
        seen_paths = [output_jsonl]
        if not args.retry_errors:
            seen_paths.append(error_jsonl)
        seen = load_seen(seen_paths)
        zips = [zip_path for zip_path in zips if zip_path.name not in seen]

    if not zips:
        print("No remaining zips to process.")
        return 0

    if args.test_one:
        zips = zips[:1]

    lock = threading.Lock()
    status_lock = threading.Lock()
    ok_count = 0
    err_count = 0
    done = 0
    started_at = time.time()
    last_progress_at = started_at
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    shared_state = {"pause_until": 0.0}
    shared_lock = threading.Lock()
    worker_state = {
        "started_at": started_at,
        "total": len(zips),
        "done": 0,
        "ok": 0,
        "errors": 0,
        "workers": {},
    }
    write_status_json(status_json, worker_state, status_lock)

    interrupted = False
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = []
        worker_count = max(1, args.workers)
        for index, zip_path in enumerate(zips):
            worker_id = index % worker_count
            futures.append(
                executor.submit(
                    process_one,
                    zip_path,
                    prompt_file,
                    args.model,
                    args.api_key,
                    args.max_output_tokens,
                    args.retries,
                    worker_id,
                    status_json,
                    status_lock,
                    worker_state["workers"],
                    shared_state,
                    shared_lock,
                    args.rate_limit_cooldown,
                )
            )
        try:
            for future in as_completed(futures):
                outcome = future.result()
                if outcome["ok"]:
                    append_jsonl(output_jsonl, outcome["result"], lock)
                    ok_count += 1
                    usage = outcome.get("usage") or {}
                    input_tokens += int(usage.get("input_tokens", 0) or 0)
                    output_tokens += int(usage.get("output_tokens", 0) or 0)
                    total_tokens += int(usage.get("total_tokens", 0) or 0)
                else:
                    append_jsonl(error_jsonl, outcome["result"], lock)
                    err_count += 1
                    raw_response = outcome["result"].get("raw_response")
                    if args.test_one and args.print_raw_response and isinstance(raw_response, str) and raw_response:
                        print(raw_response, flush=True)

                done += 1
                worker_state["done"] = done
                worker_state["ok"] = ok_count
                worker_state["errors"] = err_count
                worker_state["input_tokens"] = input_tokens
                worker_state["output_tokens"] = output_tokens
                worker_state["total_tokens"] = total_tokens
                worker_state["pause_until"] = shared_state.get("pause_until", 0.0)
                worker_state["updated_at"] = time.time()
                write_status_json(status_json, worker_state, status_lock)

                now = time.time()
                emit_by_count = args.progress_every > 0 and done % args.progress_every == 0
                emit_by_time = args.progress_seconds > 0 and (now - last_progress_at) >= args.progress_seconds
                if emit_by_count or emit_by_time:
                    emit_progress(
                        done,
                        len(zips),
                        ok_count,
                        err_count,
                        started_at,
                        input_tokens,
                        output_tokens,
                        total_tokens,
                    )
                    last_progress_at = now
        except KeyboardInterrupt:
            interrupted = True
            for future in futures:
                future.cancel()
            worker_state["interrupted"] = True
            worker_state["updated_at"] = time.time()
            write_status_json(status_json, worker_state, status_lock)

    summary = {
        "processed": len(zips),
        "ok": ok_count,
        "errors": err_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "output_jsonl": str(output_jsonl),
        "error_jsonl": str(error_jsonl),
        "status_json": str(status_json),
        "interrupted": interrupted,
    }
    print(
        json.dumps(
            summary,
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
