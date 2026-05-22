# Skills.sh Index

Skills.sh is represented by marketplace SQLite dumps. This bundle includes:

- `../../data/source/skills_sh_04_03_2026.db`: the paper snapshot used by the
  artifact runner
- `../../data/source/skills_sh_skill.db`: the SQLite dump from the original
  `download_skills` bundle

The indexing/crawling code is in `crawling/`:

- `crawl_skills_sh.py` crawls `https://skills.sh/api/skills/all-time/{page}`
  into a normalized SQLite database.
- `enrich_github_metadata_v2.py` enriches repository rows with GitHub
  existence, redirects, and star metadata.

`downloader/download_multithreaded.py` reads SQLite inputs. For this dump, the
repo owner/name is read from the `source` column in the `github_repo_metadata`
table by default:

```bash
python3 downloader/download_multithreaded.py marketplaces/skills_sh/04_03_2026.db \
  --root /scans/01_DATA_agentskills \
  --db /scans/01_DATA_agentskills/state/sqlite.db \
  --extract \
  --jobs 32 \
  --filter-blobs \
  --skip-store-repo
```

The downloader also reads the companion `skills` table when present, preserving
skill IDs/names/installs in `skill_input_metadata`.
