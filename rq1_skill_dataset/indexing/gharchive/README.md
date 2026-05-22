# GHArchive Index

The final GitHub/GHArchive input used by the artifact runner is:

```text
../../data/source/gharchive_skill_repo_github.txt
```

The relevant indexing script kept here is:

```text
gharchive_sampled_multiple_markers_latest.py
```

It scans GHArchive hourly event data, scores repositories with agent-skill
markers, and emits candidate repositories. The final owner/repo list was then
used as the GitHub-backed marketplace input for `download_multithreaded.py`.

Example shape:

```bash
python3 gharchive_sampled_multiple_markers_latest.py \
  --start 2025-10-01 \
  --workdir /scans/09_DATA_gharchive/agentscan/tmp \
  --outdir /scans/09_DATA_gharchive/agentscan/out \
  --mark-scores 1,2
```
