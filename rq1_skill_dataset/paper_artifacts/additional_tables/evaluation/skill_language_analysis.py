#!/usr/bin/env python3
"""Summarize programming languages used by analyzed skills.

Language detection is intentionally suffix-based so it can operate directly on
the file metadata table produced by `code/analysis/analysis.py`.
"""

import argparse
import csv
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

SUFFIX_TO_LANGUAGE = {
    ".py": "Python",
    ".js": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".mts": "TypeScript",
    ".cts": "TypeScript",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".swift": "Swift",
    ".go": "Go",
    ".rb": "Ruby",
    ".php": "PHP",
    ".c": "C",
    ".h": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cxx": "C++",
    ".hh": "C++",
    ".hpp": "C++",
    ".hxx": "C++",
    ".cs": "C#",
    ".rs": "Rust",
    ".scala": "Scala",
    ".dart": "Dart",
    ".lua": "Lua",
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".fish": "Shell",
    ".ps1": "PowerShell",
    ".r": "R",
    ".jl": "Julia",
    ".m": "Objective-C / MATLAB",
    ".mm": "Objective-C++",
    ".svelte": "Svelte",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".erl": "Erlang",
    ".hrl": "Erlang",
    ".clj": "Clojure",
    ".cljs": "Clojure",
    ".cljc": "Clojure",
    ".fs": "F#",
    ".fsi": "F#",
    ".fsx": "F#",
    ".nim": "Nim",
    ".zig": "Zig",
}

"""
    ".sql": "SQL",
    ".html": "HTML",
    ".htm": "HTML",
    ".css": "CSS",
    ".scss": "CSS",
    ".sass": "CSS",
    ".less": "CSS",
    ".vue": "Vue",
"""


def detect_language(file_path, file_name, suffix_col):
    """Infer a language from file path, file name, or stored suffix metadata."""

    candidates = []

    for raw in (file_path, file_name):
        if not raw:
            continue
        suffix = Path(str(raw).strip()).suffix.lower()
        if suffix:
            candidates.append(suffix)

    if suffix_col:
        normalized = str(suffix_col).strip().lower()
        if normalized and not normalized.startswith("."):
            normalized = f".{normalized}"
        if normalized:
            candidates.append(normalized)

    for suffix in candidates:
        language = SUFFIX_TO_LANGUAGE.get(suffix)
        if language:
            return language

    return None


def main():
    parser = argparse.ArgumentParser(
        description="Count how many skills/apps use each programming language based on file suffixes."
    )
    parser.add_argument("--db", default="database.db", help="Path to SQLite DB")
    parser.add_argument(
        "--out-summary",
        default="analysis_outputs/skill_language_usage.csv",
        help="Output CSV with language-level counts",
    )
    parser.add_argument(
        "--out-by-app",
        default="analysis_outputs/skill_languages_by_app.csv",
        help="Output CSV with app_id-language breakdown",
    )
    parser.add_argument("--top", type=int, default=20, help="Rows to print")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        """
        SELECT f.app_id, a.app_name, f.file_path, f.file_name, f.suffix
        FROM files f
        LEFT JOIN apps a ON a.id = f.app_id
        WHERE f.app_id IS NOT NULL
        """
    ).fetchall()
    all_app_ids = {
        row[0]
        for row in conn.execute(
            """
            SELECT id
            FROM apps
            """
        ).fetchall()
    }
    conn.close()

    app_lang_file_counts = defaultdict(Counter)
    for app_id, app_name, file_path, file_name, suffix_col in rows:
        language = detect_language(file_path, file_name, suffix_col)
        if not language:
            continue
        app_key = (app_id, app_name or "")
        app_lang_file_counts[app_key][language] += 1

    language_skill_counts = Counter()
    language_file_counts = Counter()

    for _, lang_counts in app_lang_file_counts.items():
        for language, file_count in lang_counts.items():
            language_skill_counts[language] += 1
            language_file_counts[language] += file_count

    total_skills = len(all_app_ids)

    out_summary = Path(args.out_summary)
    out_summary.parent.mkdir(parents=True, exist_ok=True)
    with out_summary.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["language", "skill_count", "skill_pct_of_total", "file_count"])
        for language, skill_count in language_skill_counts.most_common():
            skill_pct_of_total = (skill_count / total_skills * 100.0) if total_skills else 0.0
            writer.writerow([language, skill_count, f"{skill_pct_of_total:.2f}", language_file_counts[language]])

    out_by_app = Path(args.out_by_app)
    out_by_app.parent.mkdir(parents=True, exist_ok=True)
    with out_by_app.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["app_id", "app_name", "language", "file_count"])
        for (app_id, app_name), lang_counts in sorted(app_lang_file_counts.items(), key=lambda x: x[0][0]):
            for language, file_count in sorted(lang_counts.items(), key=lambda x: (-x[1], x[0])):
                writer.writerow([app_id, app_name, language, file_count])

    print("Languages by number of skills:")
    for language, skill_count in language_skill_counts.most_common(args.top):
        skill_pct_of_total = (skill_count / total_skills * 100.0) if total_skills else 0.0
        print(
            f"  {language}: {skill_count} skills ({skill_pct_of_total:.2f}% of total, {language_file_counts[language]} files)"
        )

    detected_app_ids = {app_id for app_id, _ in app_lang_file_counts.keys()}
    skills_without_detected_language = len(all_app_ids - detected_app_ids)

    print(f"Total skills: {total_skills}")
    print(f"Total skills with at least one detected language: {len(app_lang_file_counts)}")
    print(f"Total skills without a detected language: {skills_without_detected_language}")
    print(f"Wrote: {out_summary}")
    print(f"Wrote: {out_by_app}")


if __name__ == "__main__":
    main()
