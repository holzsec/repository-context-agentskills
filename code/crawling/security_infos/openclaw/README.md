# OpenClaw Security Crawler

This directory contains the ClawHub/OpenClaw security scan crawler.

## Script

- `crawl_clawhub_security_to_sqlite.py` - Reads the latest skill records from a
  `pipeline.jsonl` export, queries the ClawHub Convex API for each slug, and
  stores VirusTotal and OpenClaw findings in SQLite.

Use `--resume` to skip slugs already present in `security_scans`, and tune
`--workers`, `--timeout`, and `--retries` for the network environment.
