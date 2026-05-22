# RQ2: Malicious Classification

This folder contains the code used for scanner-based malicious-classification analysis, including scanner batch execution, cross-scanner agreement plots, and the repository-level flagged-skill aggregation used to motivate the repository-aware analysis.

## Contents

```text
scripts/skillscan/
  scan_skills_batch.py
  scan_skills_api_batch.py

scripts/questions_vs_cisco/
  build_security_scan_with_questions_db.py
  plot_questions_vs_cisco_echarts.py

scripts/tables/
  generate_skillsh_security_tables.py

plots/conditional/
  build_skillsh_scanner_comparison.py
  plot_flagged_skills_by_threshold.py
  plot_scanner_overlap_heatmaps.py

tables/
  skillsh_security_scanners.tex
  skillsh_security_scans.tex

artifacts/questions_vs_cisco/
  summary.json
  skills_sh_scanner_table_rows_questions_vs_cisco.tex
```

The `plots/conditional/` scripts generate the two Skills.sh agreement figures used by `RQ2.tex`.

## Data Inputs

RQ2 data is stored under `../data/rq2_malicious_classification/`:

- `security_scan.db`: Cisco Skill Scanner results.
- `security_scan_with_questions.db`: Cisco results plus imported GPT question results.
- `skills_sh_security_scan_manual_crawl.db`: Skills.sh marketplace scanner results for Agent Trust Hub, Snyk, and Socket.
- `results_questions.csv`: GPT question output CSV. This is the research-question CSV referred to in notes.
- `results_questions_with_security_scanner_classification.csv`: second GPT question output CSV retained for rebuilding/consistency checks.

The import script is:

```bash
python scripts/questions_vs_cisco/build_security_scan_with_questions_db.py \
  --append-only \
  --run results_questions_with_security_scanner_classification=../data/rq2_malicious_classification/results_questions_with_security_scanner_classification.csv
```

This appends question CSV rows into `security_scan_with_questions.db`, which already contains the Cisco `scan_results` table.

## Reported Rates

The marketplace scanner rates in the paper are derived from the scanner output tables and summarized by the plotting scripts in `plots/conditional/`.

The Skills.sh GPT-vs-Cisco comparison is computed on the common hash cohort where both the question-based GPT run and Cisco scan have results. The current artifact is:

```text
artifacts/questions_vs_cisco/skills_sh_scanner_table_rows_questions_vs_cisco.tex
```

It reports `n=52,577`, GPT question threshold `overal_malicousness_rating >= 4`, GPT flagged `14,343 / 52,577 = 27.28%`, and Cisco flagged `7,381 / 52,577 = 14.04%`.

The generic GPT-vs-Cisco evaluation script is:

```bash
python scripts/questions_vs_cisco/plot_questions_vs_cisco_echarts.py \
  --run-name results_questions \
  --llm-threshold 4
```

The checked-in table rows above are preserved as the paper artifact for the Skills.sh cohort used in the text.

## Skills.sh Scanner Tables

The two table files included by `RQ2.tex` are:

```text
tables/skillsh_security_scanners.tex
tables/skillsh_security_scans.tex
```

They are regenerated from the packaged Skills.sh marketplace-scanner database:

```bash
python scripts/tables/generate_skillsh_security_tables.py
```

`skillsh_security_scanners.tex` reports the per-scanner pass/fail counts for Agent Trust Hub, Snyk, and Socket. `skillsh_security_scans.tex` aggregates the same skill-level flags into the strict skill and repository detection rates reported in `RQ2.tex`.

## Skills.sh Conditional Agreement

The conditional scanner agreement figures in `RQ2.tex` are produced in three steps:

```bash
python plots/conditional/build_skillsh_scanner_comparison.py
python plots/conditional/plot_scanner_overlap_heatmaps.py
python plots/conditional/plot_flagged_skills_by_threshold.py
```

`build_skillsh_scanner_comparison.py` joins the packaged `security_scan_with_questions.db`, the RQ1 state database `../data/rq1_skill_dataset/source/sqlite.db`, and `skills_sh_security_scan_manual_crawl.db`. It writes `generated/conditional/skillsh_common_5scanner_summary.json`, which is then consumed by the heatmap and flagged-by-k plot scripts. The builder only writes this summary JSON; figure files are created by the two plotting scripts. The `generated/` directory is intentionally ignored; rerun the commands above to recreate the paper figures instead of storing every intermediate HTML/PNG/PDF in the reproduction bundle.

The generated summary reproduces the values used in `RQ2.tex`: `27,111` Skills.sh skills evaluated by all five scanners, `8,402` flagged by at least one scanner, and the flagged-by-k distribution `6,032`, `1,629`, `540`, `168`, and `33`.
