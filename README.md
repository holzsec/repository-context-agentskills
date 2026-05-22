# Agent Skills Security - Reproduction Bundle

This repository contains the code and data artifacts accompanying the paper **"Context Matters: Repository-Aware Security Analysis of the Agent Skill Ecosystem"**. The paper studies the security of agent skill marketplaces through three research questions, and all code needed to replicate the reported results is organised by RQ.

---

## Repository Structure

```text
.
├── rq1_skill_dataset/          # RQ1: dataset collection and paper artifacts
│   ├── indexing/               #   marketplace crawlers (Skills.sh, SkillsDirectory,
│   │                           #   GHArchive, ClawHub) and security-info crawlers
│   ├── downloader/             #   multi-threaded repo clone + skill extraction
│   ├── flatten/                #   SQLite -> flat marketplace DB
│   ├── metadata/               #   ClawHub hash universe and exclusion lists
│   ├── paper_artifacts/        #   Table 1, Figure 2, Figure 3 generation scripts
│   ├── generated/              #   output directory (PDF/PNG/TeX written here)
│   └── run_repro_artifacts.sh  #   one-shot script to rebuild all RQ1 paper artifacts
│
├── rq2_malicious_classification/   # RQ2: scanner-based malicious classification
│   ├── scripts/skillscan/          #   Cisco Skill Scanner batch runners
│   ├── scripts/questions_vs_cisco/ #   GPT-vs-Cisco comparison on common hash cohort
│   ├── scripts/tables/             #   scanner table generation (Table 2, Table 3)
│   ├── plots/conditional/          #   cross-scanner agreement and threshold figures
│   ├── tables/                     #   pre-built LaTeX table files
│   ├── artifacts/                  #   small generated summaries used in text
│   └── RQ2.tex                     #   paper section (reference)
│
├── rq3_repository_context/         # RQ3: repository-aware security analysis
│   ├── codex_skillfix/             #   prompts and repository-context evaluation helpers
│   ├── scripts/input_build/        #   flagged skill selection, repo cloning, metadata
│   ├── scripts/analyzers/          #   repository ranking for multi-repo skills
│   ├── scripts/openai/             #   OpenAI batch evaluation runners
│   ├── scripts/data_build/         #   DB import, scoring, and aggregation
│   ├── plots/                      #   metadata, codebase, and score-panel figures
│   ├── figures/                    #   pre-built PDF figures included in the paper
│   └── RQ3.tex                     #   paper section (reference)
│
└── data/                           # data files (download separately - see below)
    ├── rq1_skill_dataset/
    ├── rq2_malicious_classification/
    └── rq3_repository_context/
```

---

## Data Download

Large data files are hosted externally and are **not** included in this repository. Download and extract the archive before running any script:

```
https://drive.proton.me/urls/A4HTQJ4P2R#YeP42TknFzG7
```

After extraction, place the contents so that the `data/` directory has the following layout:

```text
data/
├── rq1_skill_dataset/
│   ├── source/
│   │   ├── sqlite.db
│   │   ├── clawhub_hashed.jsonl
│   │   ├── skills_sh_04_03_2026.db
│   │   ├── skills_sh_skill.db
│   │   ├── skillsdirectory_skills.jsonl
│   │   └── gharchive_skill_repo_github.txt
│   ├── intermediate/
│   │   ├── full_marketplace.db
│   │   └── full_marketplace_strict.db
│   └── unique_skills.txt
│
├── rq2_malicious_classification/
│   ├── security_scan.db
│   ├── security_scan_with_questions.db
│   ├── skills_sh_security_scan_manual_crawl.db
│   ├── results_questions.csv
│   └── results_questions_with_security_scanner_classification.csv
│
└── rq3_repository_context/
    └── repo_context.db
```

---

## RQ1 - Skill Dataset Collection

**Research question:** How large is the agent skill ecosystem, and how do the major marketplaces overlap?

The paper reports four marketplaces crawled as of 04 March 2026:

| Marketplace     | Indexed | Retrieved | Added (unique) |
|-----------------|--------:|----------:|---------------:|
| ClawHub         |  16,755 |    16,755 |         16,755 |
| SkillsDirectory |  32,896 |    17,611 |         17,611 |
| Skills.sh       |  79,735 |   125,928 |        112,231 |
| GitHub Archive  | 142,824 |   136,095 |         91,583 |

**Paper artifacts produced:** Table 1 (marketplace overview), Figure 2 (weekly skill freshness timeline), Figure 3 (cross-marketplace overlap heatmap and UpSet plot).

### Pipeline overview

1. **Index** - marketplace crawlers under `rq1_skill_dataset/indexing/` produce the source files stored in `data/rq1_skill_dataset/source/`.
2. **Download** - `downloader/download_multithreaded.py` clones each GitHub-backed repository, extracts skill folders, hashes them, and records everything to SQLite (`data/rq1_skill_dataset/source/sqlite.db`).
3. **Flatten** - `flatten/extract_skills_additional_flat_db.py` converts the state DB into a single flat table, applying cross-marketplace deduplication and strict downloaded-skill filtering.
4. **Artifacts** - `paper_artifacts/` scripts generate Table 1 and the overlap and timeline figures.

