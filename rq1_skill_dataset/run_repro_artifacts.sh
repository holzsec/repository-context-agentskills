#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$(cd "$SCRIPT_DIR/../data/rq1_skill_dataset" && pwd)"
OUT_DIR="$SCRIPT_DIR/generated"

mkdir -p "$OUT_DIR"
export MPLCONFIGDIR="${MPLCONFIGDIR:-$OUT_DIR/.matplotlib}"
mkdir -p "$MPLCONFIGDIR"

INPUT_DB="$DATA_DIR/source/sqlite.db"
INDEXED_DB="$DATA_DIR/intermediate/full_marketplace.db"
UNIQUE_SKILLS="$DATA_DIR/unique_skills.txt"
CLAW_JSONL="$DATA_DIR/source/clawhub_hashed.jsonl"
CLAW_HASHES="$SCRIPT_DIR/metadata/clawhub_hashes.json"
EXCLUDE_HASHES="$SCRIPT_DIR/metadata/exclude_hashes_paper_table.txt"
STRICT_DB="$OUT_DIR/full_marketplace_strict.db"

python3 "$SCRIPT_DIR/flatten/extract_skills_additional_flat_db.py" \
  "$INPUT_DB" \
  "$STRICT_DB" \
  --clawhub-json "$CLAW_HASHES" \
  --strict-downloaded-inputs

python3 "$SCRIPT_DIR/paper_artifacts/build_analyzed_skills_table.py" \
  --db "$STRICT_DB" \
  --unique-skills "$UNIQUE_SKILLS"

python3 "$SCRIPT_DIR/paper_artifacts/generate_marketplace_overview_table.py" \
  --db "$STRICT_DB" \
  --indexed-db "$INDEXED_DB" \
  --unique-skills "$UNIQUE_SKILLS" \
  --exclude-hashes "$EXCLUDE_HASHES" \
  --out "$OUT_DIR/table1_marketplace_overview.tex"

python3 "$SCRIPT_DIR/paper_artifacts/generate_timeline_weekly.py" \
  --db "$INPUT_DB" \
  --clawhub-jsonl "$CLAW_JSONL" \
  --clawhub-hashes "$CLAW_HASHES" \
  --out "$OUT_DIR/figure2_timeline_weekly.pdf"

python3 "$SCRIPT_DIR/paper_artifacts/generate_overlap_heatmap.py" \
  --db "$STRICT_DB" \
  --out "$OUT_DIR/figure3_overlap_heatmap.pdf" \
  --include-skills-sh-additional \
  --once \
  --fig-width 5.2 \
  --fig-height 3.9

python3 "$SCRIPT_DIR/paper_artifacts/generate_overlap_upset.py" \
  --db "$STRICT_DB" \
  --out "$OUT_DIR/figure3_overlap_upset.pdf" \
  --fig-width 3.55 \
  --fig-height 3.25

echo "Wrote artifacts to $OUT_DIR"
