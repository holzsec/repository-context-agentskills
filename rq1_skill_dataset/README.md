# RQ1: Skill Dataset Reproduction Bundle

This directory contains the marketplace indexing, download, flattening, and artifact-generation code for the collected skill dataset.

## Directory Structure

```text
rq1_skill_dataset/
├── indexing/
│   ├── clawhub/
│   │   ├── clawhub_dl.py
│   │   ├── clawhub_dl_single.py
│   │   ├── cp_skills.py
│   │   └── enrich_clawhub_hashes.py
│   ├── gharchive/
│   │   ├── README.md
│   │   └── gharchive_sampled_multiple_markers_latest.py
│   ├── skills_sh/
│   │   ├── README.md
│   │   └── crawling/
│   │       ├── crawl_skills_sh.py
│   │       └── enrich_github_metadata_v2.py
│   ├── security_infos/
│   │   ├── openclaw/
│   │   │   └── crawl_clawhub_security_to_sqlite.py
│   │   └── skills_sh/
│   │       ├── get_from_api.py
│   │       └── scrape_skills_security.py
│   └── skillsdirectory/
│       ├── README.md
│       └── fetch.py
├── downloader/
│   ├── download_multithreaded.py
│   ├── sqlite_store.py
│   ├── extract.py
│   ├── backfill_additional_skill_hashes.py
│   ├── backfill_skills_last_modified.py
│   ├── fix_additional_skills_paths.py
│   ├── mark_duplicate_skills_by_hash.py
│   └── post_download_skills_sh_sync.py
├── flatten/
│   └── extract_skills_additional_flat_db.py
├── metadata/
│   ├── clawhub_hashes.json
│   └── exclude_hashes_paper_table.txt
├── paper_artifacts/
│   ├── build_analyzed_skills_table.py
│   ├── generate_marketplace_overview_table.py
│   ├── generate_timeline_weekly.py
│   ├── generate_overlap_heatmap.py
│   ├── generate_overlap_upset.py
│   ├── generate_overlap_membership_bars.py
│   ├── strict_marketplace_overview.tex
│   └── additional_tables/
│       ├── README.md
│       ├── analysis/
│       │   └── analysis.py
│       └── evaluation/
│           ├── endpoint_category_analysis.py
│           ├── endpoint_geolocation_analysis.py
│           ├── marketplace_latex_utils.py
│           ├── skill_language_analysis.py
│           ├── verified_secrets_per_marketplace.py
│           └── vulnerable_repos_per_marketplace.py
├── generated/              <- output directory (PDF/PNG/TeX written here)
└── run_repro_artifacts.sh
```

---

## Pipeline Overview

