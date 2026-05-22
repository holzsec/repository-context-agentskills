#!/usr/bin/env python3
import argparse
import json
from collections import Counter
from pathlib import Path


def load_rows(jsonl_path: Path):
    rows = []
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def build_log(rows):
    category_fields = [
        "domain_match",
        "code_match",
        "readme_match",
        "repo_maliciousness",
        "security_tooling",
    ]
    reason_fields = [
        "domain_reason",
        "code_reason",
        "readme_reason",
        "repo_maliciousness_reason",
        "security_tooling_reason",
    ]

    lines = [f"TOTAL_RECORDS\t{len(rows)}", "", "RATING_COUNTS"]

    for field in category_fields:
        counts = Counter(row.get(field, "<missing>") for row in rows)
        lines.append(f"[{field}]")
        for value, count in counts.most_common():
            lines.append(f"{value}\t{count}")
        lines.append("")

    lines.append("TOP_5_REASONS_PER_CATEGORY")
    for field in reason_fields:
        counts = Counter(row.get(field, "<missing>") for row in rows)
        lines.append(f"[{field}]")
        for reason, count in counts.most_common(5):
            normalized = " ".join(str(reason).split())
            lines.append(f"{count}\t{normalized}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main():
    parser = argparse.ArgumentParser(
        description="Generate repository context rating/reason summary log."
    )
    parser.add_argument(
        "--input",
        default="repository_context_checks.jsonl",
        help="Input JSONL filename (default: repository_context_checks.jsonl)",
    )
    parser.add_argument(
        "--output",
        default="repository_context_checks.log",
        help="Output log filename (default: repository_context_checks.log)",
    )
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    input_path = (script_dir / args.input).resolve()
    output_path = (script_dir / args.output).resolve()

    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    rows = load_rows(input_path)
    output_path.write_text(build_log(rows), encoding="utf-8")
    print(output_path)


if __name__ == "__main__":
    main()