### Rebuild paper artifacts (Table 1, Figures 2 & 3)

```bash
cd rq1_skill_dataset
./run_repro_artifacts.sh
```

Outputs are written to `rq1_skill_dataset/generated/`:

```
table1_marketplace_overview.tex
figure2_timeline_weekly.pdf
figure3_overlap_heatmap.pdf
figure3_overlap_upset.pdf
```

---

## RQ2 - Malicious Classification

**Research question:** How consistently do existing security scanners classify agent skills as malicious, and how reliable are these ratings?

The paper compares five scanners on the shared cohort of 27,111 Skills.sh skills analyzed by all tools. Scanner fail rates range from 3.8 % to 41.9 %, and cross-scanner agreement is low - only 33 of the 8,402 flagged skills are flagged by all five scanners simultaneously. Repository-level aggregation amplifies this effect: 45.9 % of all repositories contain at least one flagged skill, rising to 61.4 % for repositories associated with highly-installed skills.

**Paper artifacts produced:** Table 2 (per-scanner pass/fail rates), Table 3 (repository-level detection rates), Figure 4 (conditional scanner agreement heatmap), Figure 5 (flagged-by-k distribution).

### Running the scripts

**Rebuild scanner tables (Tables 2 & 3):**

```bash
python rq2_malicious_classification/scripts/tables/generate_skillsh_security_tables.py
```

Output: `rq2_malicious_classification/tables/skillsh_security_scanners.tex` and `skillsh_security_scans.tex`.

**Rebuild conditional agreement figures (Figures 4 & 5):**

```bash
python rq2_malicious_classification/plots/conditional/build_skillsh_scanner_comparison.py
python rq2_malicious_classification/plots/conditional/plot_scanner_overlap_heatmaps.py
python rq2_malicious_classification/plots/conditional/plot_flagged_skills_by_threshold.py
```

The builder step joins `data/rq2_malicious_classification/security_scan_with_questions.db`, the RQ1 state database `data/rq1_skill_dataset/source/sqlite.db`, and `skills_sh_security_scan_manual_crawl.db`. It writes `generated/conditional/skillsh_common_5scanner_summary.json` consumed by the two plotting scripts.

**Rebuild GPT-vs-Cisco comparison:**

```bash
python rq2_malicious_classification/scripts/questions_vs_cisco/build_security_scan_with_questions_db.py \
  --append-only \
  --run results_questions_with_security_scanner_classification=data/rq2_malicious_classification/results_questions_with_security_scanner_classification.csv

python rq2_malicious_classification/scripts/questions_vs_cisco/plot_questions_vs_cisco_echarts.py \
  --run-name results_questions \
  --llm-threshold 4
```

### Data inputs

| File | Contents |
|------|----------|
| `security_scan.db` | Cisco Skill Scanner results |
| `security_scan_with_questions.db` | Cisco results + imported GPT question results |
| `skills_sh_security_scan_manual_crawl.db` | Skills.sh marketplace scanner results (Agent Trust Hub, Snyk, Socket) |
| `results_questions.csv` | GPT question output (primary classification CSV) |
| `results_questions_with_security_scanner_classification.csv` | Second GPT run retained for consistency checks |

---

## RQ3 - Repository-Aware Security Analysis

**Research question:** Do skills flagged as malicious by automated scanners remain suspicious when evaluated within the context of their GitHub repositories?

Starting from 8,153 (skill, repository) combinations flagged by both the Cisco Skill Scanner (severity high/critical) and the GPT-based classifier (score > 3), the paper randomly samples 3,000 pairs and evaluates them with a repository-context scoring approach combining:

- **Metadata score (30 %)** - repository size, age, activity, popularity, and issue signals bucketed into predefined ranges.
- **Codebase score (70 %)** - an LLM-based evidence prompt assessing domain alignment, code similarity, README consistency, and maliciousness-adjusted signals from the surrounding codebase.

Key findings: 98.0 % of repositories fall into the lowest maliciousness category; 72 % exhibit at least moderate domain alignment with the skill description; the combined repository-context score has a mean of 58.5, and only 4.2 % of skills score below 40.

**Paper artifacts produced:** Figure 6 (metadata score category bars), Figure 7 (codebase score category bars), Figure 8 (score panel - combined score and subcomponents).

### Scoring details

#### Codebase score (0-100)

Computed in `scripts/data_build/import_openai_eval_results.py` from six LLM-assigned evidence ratings produced by the prompt in `codex_skillfix/prompt_repo_context_eval.md`:

| Dimension | Weight | Evidence levels |
|-----------|-------:|-----------------|
| Domain match | 40 % | evidence (`e`=1.0) / some evidence (`s`=0.5) / no evidence (`n`=0.0) |
| Code match | 35 % | same; `na` (no repo code) falls back to 0.25 |
| README match | 25 % | same; `na` (no README) falls back to 0.25 |

