# Skills.sh Crawlers

This directory contains Skills.sh-specific data collection tools.

## Scripts

- `crawl_skills_sh.py` - Crawls `https://skills.sh/api/skills/all-time/{page}`
  and stores normalized skill rows in SQLite.
- `enrich_github_metadata_v2.py` - Reads `owner/repo` values from
  `repo_marketplace_links.repository`, queries the GitHub API, and stores
  repository existence, redirect, and star metadata.

## Notes

`enrich_github_metadata_v2.py` requires a GitHub API token via `--token`.
Both scripts support limits and sleep intervals for controlled test runs.