The full data collection pipeline runs in six stages. The ASCII diagram below shows the data flow. Counts in the diagram reflect the **04 March 2026 crawl** and will differ if the pipeline is re-run.

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Marketplace sources                          │
│                                                                     │
│  skills.sh            SkillsDirectory        GH Archive            │
│  04_03_2026.db        skills.jsonl            skill_repo_github.txt │
│  (SQLite, 82,879)     (JSONL, ~33K)           (plain text, ~287K)   │
│  name, slug,          name, slug,             owner/repo per line   │
│  repo, stars, ...     repo, stars, ...                              │
└──────────┬──────────────────┬──────────────────┬────────────────────┘
           │                  │                  │
           └──────────────────┴──────────────────┘
                              │
                              ▼
            ┌─────────────────────────────────────┐
            │      download_multithreaded.py       │
            │                                      │
            │  For each repo:                      │
            │  1. git clone (single-branch,        │
            │     blob filter, timeout)            │
            │  2. extract.py -> find skill folders │
            │  3. hash_folder() -> skill_hash      │
            │  4. record to SQLite                 │
            │  5. rm clone, keep skills/           │
            └──────────────┬──────────────────────┘
                           │
                           ▼
            ┌─────────────────────────────────────┐
            │   /scans/01_DATA_agentskills/        │
            │                                      │
            │  state/sqlite.db   <- main DB        │
            │  skills/           <- extracted skill│
            │                       folders        │
            │  skills_additional/<- skills found   │
            │                       beyond input   │
            │  repo_zip/         <- zipped repos   │
            └──────────────┬──────────────────────┘
                           │
                           ▼
            ┌─────────────────────────────────────┐
            │         Post-processing              │
            │                                      │
            │  backfill_additional_skill_hashes.py │
            │  backfill_skills_last_modified.py    │
            │  fix_additional_skills_paths.py      │
            │  post_download_skills_sh_sync.py     │
            └──────────────┬──────────────────────┘
                           │
                           ▼
            ┌─────────────────────────────────────┐
            │   data/sqlite.db  (snapshot copy)    │
            │                                      │
            │  skills              ~58K rows       │
            │  skills_additional   ~376K rows      │
            │  skill_marketplace_links             │
            │  skills_additional_marketplace_links │
            │  repos / marketplaces / ...          │
            └──────────────┬──────────────────────┘
                           │
                           ▼
            ┌─────────────────────────────────────┐
            │  extract_skills_additional_flat_db   │
            │                                      │
            │  + skill_input_metadata (primary)    │
            │  + skills_additional (additional)    │
            │  + clawhub_hashes.json               │
            │  - dedup skill.sh overlap            │
            │  - dedup (hash, marketplace)         │
            └──────────────┬──────────────────────┘
                           │
                           ▼
            ┌─────────────────────────────────────┐
            │   full_marketplace_strict.db         │
            │   table: skills_additional_flat      │
            │                                      │
            │  gharchive          142,824          │
            │  skill.sh_additional 77,456          │
            │  skill.sh            55,366          │
            │  skillsdirectory     17,624          │
            │  clawhub             16,755          │
            │  Total              310,025          │
            └─────────────────────────────────────┘
```

---

## Step 1 - Marketplace Input Files

Each marketplace provides a listing of known agent skill repositories.

| Marketplace | File | Format | Key fields |
|-------------|------|--------|-----------|
| Skills.sh | `../data/rq1_skill_dataset/source/skills_sh_04_03_2026.db` | SQLite (`skills` table) | `name`, `slug`, `repository`, `stars` |
| SkillsDirectory | `../data/rq1_skill_dataset/source/skillsdirectory_skills.jsonl` | JSONL | `name`, `slug`, `repository`, `stars`, `category`, `author` |
| GH Archive | `../data/rq1_skill_dataset/source/gharchive_skill_repo_github.txt` | Plain text | one `owner/repo` per line |
| ClawHub | `metadata/clawhub_hashes.json` | JSON dict | `skill_hash -> skill_name` (not cloned - no public git repo) |

The Skills.sh index crawler is `indexing/skills_sh/crawling/crawl_skills_sh.py`; `enrich_github_metadata_v2.py` enriches rows with GitHub existence, redirect, and star metadata. The SkillsDirectory fetcher is `indexing/skillsdirectory/fetch.py`. The GHArchive marker script is `indexing/gharchive/gharchive_sampled_multiple_markers_latest.py`. ClawHub listing and download use `indexing/clawhub/clawhub_dl.py`; hash enrichment uses `indexing/clawhub/enrich_clawhub_hashes.py`. Security metadata crawlers for Skills.sh and OpenClaw/ClawHub live under `indexing/security_infos/`.

---

## Step 2 - Clone, Extract, Hash (`downloader/download_multithreaded.py`)

```bash
# Phase 1: clone repos + extract skill folders
python3 downloader/download_multithreaded.py ../data/rq1_skill_dataset/source/skills_sh_04_03_2026.db \
    --root /scans/01_DATA_agentskills \
    --filter-blobs --single-branch --clone-timeout-min 2 \
    --zip-max-repo-mb 10 --max-extracted-skills-mb 200 \
    --extract --skip-store-repo --jobs 40

# Phase 2: populate SQLite from already-extracted skills (no re-clone needed)
python3 downloader/download_multithreaded.py ../data/rq1_skill_dataset/source/skills_sh_04_03_2026.db \
    --root /scans/01_DATA_agentskills \
    --db /scans/01_DATA_agentskills/state/sqlite.db \
    --from-extracted-skills --jobs 24
