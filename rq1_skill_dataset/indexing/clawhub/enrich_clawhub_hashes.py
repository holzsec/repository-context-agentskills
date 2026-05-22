#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple


def hash_folder_excluding_git(folder: Path) -> Optional[str]:
    if not folder.exists() or not folder.is_dir():
        return None
    h = hashlib.sha256()
    try:
        files = sorted(folder.rglob("*"))
    except OSError:
        return None
    saw_file = False
    for p in files:
        if ".git" in p.parts:
            continue
        try:
            is_file = p.is_file()
        except OSError:
            continue
        if not is_file:
            continue
        saw_file = True
        try:
            rel = str(p.relative_to(folder)).replace("\\", "/")
            h.update(rel.encode("utf-8"))
            h.update(b"\0")
            with p.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            h.update(b"\0")
        except OSError:
            continue
    if not saw_file:
        return None
    return h.hexdigest()


def folder_last_modified_iso(folder: Path) -> Optional[str]:
    if not folder.exists() or not folder.is_dir():
        return None
    latest = None
    try:
        files = folder.rglob("*")
    except OSError:
        return None
    for p in files:
        try:
            if not p.is_file():
                continue
            m = p.stat().st_mtime
            latest = m if latest is None else max(latest, m)
        except OSError:
            continue
    if latest is None:
        return None
    return datetime.fromtimestamp(latest, tz=timezone.utc).isoformat()


def hash_one_skill_dir(skill_dir: Path) -> Tuple[str, Optional[str], Optional[str]]:
    slug = skill_dir.name
    return (slug, hash_folder_excluding_git(skill_dir), folder_last_modified_iso(skill_dir))


def ms_to_iso(v: object) -> Optional[str]:
    if v is None:
        return None
    try:
        ms = int(v)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()


def load_pipeline_skill_lookup(path: Path) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            text = line.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if str(obj.get("type", "")).strip().lower() != "skill":
                continue
            slug = str(obj.get("slug", "")).strip()
            if not slug:
                continue
            old = out.get(slug)
            if old is None:
                out[slug] = obj
                continue
            old_u = int(old.get("updatedAt", 0) or 0)
            new_u = int(obj.get("updatedAt", 0) or 0)
            if new_u > old_u:
                out[slug] = obj
    return out


def build_slug_hash_index(skills_dir: Path, jobs: int) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
    dirs = [p for p in skills_dir.iterdir() if p.is_dir()]
    out: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        futs = [ex.submit(hash_one_skill_dir, d) for d in dirs]
        for i, fut in enumerate(as_completed(futs), start=1):
            slug, h, lm = fut.result()
            out[slug] = (h, lm)
            if i % 1000 == 0:
                print(f"hashed {i}/{len(dirs)} skill directories...")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Add skill hashes to clawhub jsonl using local skill folders.")
    ap.add_argument("--input-jsonl", required=True, help="Input clawhub JSONL file")
    ap.add_argument("--skills-dir", required=True, help="Root directory containing clawhub skill folders")
    ap.add_argument(
        "--pipeline-jsonl",
        default="",
        help="Optional crawlhub pipeline.jsonl to enrich createdAt/updatedAt/stats metadata.",
    )
    ap.add_argument("--output-jsonl", required=True, help="Output JSONL file with skill_hash fields")
    ap.add_argument("--output-json", default="", help="Optional output JSON array path")
    ap.add_argument("--jobs", type=int, default=8, help="Parallel hash workers")
    args = ap.parse_args()

    input_jsonl = Path(args.input_jsonl)
    skills_dir = Path(args.skills_dir)
    output_jsonl = Path(args.output_jsonl)
    output_json = Path(args.output_json) if args.output_json else None
    pipeline_jsonl = Path(args.pipeline_jsonl) if args.pipeline_jsonl else None

    if not input_jsonl.exists():
        raise RuntimeError(f"Input JSONL not found: {input_jsonl}")
    if not skills_dir.exists() or not skills_dir.is_dir():
        raise RuntimeError(f"Skills dir not found or not a directory: {skills_dir}")

    print(f"building hash index from: {skills_dir}")
    slug_index = build_slug_hash_index(skills_dir, args.jobs)
    print(f"hashed skill folders: {len(slug_index)}")

    pipeline_lookup: Dict[str, dict] = {}
    if pipeline_jsonl:
        if not pipeline_jsonl.exists():
            raise RuntimeError(f"pipeline jsonl not found: {pipeline_jsonl}")
        print(f"loading pipeline metadata from: {pipeline_jsonl}")
        pipeline_lookup = load_pipeline_skill_lookup(pipeline_jsonl)
        print(f"pipeline skill rows indexed: {len(pipeline_lookup)}")

    output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    rows_out = []
    total = 0
    matched = 0
    with_meta = 0
    with input_jsonl.open("r", encoding="utf-8") as fin, output_jsonl.open("w", encoding="utf-8") as fout:
        for line in fin:
            text = line.strip()
            if not text:
                continue
            total += 1
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue

            slug = str(obj.get("slug", "")).strip()
            h = None
            lm_fs = None
            if slug:
                h, lm_fs = slug_index.get(slug, (None, None))
            if h:
                obj["skill_hash"] = h
                matched += 1

            pipeline_obj = pipeline_lookup.get(slug) if slug else None
            if pipeline_obj:
                with_meta += 1
                obj["displayName"] = pipeline_obj.get("displayName")
                obj["summary"] = pipeline_obj.get("summary")
                obj["latest"] = pipeline_obj.get("latest")
                obj["createdAt"] = pipeline_obj.get("createdAt")
                obj["updatedAt"] = pipeline_obj.get("updatedAt")
                obj["pipeline_ts"] = pipeline_obj.get("ts")
                obj["created_at_iso"] = ms_to_iso(pipeline_obj.get("createdAt"))
                obj["updated_at_iso"] = ms_to_iso(pipeline_obj.get("updatedAt"))
                obj["skill_uploaded_at"] = obj["created_at_iso"]
                obj["skill_updated_at"] = obj["updated_at_iso"]
                obj["stats"] = pipeline_obj.get("stats") if isinstance(pipeline_obj.get("stats"), dict) else {}
                stats = obj["stats"] if isinstance(obj["stats"], dict) else {}
                obj["stars"] = stats.get("stars")
                obj["downloads"] = stats.get("downloads")
                obj["versions"] = stats.get("versions")
                obj["installsAllTime"] = stats.get("installsAllTime")
                obj["installsCurrent"] = stats.get("installsCurrent")
                obj["comments"] = stats.get("comments")
                # Keep explicit upload and update fields; compatibility field remains "last_modified".
                obj["skill_last_modified_at"] = obj["updated_at_iso"] or obj["created_at_iso"]
            elif lm_fs:
                obj["skill_last_modified_at"] = lm_fs

            out_line = json.dumps(obj, ensure_ascii=False)
            fout.write(out_line + "\n")
            if output_json is not None:
                rows_out.append(obj)

    if output_json is not None:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(json.dumps(rows_out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"input rows:              {total}")
    print(f"rows with hash assigned: {matched}")
    print(f"rows with pipeline meta: {with_meta}")
    print(f"hash coverage:           {matched / total:.4f}" if total else "hash coverage:           n/a")
    print(f"output jsonl:            {output_jsonl}")
    if output_json is not None:
        print(f"output json:             {output_json}")


if __name__ == "__main__":
    main()
