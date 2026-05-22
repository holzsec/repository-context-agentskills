# Additional Analysis Tables

This folder contains the extra table-generation and analysis code from
`download_skills/david`, beyond Table 1 and Figures 2/3.

## `analysis/`

- `analysis.py`: scans extracted skill directories, records file metadata,
  endpoint/reference findings, command snippets, and verified secret findings in
  SQLite.

## `evaluation/`

- `endpoint_category_analysis.py`: groups endpoint findings by tracker/category.
- `endpoint_geolocation_analysis.py`: resolves endpoint hosts and writes
  location/geography tables.
- `skill_language_analysis.py`: summarizes programming language usage from file
  suffixes.
- `verified_secrets_per_marketplace.py`: summarizes verified secret findings per
  marketplace.
- `vulnerable_repos_per_marketplace.py`: summarizes vulnerable repositories and
  affected skills per marketplace.
- `marketplace_latex_utils.py`: shared marketplace ordering and LaTeX table
  helper required by the endpoint table scripts.

These scripts expect the analysis/security SQLite databases produced by
`analysis.py` and the security crawlers in `../../indexing/security_infos/`.
