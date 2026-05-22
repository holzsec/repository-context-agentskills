# SkillsDirectory Index

`fetch.py` downloads the public SkillsDirectory registry API and writes:

- `skills.jsonl`: one JSON object per indexed skill
- `skills.csv`: selected common fields for inspection

The downloader can consume `skills.jsonl` directly:

```bash
python3 downloader/download_multithreaded.py marketplaces/skillsdirectory/skills.jsonl \
  --root /scans/01_DATA_agentskills \
  --db /scans/01_DATA_agentskills/state/sqlite.db \
  --extract \
  --jobs 32 \
  --filter-blobs \
  --skip-store-repo
```
