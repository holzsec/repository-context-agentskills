#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI


README_NAMES = {"readme", "readme.md", "readme.rst", "readme.txt", "readme.mdx"}
SKILL_MARKDOWN_NAME = "skill.md"
SKILL_DIR_MARKERS = {"/skills/", "/skill/", "/.claude/skills/", "/.cursor/skills/", "/.agents/skills/"}
CODE_EXTS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".rb",
    ".php",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".swift",
    ".scala",
    ".sh",
    ".sql",
}
SKILL_CODE_EXTS = CODE_EXTS | {".mjs", ".cjs"}
EXCLUDE_PARTS = {
    ".git",
    "node_modules",
    "dist",
    "build",
    "coverage",
    "__pycache__",
    ".next",
    "vendor",
    "target",
}
MAX_LINE_CHARS = 300
MAX_SNIPPET_CHARS = 12000
MAX_TOTAL_PAYLOAD_CHARS = 28000

REQUIRED_RESPONSE_KEYS = {
    "r",
    "s",
    "c",
    "dm",
    "cm",
    "rm",
    "mal",
    "sec",
    "v",
    "cf",
    "why",
}

ALLOWED_VALUES = {
    "r": {"0", "1"},
    "s": {"0", "1"},
    "c": {"0", "1"},
    "dm": {"e", "s", "n"},
    "cm": {"e", "s", "n", "na"},
    "rm": {"e", "s", "n", "na"},
    "mal": {"e", "s", "n"},
    "sec": {"e", "s", "n"},
    "v": {"ab", "as", "na", "i"},
    "cf": {"h", "m", "l"},
}

NORMALIZE_MAP = {
    "dm": {
        "evidence": "e",
        "some evidence": "s",
        "no evidence": "n",
    },
    "cm": {
        "evidence": "e",
        "some evidence": "s",
        "no evidence": "n",
        "not applicable": "na",
    },
    "rm": {
        "evidence": "e",
        "some evidence": "s",
        "no evidence": "n",
        "not applicable": "na",
    },
    "mal": {
        "evidence": "e",
        "some evidence": "s",
        "no evidence": "n",
    },
    "sec": {
        "evidence": "e",
        "some evidence": "s",
        "no evidence": "n",
    },
    "v": {
        "aligned_and_benign": "ab",
        "aligned_but_repo_suspicious": "as",
        "not_aligned": "na",
        "inconclusive": "i",
    },
    "cf": {
        "high": "h",
        "medium": "m",
        "low": "l",
    },
}


@dataclass
class Snippet:
    path: str
    kind: str
    text: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one SKILLFIX repository-context evaluation via the OpenAI API."
    )
    parser.add_argument("zip_path", help="Path to one bundled skill/repo zip")
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
        help="Max output tokens for the API response",
    )
    parser.add_argument(
        "--print-raw-response",
        action="store_true",
        help="Print the raw model response to stderr before validation",
    )
    return parser.parse_args()


def read_text_from_zip(zf: zipfile.ZipFile, member: str, max_lines: int) -> str:
    try:
        raw = zf.read(member)
    except KeyError:
        return ""
    text = raw.decode("utf-8", errors="replace")
    lines = []
    for line in text.splitlines()[:max_lines]:
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + " ..."
        lines.append(line)
    clipped = "\n".join(lines).strip()
    return clipped[:MAX_SNIPPET_CHARS]


def normalize_path(name: str) -> str:
    return name.replace("\\", "/")


def path_parts(name: str) -> list[str]:
    return [part for part in normalize_path(name).split("/") if part]


def is_readme(name: str) -> bool:
    base = Path(name).name.lower()
    return base in README_NAMES or base.startswith("readme.")


def is_skill_markdown(name: str) -> bool:
    return Path(name).name.lower() == SKILL_MARKDOWN_NAME


def looks_like_skill_dir(name: str) -> bool:
    lowered = normalize_path(name).lower()
    return any(marker in lowered for marker in SKILL_DIR_MARKERS)


def is_excluded(name: str) -> bool:
    parts = set(path_parts(name))
    return any(part in parts for part in EXCLUDE_PARTS)


def is_code_file(name: str) -> bool:
    return Path(name).suffix.lower() in CODE_EXTS


def is_skill_code_file(name: str) -> bool:
    return Path(name).suffix.lower() in SKILL_CODE_EXTS


