# Evaluation Scripts

This directory contains reporting scripts that read the collected SQLite
databases and produce CSV, LaTeX, or PDF outputs.

## Scripts

- `endpoint_category_analysis.py` - Groups discovered endpoints by tracker and
  category, with optional Exodus tracker metadata.
- `endpoint_geolocation_analysis.py` - Resolves endpoint hosts to IPs and
  geolocation buckets for marketplace comparisons.
- `generate_marketplace_overview_table.py` - Generates the marketplace overview
  LaTeX table.
- `generate_overlap_heatmap.py` - Renders a marketplace overlap heatmap from
  skill hashes.
- `skill_language_analysis.py` - Counts programming languages by file suffix.
- `verified_secrets_per_marketplace.py` - Summarizes verified secrets by
  marketplace.
- `vulnerable_repos_per_marketplace.py` - Summarizes vulnerable repositories and
  related skill counts by marketplace.

Most scripts default to local database and output paths. Run a script with
`--help` before use to verify the expected input schema and output location.