```
codebase_score = 100 x (0.40 x domain_pts + 0.35 x code_pts + 0.25 x readme_pts)
```

The codebase score figure (Figure 7) also plots three further single-axis categories that do not feed directly into the score:

- **Repository maliciousness** - `n`=no evidence (Low), `s`=some evidence (Medium), `e`=evidence (High); lower is better, marked with `*` in the figure.
- **Security-tooling signal** - same three levels; high means the repository is clearly a legitimate security/research tool.
- **Confidence** - `l`=Low, `m`=Medium, `h`=High; reflects evidence strength, not match quality.

All six dimensions use the same numeric encoding stored in the `_num` DB columns: `n`=0 (Low), `s`=1 (Medium), `e`=2 (High).

#### Metadata score (0-100)

Computed in `scripts/data_build/build_repo_context_database.py` as a weighted average over six repository signals. Missing signals are excluded and the remaining weights are renormalised.

| Signal | Weight | Bucket ranges (lowest to highest score) |
|--------|-------:|----------------------------------------|
| Stars | 35 % | 0 -> 1-99 -> 100-999 -> >= 1000 |
| Age | 20 % | < 1 month -> 1 month - 1 year -> > 1 year |
| Repo size | 20 % | < 10 MB -> 10-100 MB -> 100-500 MB -> >= 500 MB |
| Forks | 10 % | 0 -> 1-99 -> 100-999 -> >= 1000 |
| Open issues | 10 % | 0 -> 1-99 -> 100-999 -> >= 1000 |
| Activity | 5 % | > 180 days since push -> 31-180 days -> <= 30 days |

Each signal maps its highest bucket to 1.0, lowest to 0.0, with intermediate levels at 1/3 and 2/3 (four-level signals) or 0.5 (three-level signals).

A prevalence term accounts for skills appearing across multiple repositories:

```
metadata_score = 100 x (0.90 x base_weighted_mean + 0.10 x (1 - exp(-k / 3)))
```

where `k` is the number of distinct repositories containing the same skill hash.

The metadata figure (Figure 6) shows the distribution of each signal across five ordinal levels: **zero**, **low**, **medium**, **high**, **very high**.

#### Repository context score (0-100) and thresholds

```
repository_context_score = max(0, 0.70 x codebase_score + 0.30 x metadata_score - penalty)
```

A penalty of 50 points is applied when the codebase verdict is `aligned_but_repo_suspicious`.

Score bands used in the paper and code:

| Range | Label | Cases (n=2,887) |
|-------|-------|----------------|
| < 40 | likely valid flag | 4.2 % (mean 36.2) |
| 40 - < 60 | uncertain / moderate alignment | 47.6 % (mean 50.8) |
| 60 - < 70 | strong alignment | included in 48.3 % >= 60 |
| >= 70 | likely false positive (`verdict_from_score`) | - |

---

### Pipeline

1. **Select flagged skills** - `scripts/input_build/collect_flagged_skills_input.py` filters skills flagged by both Cisco (high/critical) and the GPT scorer, excludes ClawHub (no repository context), and writes input CSVs.
2. **Rank multi-repo skills** - `scripts/analyzers/rank_repo_context_inputs.py` selects the best repository for skills appearing in multiple repos.
3. **Bundle repositories** - `scripts/input_build/bundle_flagged_skills.py` clones the selected repositories and packages skill bundles for evaluation.
4. **Collect metadata** - `scripts/input_build/collect_repo_metadata.py` builds the repository metadata database.
5. **Evaluate** - `scripts/openai/evaluate_skill_openai_batch.py` runs the repository-context prompt via the OpenAI Batch API.
6. **Build DB** - `scripts/data_build/` imports model outputs, merges metadata and codebase scores, and writes the canonical tables to `data/rq3_repository_context/repo_context.db`.
7. **Plot** - `plots/` scripts generate the three RQ3 figures from `repo_context.db`.

### Rebuild figures

```bash
# Figure 6 - metadata score categories
python rq3_repository_context/plots/metadata/plot_metadata_score_distributions.py

# Figure 7 - codebase score categories
python rq3_repository_context/plots/codebase/plot_codebase_score_categories.py

# Figure 8 - score panel
python rq3_repository_context/plots/score_panel/plot_score_panel.py
```

All scripts read from `data/rq3_repository_context/repo_context.db` and write their output PDFs to `rq3_repository_context/figures/`.

### Database tables in `repo_context.db`

| Table | Description |
|-------|-------------|
| `metadata_repositories` | Final 3,000 sampled skill/repository rows with metadata scores |
| `codebase_scan_results` | 3,084 repository-context model outputs |
| `repository_context_scores` | Merged 3,000-row table used for score panel and interpretation |

---

## Requirements

All scripts are Python 3.10+. Install dependencies with:

```bash
pip install -r requirements.txt
```

Individual script directories may contain their own `requirements.txt` or note additional dependencies (e.g., the Cisco Skill Scanner CLI for `rq2_malicious_classification/scripts/skillscan/`).