def score_repo_file(name: str) -> tuple[int, int, str]:
    lowered = normalize_path(name).lower()
    score = 0
    if any(seg in lowered for seg in ("/src/", "/app/", "/lib/", "/server/", "/api/")):
        score += 4
    if any(seg in lowered for seg in ("/scripts/", "/cmd/", "/internal/", "/pkg/")):
        score += 2
    if Path(name).suffix.lower() in {".ts", ".tsx", ".py", ".go", ".rs", ".js"}:
        score += 3
    depth_penalty = len(path_parts(name))
    return (-score, depth_penalty, lowered)


def looks_minified(text: str) -> bool:
    lines = text.splitlines()
    if not lines:
        return False
    long_lines = sum(1 for line in lines if len(line) > 500)
    avg_len = sum(len(line) for line in lines) / max(len(lines), 1)
    return long_lines >= 3 or avg_len > 220


def discover_members(zf: zipfile.ZipFile) -> tuple[str | None, str | None, list[str], bool, bool]:
    names = [normalize_path(i.filename) for i in zf.infolist() if not i.is_dir()]
    skill_md = next((n for n in names if is_skill_markdown(n)), None)
    readme = next((n for n in names if is_readme(n) and not looks_like_skill_dir(n)), None)

    skill_dir = None
    if skill_md:
        skill_dir = str(Path(skill_md).parent).rstrip("/")

    skill_code_exists = False
    repo_code_exists = False
    repo_files: list[str] = []
    for name in names:
        if is_excluded(name):
            continue
        inside_skill = bool(skill_dir) and (
            name == skill_dir or name.startswith(skill_dir + "/")
        )
        if inside_skill and is_skill_code_file(name):
            skill_code_exists = True
        if inside_skill:
            continue
        if is_code_file(name):
            repo_code_exists = True
            repo_files.append(name)

    repo_files = sorted(repo_files, key=score_repo_file)[:3]
    return skill_md, readme, repo_files, skill_code_exists, repo_code_exists


def build_payload(
    zip_path: Path,
    skill_md_path: str | None,
    readme_path: str | None,
    repo_file_paths: list[str],
    skill_code_exists: bool,
    repo_code_exists: bool,
) -> str:
    with zipfile.ZipFile(zip_path) as zf:
        skill_md_text = read_text_from_zip(zf, skill_md_path, 200) if skill_md_path else ""
        readme_text = read_text_from_zip(zf, readme_path, 200) if readme_path else ""
        repo_files = []
        for path in repo_file_paths:
            text = read_text_from_zip(zf, path, 100)
            if looks_minified(text):
                continue
            repo_files.append({"path": path, "text": text})

    payload = {
        "presence": {
            "readme_exists": bool(readme_path),
            "skill_code_exists": skill_code_exists,
            "repo_code_exists": repo_code_exists,
        },
        "skill_md": {
            "path": skill_md_path,
            "text": skill_md_text,
        },
        "readme": {
            "path": readme_path,
            "text": readme_text,
        },
        "repo_files": repo_files,
    }
    serialized = json.dumps(payload, ensure_ascii=True, indent=2)
    if len(serialized) <= MAX_TOTAL_PAYLOAD_CHARS:
        return serialized

    if readme_text:
        payload["readme"]["text"] = readme_text[: min(len(readme_text), 6000)]
        serialized = json.dumps(payload, ensure_ascii=True, indent=2)
    if len(serialized) <= MAX_TOTAL_PAYLOAD_CHARS:
        return serialized

    trimmed_repo_files = []
    for file_obj in payload["repo_files"][:2]:
        trimmed_repo_files.append(
            {
                "path": file_obj["path"],
                "text": str(file_obj["text"])[:4000],
            }
        )
    payload["repo_files"] = trimmed_repo_files
    serialized = json.dumps(payload, ensure_ascii=True, indent=2)
    if len(serialized) <= MAX_TOTAL_PAYLOAD_CHARS:
        return serialized

    if skill_md_text:
        payload["skill_md"]["text"] = skill_md_text[:8000]
    return json.dumps(payload, ensure_ascii=True, indent=2)[:MAX_TOTAL_PAYLOAD_CHARS]


def extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        err = ValueError("Model output did not contain a JSON object")
        setattr(err, "raw_response_text", text)
        raise err
    return text[start : end + 1]


def derive_repo_name_skill(zip_name: str) -> str:
    stem = Path(zip_name).stem
    parts = stem.split("__")
    if len(parts) < 4:
        return stem

    owner = parts[0]
    repo = parts[1]
    bundle_hash = parts[2]
    tail = parts[3]
    marker = f"_{owner}_{repo}_"
    marker_index = tail.find(marker)
    if marker_index != -1:
        skill_name = tail[marker_index + len(marker) :]
        if skill_name:
            return f"{repo}_{skill_name}_{bundle_hash}"
    return f"{repo}_{tail}_{bundle_hash}"