```

Repeat for SkillsDirectory (`skillsdirectory_skills.jsonl`) and GH Archive (`gharchive_skill_repo_github.txt`).

**What happens per repo:**

1. `git clone --single-branch --filter=blob:none` into a temp workspace
2. `extract.py` scans the repo tree and identifies skill folders (folders containing a `skill.md` / config file matching Claude skill conventions)
3. Each skill folder is content-hashed (SHA-256 over all non-git files) -> `skill_hash`
4. The skill path, hash, and marketplace links are written to SQLite
5. The clone is deleted; the extracted `skills/` folder is kept on disk

**`skills` vs `skills_additional`:**

- **`skills`** - skill folders that directly match a slug/entry from the marketplace input
- **`skills_additional`** - skill folders discovered in the same repo that were *not* in the input listing (found by `extract.py` scanning the whole repo)

---

## Step 3 - Post-processing

After all marketplaces are downloaded, run the backfill scripts to complete the DB:

```bash
# Fix path references for skills_additional entries
python3 downloader/fix_additional_skills_paths.py \
    --db /scans/01_DATA_agentskills/state/sqlite.db \
    --root /scans/01_DATA_agentskills

# Backfill skill_hash for skills_additional rows that missed hashing
python3 downloader/backfill_additional_skill_hashes.py \
    --db /scans/01_DATA_agentskills/state/sqlite.db \
    --root /scans/01_DATA_agentskills \
    --old-db /scans/01_DATA_agentskills/state/backup/<timestamp>/sqlite.db

# Backfill skill_last_modified_at from git log for all skills
python3 downloader/backfill_skills_last_modified.py \
    --db /scans/01_DATA_agentskills/state/sqlite.db \
    --old-db /scans/01_DATA_agentskills/state/backup/<timestamp>/sqlite.db

# Sync skills.sh metadata updates (stars, slugs) into the DB
python3 downloader/post_download_skills_sh_sync.py \
    --input-db ../data/rq1_skill_dataset/source/skills_sh_04_03_2026.db \
    --state-db /scans/01_DATA_agentskills/state/sqlite.db \
    --root /scans/01_DATA_agentskills
```

---

## Step 4 - Copy Snapshot

`../data/rq1_skill_dataset/source/sqlite.db` is a snapshot copy of the live state DB used by all analysis and paper scripts:

```bash
cp /scans/01_DATA_agentskills/state/sqlite.db ../data/rq1_skill_dataset/source/sqlite.db
```

---

## Step 5 - Flatten (`flatten/extract_skills_additional_flat_db.py`)

This script produces a single flat table `skills_additional_flat` combining all skill sources for analysis.

```bash
python3 flatten/extract_skills_additional_flat_db.py \
    ../data/rq1_skill_dataset/source/sqlite.db \
    ../data/rq1_skill_dataset/intermediate/full_marketplace.db \
    --clawhub-json metadata/clawhub_hashes.json
```

For analyses that should only count downloaded/extracted skills, use strict input mode (this is what the paper uses):

```bash
python3 flatten/extract_skills_additional_flat_db.py \
    ../data/rq1_skill_dataset/source/sqlite.db \
    ../data/rq1_skill_dataset/intermediate/full_marketplace_strict.db \
    --clawhub-json metadata/clawhub_hashes.json \
    --strict-downloaded-inputs
