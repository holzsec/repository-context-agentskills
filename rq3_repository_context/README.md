# RQ3: Repository-Aware Security Analysis

This folder contains the repository-aware security-analysis pipeline. It keeps the latest code used for prompt-based repository-context evaluation, database construction, scoring, and figure generation. The interactive API service is not part of this reproduction bundle.

## Contents

```text
codex_skillfix/
  prompt_repo_context_eval.md
  prompt_skill_content_eval.md
  generate_repository_context_log.py

scripts/openai/
  evaluate_skill_openai.py
  evaluate_skill_openai_batch.py

scripts/input_build/
  collect_flagged_skills_input.py
  bundle_flagged_skills.py
  collect_repo_metadata.py

scripts/analyzers/
  rank_repo_context_inputs.py

scripts/data_build/
  build_repo_context_database.py
  build_api_results_table.py
  import_openai_eval_results.py

plots/metadata/
  plot_metadata_score_distributions.py
  plot_repo_context_api_distributions.py

plots/codebase/
  plot_codebase_score_categories.py

plots/score_panel/
  plot_score_panel.py

figures/
  metadata_score_categories.pdf
  codebase_score_categories.pdf
  score_panel.pdf
  metadata_score_percentile_lines_by_marketplace.csv
```

Large RQ3 data is stored outside this code folder:

```text
../data/rq3_repository_context/repo_context.db
```

## Paper Figures

`RQ3.tex` references four figures:

- `figures/metadata_score_categories.pdf`
- `figures/codebase_score_categories.pdf`
- `figures/score_panel.pdf`
- `figures/maturity_validation.pdf`

The first three are included here. `maturity_validation.pdf` is still missing from the reproducibility bundle because the manual reviewer score file and the script that generated that validation plot are not present in the current repo.

The SQLite database in `../data/rq3_repository_context/repo_context.db` is a compact paper artifact, not the full working database. It contains only the final tables used by RQ3:

- `metadata_repositories`: the final 3,000 sampled skill/repository metadata rows used for metadata buckets.
- `codebase_scan_results`: the 3,084 repository-context model outputs before joining them to the sampled metadata spine.
- `repository_context_scores`: the final 3,000-row merged table used for the score panel and score interpretation.

Older working tables and suffixes such as `v2`/`v3` are intentionally omitted from the shared DB. The scripts in this folder use the canonical table names above.

## Pipeline Mapping

The RQ3 pipeline is:

1. `scripts/input_build/collect_flagged_skills_input.py` selects skills flagged by both Cisco high/critical findings and the GPT-based maliciousness score, excludes marketplaces without repository context such as ClawHub, and writes the flagged skill/repository input CSVs.
2. `scripts/analyzers/rank_repo_context_inputs.py` ranks candidate repositories for skills that appear in multiple repositories.
3. `scripts/input_build/bundle_flagged_skills.py` clones the selected repositories and prepares full-repository and stripped skill bundles for repository-context evaluation.
4. `scripts/input_build/collect_repo_metadata.py` builds the repository metadata database used by the metadata score.
5. `codex_skillfix/generate_repository_context_log.py` and `scripts/openai/*` create the prompt inputs and run the repository-context model evaluation.
6. `scripts/data_build/*` imports the model outputs, merges metadata and codebase scores, and builds the canonical final tables in `../data/rq3_repository_context/repo_context.db`.
7. `plots/*` generate the RQ3 paper figures from `repo_context.db`.