def validate_response_object(obj: object) -> dict:
    if not isinstance(obj, dict):
        raise ValueError("Response is not a JSON object")
    keys = set(obj.keys())
    missing = REQUIRED_RESPONSE_KEYS - keys
    extra = keys - REQUIRED_RESPONSE_KEYS
    if missing:
        raise ValueError(f"Missing keys: {sorted(missing)}")
    if extra:
        raise ValueError(f"Unexpected keys: {sorted(extra)}")
    for key, aliases in NORMALIZE_MAP.items():
        value = obj.get(key)
        if isinstance(value, str):
            normalized = aliases.get(value.strip().lower())
            if normalized:
                obj[key] = normalized
    for key, allowed in ALLOWED_VALUES.items():
        value = obj.get(key)
        if not isinstance(value, str) or value not in allowed:
            raise ValueError(f"Invalid value for {key}: {value!r}")
    why = obj.get("why")
    if not isinstance(why, str) or not why.strip():
        raise ValueError("Field 'why' must be a non-empty string")
    return obj


def attach_raw_response(exc: Exception, raw: str) -> Exception:
    if raw:
        setattr(exc, "raw_response_text", raw)
    return exc


def attach_debug_response(exc: Exception, response: object) -> Exception:
    try:
        debug_text = response.model_dump_json(indent=2)
    except Exception:
        debug_text = repr(response)
    setattr(exc, "debug_response_text", debug_text)
    return exc


def evaluate_zip(
    zip_path: Path,
    prompt_file: Path,
    model: str,
    api_key: str,
    max_output_tokens: int,
) -> tuple[dict, str, dict]:
    with zipfile.ZipFile(zip_path) as zf:
        skill_md_path, readme_path, repo_file_paths, skill_code_exists, repo_code_exists = discover_members(zf)

    prompt_text = prompt_file.read_text(encoding="utf-8")
    if "json" not in prompt_text.lower():
        prompt_text = prompt_text.rstrip() + "\n\nReturn valid json only."
    payload_text = build_payload(
        zip_path=zip_path,
        skill_md_path=skill_md_path,
        readme_path=readme_path,
        repo_file_paths=repo_file_paths,
        skill_code_exists=skill_code_exists,
        repo_code_exists=repo_code_exists,
    )
    payload_text = "Return json only.\n" + payload_text

    client = OpenAI(api_key=api_key)
    response = client.responses.create(
        model=model,
        instructions=prompt_text,
        input=payload_text,
        max_output_tokens=max_output_tokens,
        reasoning={"effort": "minimal"},
        text={"format": {"type": "json_object"}, "verbosity": "low"},
    )
    raw = (getattr(response, "output_text", None) or "").strip()
    try:
        obj = json.loads(extract_json(raw))
    except Exception as exc:
        exc = attach_raw_response(exc, raw)
        raise attach_debug_response(exc, response)
    usage = getattr(response, "usage", None)
    usage_dict = {
        "input_tokens": int(getattr(usage, "input_tokens", 0) or getattr(usage, "prompt_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or getattr(usage, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
    }
    try:
        obj = validate_response_object(obj)
    except Exception as exc:
        exc = attach_raw_response(exc, raw)
        raise attach_debug_response(exc, response)
    obj["repo_name_skill"] = derive_repo_name_skill(zip_path.name)
    return obj, raw, usage_dict


def main() -> int:
    args = parse_args()
    if not args.api_key:
        print("Missing API key. Set OPENAI_API_KEY or pass --api-key.", file=sys.stderr)
        return 2

    zip_path = Path(args.zip_path).resolve()
    if not zip_path.exists():
        print(f"Zip not found: {zip_path}", file=sys.stderr)
        return 2

    prompt_file = Path(args.prompt_file).resolve()
    if not prompt_file.exists():
        print(f"Prompt file not found: {prompt_file}", file=sys.stderr)
        return 2

    try:
        obj, raw, _usage = evaluate_zip(
            zip_path=zip_path,
            prompt_file=prompt_file,
            model=args.model,
            api_key=args.api_key,
            max_output_tokens=args.max_output_tokens,
        )
    except Exception as exc:
        raw = getattr(exc, "raw_response_text", "")
        debug = getattr(exc, "debug_response_text", "")
        if args.print_raw_response and raw:
            print(raw, file=sys.stderr)
        elif args.print_raw_response and debug:
            print(debug, file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 1

    if args.print_raw_response:
        print(raw, file=sys.stderr)
    print(json.dumps(obj, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