```

**What it does:**

1. Joins `skills_additional -> skills_additional_marketplace_links -> marketplaces` to get all additional skills with their marketplace name
2. Reads `skill_input_metadata` to get primary (marketplace-listed) skills with hash + marketplace name
3. Adds ClawHub skills from `clawhub_hashes.json` (`{skill_hash: skill_name}` dict) - these have no git repo
4. Normalises marketplace names: `skills_sh` -> `skill.sh` (primary) or `skill.sh_additional` (additional)
5. Drops `skill.sh_additional` rows where the same hash exists in `skill.sh` (avoiding double-counting)
6. Deduplicates by `(skill_hash, marketplace)` pair; skills without a `skill_hash` get a synthetic key `nohash:repo:path` in non-strict mode

With `--strict-downloaded-inputs`, step 2 only includes `skill_input_metadata` rows where `skill_downloaded=1` and `skill_hash` is a real non-empty hash. Use this mode for downloaded-skill, analyzed-skill, and content-overlap counts.

**Non-strict output (04 Mar 2026 snapshot):**

| marketplace | rows |
|-------------|------|
| gharchive | 142,824 |
| skill.sh | 79,735 |
| skill.sh_additional | 77,456 |
| skillsdirectory | 32,896 |
| clawhub | 16,755 |
| **Total** | **349,666** |

**Strict output (04 Mar 2026 snapshot) - used for all paper numbers:**

| marketplace | rows |
|-------------|------|
| gharchive | 142,824 |
| skill.sh_additional | 77,456 |
| skill.sh | 55,366 |
| skillsdirectory | 17,624 |
| clawhub | 16,755 |
| **Total** | **310,025** |

Strict mode contains 245,077 distinct `skill_hash` values and excludes the 39,641 unmatched marketplace input rows represented as synthetic `nohash:*` identifiers in non-strict mode.

---

## Step 6 - Tier Deduplication

The tier-ordered, hash-deduplicated skill set used for analysis is produced by assigning each unique hash to its highest-priority marketplace. Deduplication priority: `skillsdirectory -> skills_sh -> gharchive -> skills_sh_additional`. The packaging script used for the original crawl is not included in this artifact bundle; the pre-built outputs are distributed via the data archive.

**Tier specs and deduplication order (04 Mar 2026 snapshot):**

| Tier prefix | Source | Table | Unique added |
|-------------|--------|-------|-------------|
| `00` | skillsdirectory | `skill_input_metadata` (primary) | 17,624 |
| `01` | skills_sh | `skill_input_metadata` (primary) | 48,733 |
| `02` | gharchive | `skills_additional_marketplace_links` | 122,010 |
| `03` | skills_sh | `skills_additional_marketplace_links` | 40,009 |
| `010` | clawhub | (tarball only) | ~16,755 |

Total unique skills (tiers 00-03): **228,376**. Including the ClawHub tier (`010`), the analysis directory contains **238,228** entries named `{tier_prefix}_{skill_hash}`. Tier prefixes: `00=skillsdirectory`, `01=skills_sh`, `02=gharchive`, `03=skills_sh (additional)`, `010=clawhub`.

---

## Rebuild Paper Artifacts (Table 1, Figures 2 & 3)

```bash
cd rq1_skill_dataset
./run_repro_artifacts.sh
```

The script reads from `../data/rq1_skill_dataset/` and writes outputs to `generated/`:

```
full_marketplace_strict.db
table1_marketplace_overview.tex
figure2_timeline_weekly.pdf
figure2_timeline_weekly.png
figure3_overlap_heatmap.pdf
figure3_overlap_upset.pdf
```

**Table 1 column order:** `ClawHub | SkillsDir. | Skills.sh | GitHub`. `Retrieved` is the analyzed/retrieved-for-analysis count. `Added` is the left-to-right marginal contribution after cross-source deduplication (order-dependent).

**Figure 2 (weekly timeline):** The log-scaled y-axis is labelled `Modified/uploaded skills` because the plot is a freshness metric, not a pure creation-rate metric. Each skill is placed in the week of its latest `skill_last_modified_at` timestamp. For ClawHub, enriched timestamps are read from `../data/rq1_skill_dataset/source/clawhub_hashed.jsonl` and filtered to the 16,755 hashes in `metadata/clawhub_hashes.json`. By default the GitHub series includes `skills_additional` rows linked to `gharchive`; use `--no-gharchive-additional` to plot only marketplace-listed GitHub skills.

Individual artifact scripts:

- `paper_artifacts/build_analyzed_skills_table.py` - creates `analyzed_skills` in `full_marketplace_strict.db`
- `paper_artifacts/generate_marketplace_overview_table.py` - Table 1
- `paper_artifacts/generate_timeline_weekly.py` - Figure 2
- `paper_artifacts/generate_overlap_heatmap.py` - Figure 3 (heatmap)
- `paper_artifacts/generate_overlap_upset.py` - Figure 3 (UpSet plot)
- `paper_artifacts/additional_tables/` - endpoint, geolocation, language, verified-secret, and vulnerable-repository tables

---

## SQLite DB Schema (Key Tables)

| Table | Description |
|-------|-------------|
| `skills` | One row per unique skill from marketplace input; `skill_hash`, `skill_last_modified_at`, `skill_path` |
| `skills_additional` | Skills discovered beyond the input listing |
| `skill_marketplace_links` | N:M skill <-> marketplace (via `marketplace_id`) |
| `skills_additional_marketplace_links` | Same for `skills_additional` |
| `repos` | One row per GitHub repo; metadata from git + optional GitHub API |
| `repo_marketplace_links` | N:M repo <-> marketplace |
| `marketplaces` | `id`, `name` - e.g. `1=skills_sh`, `2252845=skillsdirectory`, `2361849=gharchive` |
| `skill_input_metadata` | Raw per-skill metadata from marketplace input (stars, installs, slug, canonical path, download status) |

### Why `skill_input_metadata` and `skills` differ

`skill_input_metadata` is an input tracking table; `skills` is an extracted-skill table. They intentionally count different things.

During `download_multithreaded.py`, every marketplace slug is first written to `skill_input_metadata` when the repository is queued, marked as pending with no `skill_hash`. After cloning/extraction:

- If a matching folder is found: `skills` gets one row, `skill_marketplace_links` links it to the marketplace, and `skill_input_metadata` is updated with `skill_downloaded=1`, the canonical skill path, and the hash.
- If no match is found or the repo cannot be cloned: the `skill_input_metadata` row stays with a failure status (`not_found`, `repo_missing`, `repo_failed`).

Uniqueness keys differ:

```
skills:               UNIQUE(repository, skill_path)
skill_input_metadata: PRIMARY KEY(repository, skill_path, marketplace, slug)
```

`skill_input_metadata` can hold multiple slug-level rows that map to one extracted skill folder; `skills` collapses those to the actual folder path.

---

## Counts (04 Mar 2026 Snapshot)

### Raw state DB (before deduplication)

| Source | `skills` (primary) | `skills_additional` | Notes |
|--------|---------------------|---------------------|-------|
| skills.sh | 58,478 | 89,426 | |
| SkillsDirectory | 20,642 | - | |
| GH Archive | - | 286,855 | no primary listing |
| ClawHub | - | - | no git repo; hashes from `clawhub_hashes.json` |

### `skill_input_metadata` download status

| Marketplace | `skill_input_metadata` rows | downloaded | not downloaded | `skills` rows |
|-------------|-----------------------------|-----------|----------------|--------------|
| skills.sh | 82,879 | 58,508 | 24,371 | 58,478 |
| SkillsDirectory | 36,109 | 20,837 | 15,272 | 20,642 |

Breakdown of non-downloaded rows by `match_status` / `match_reason`:

| Marketplace | Status | Reason | Rows |
|-------------|--------|--------|------|
| skills.sh | matched | - | 58,508 |
| skills.sh | not_found | input_skill_not_present_anymore | 20,380 |
| skills.sh | repo_missing | repo_skills_folder_missing | 3,991 |
| SkillsDirectory | matched | - | 20,837 |
| SkillsDirectory | repo_failed | clone_timeout_or_repo_too_large | 13,304 |
| SkillsDirectory | not_found | input_skill_not_present_anymore | 1,420 |
| SkillsDirectory | pending | - | 360 |
| SkillsDirectory | repo_failed | auth_required_or_private_repo | 188 |

### Paper table counts (strict, after tier deduplication)

| Metric | ClawHub | SkillsDir. | Skills.sh | GitHub |
|--------|--------:|----------:|----------:|-------:|
| Indexed | 16,755 | 32,896 | 79,735 | 142,824 |
| Retrieved | 16,755 | 17,611 | 125,928 | 136,095 |
| Added | 16,755 | 17,611 | 112,231 | 91,583 |
| Owners | n/a | 709 | 7,950 | 14,197 |
| Repositories | n/a | 766 | 9,431 | 16,413 |

The strict analyzed set contains **238,180** distinct hashes after excluding the two GitHub hashes listed in `metadata/exclude_hashes_paper_table.txt`.

### Pairwise strict hash overlaps

| Pair | Overlap |
|------|--------:|
| ClawHub / GHArchive | 50 |
| ClawHub / Skills.sh | 9 |
| ClawHub / SkillsDir. | 0 |
| GHArchive / Skills.sh | 50,909 |
| GHArchive / SkillsDir. | 6,090 |
| Skills.sh / SkillsDir. | 13,700 |
